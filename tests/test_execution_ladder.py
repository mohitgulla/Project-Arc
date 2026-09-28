"""The D24 price-band ladder (E6.2): steps, cancel-confirm, fills, position model, journal.

The broker is a scripted fake: each submitted attempt gets a list of statuses
that ``order_status`` returns in turn (the last one repeats). The clock is fake
and ``sleep`` advances it, so a 60 s step runs instantly.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.broker.base import BrokerOrderStatus, MlegOrder
from arc.execution.ladder import ExecStatus, execute, fill_net_price
from arc.gate import HaltSwitch, issue_token, proposal_hash
from arc.gate.band import PriceBand
from arc.models import ApprovalDecision, ApprovalRecord, GateDecision, Proposal
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo, HaltRepo, OrderRepo, ProposalRepo
from tests import test_execution_submit as S

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings

SECRET = S.SECRET
NOW = S.NOW
BAND = PriceBand(lo=D("-0.85"), hi=D("-0.76"), max_steps=3)  # -0.85 -0.82 -0.79 -0.76


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> dt.datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += dt.timedelta(seconds=s)


def st(bid: str, status: str, filled: int = 0, price: str | None = None) -> BrokerOrderStatus:
    return BrokerOrderStatus(
        broker_order_id=bid,
        status=status,
        filled_qty=D(filled),
        filled_avg_price=D(price) if price is not None else None,
    )


class ScriptedBroker:
    """Attempt k returns ``scripts[k]`` statuses in order; the last one repeats.

    ``cancel_script[k]`` (optional) replaces the remaining statuses once cancel is called.
    Records every call so tests can check ordering (one working order at a time).
    """

    def __init__(
        self,
        scripts: list[list[str]],
        cancel_scripts: dict[int, list[tuple[str, int]]] | None = None,
        fills: dict[int, tuple[int, str]] | None = None,
    ) -> None:
        self.scripts = scripts
        self.cancel_scripts = cancel_scripts or {}
        self.fills = fills or {}
        self.orders: list[MlegOrder] = []
        self.calls: list[tuple[str, int, str]] = []
        self._queue: dict[str, list[tuple[str, int]]] = {}
        self.working: set[str] = set()
        self.max_working = 0

    def submit_mleg(self, order: MlegOrder) -> str:
        k = len(self.orders)
        self.orders.append(order)
        bid = f"brk-{k}"
        filled = self.fills.get(k, (0, ""))[0]
        self._queue[bid] = [
            (s, filled if s in ("filled", "partially_filled") else 0) for s in self.scripts[k]
        ]
        self.calls.append(("submit", k, str(order.limit_price)))
        self.working.add(bid)
        self.max_working = max(self.max_working, len(self.working))
        return bid

    def cancel(self, broker_order_id: str) -> None:
        k = int(broker_order_id.split("-")[1])
        self.calls.append(("cancel", k, ""))
        if k in self.cancel_scripts:
            self._queue[broker_order_id] = list(self.cancel_scripts[k])

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        k = int(broker_order_id.split("-")[1])
        q = self._queue[broker_order_id]
        status, filled = q.pop(0) if len(q) > 1 else q[0]
        self.calls.append(("status", k, status))
        if status in ("filled", "canceled", "expired", "rejected"):
            self.working.discard(broker_order_id)
        price = self.fills.get(k, (0, None))[1] if filled else None
        return st(broker_order_id, status, filled, price)


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"execution_step_seconds": 60, "execution_poll_seconds": 2.0}
    base.update(kw)
    return S.cfg(**base)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def seed(conn: sqlite3.Connection, p: Proposal) -> None:
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7, id=p.candidate_id
    )
    assert cid == p.candidate_id
    ProposalRepo(conn).insert(
        candidate_id=cid,
        proposal_hash=proposal_hash(p),
        structure_json=p.structure.model_dump_json(),
        thesis=p.thesis,
        quant_json=p.quant.model_dump_json(),
        sizing_json=p.sizing.model_dump_json(),
        expires_at=p.expires_at.isoformat(),
        ticker="SPY",
    )


def approved(p: Proposal) -> ApprovalRecord:
    return ApprovalRecord(
        proposal_hash=proposal_hash(p),
        slack_user="U0C5KUMH28G",
        slack_ts="1.0",
        decision=ApprovalDecision.APPROVED,
        at=NOW - dt.timedelta(minutes=1),
    )


def gated(p: Proposal, band: PriceBand | None = BAND) -> GateDecision:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    return issue_token(d, p, secret=SECRET.encode(), now=NOW, band=band)


def run(conn, broker, *, p: Proposal | None = None, decision=None, config=None, clock=None, **kw):
    p = p or S.proposal(expires_at=NOW + dt.timedelta(minutes=20))
    if not conn.execute("SELECT 1 FROM proposals").fetchone():
        seed(conn, p)
    c = clock or Clock()
    return execute(
        p,
        decision or gated(p),
        approved(p),
        conn=conn,
        broker=broker,
        config=config or cfg(),
        halt=HaltSwitch(HaltRepo(conn)),
        clock=c,
        sleep=c.sleep,
        **kw,
    )


def journal(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT choice, reason_code FROM decisions ORDER BY rowid").fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_fills_at_mid(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
    out = run(conn, b)
    assert out.status is ExecStatus.FILLED
    assert out.steps_used == 0 and out.filled_qty == 2 and out.fill_price == D("-0.85")
    (order,) = b.orders
    assert order.limit_price == D("-0.85") and order.client_order_id.endswith(".s0")
    assert ("cancel", 0, "") not in b.calls
    ex = ExecutionRepo(conn).get(out.proposal_hash)
    assert ex is not None and ex["status"] == "filled" and ex["attempts"] == 1
    (os_,) = OpenStructureRepo(conn).list_open()
    assert os_["contracts"] == 2 and D(os_["entry_net"]) == D("-0.85")
    lots = conn.execute("SELECT occ_symbol, side, qty FROM tax_lots ORDER BY occ_symbol").fetchall()
    assert [tuple(r) for r in lots] == [(S.LP, "long", 2), (S.SP, "short", 2)]
    fills = conn.execute("SELECT qty, price FROM fills").fetchall()
    assert [tuple(r) for r in fills] == [(2, "-0.85")]
    assert ("filled", "order:filled") in journal(conn)


def test_steps_through_band_then_fills(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker(
        [["new"], ["new"], ["new", "filled"]],
        cancel_scripts={0: [("pending_cancel", 0), ("canceled", 0)], 1: [("canceled", 0)]},
        fills={2: (2, "-0.79")},
    )
    out = run(conn, b)
    assert out.status is ExecStatus.FILLED and out.steps_used == 2
    assert [o.limit_price for o in b.orders] == [D("-0.85"), D("-0.82"), D("-0.79")]
    assert [o.client_order_id[-3:] for o in b.orders] == [".s0", ".s1", ".s2"]
    assert all(BAND.contains(o.limit_price) for o in b.orders)
    assert b.max_working == 1, "never two working orders"
    states = conn.execute("SELECT state FROM orders ORDER BY created_at, rowid").fetchall()
    assert [r[0] for r in states] == ["cancelled", "cancelled", "filled"]
    codes = [c for _, c in journal(conn)]
    assert codes.count("order:step") == 3 and codes.count("order:timeout_cancelled") == 2


def test_next_attempt_waits_for_cancel_confirm(conn: sqlite3.Connection) -> None:
    """D28: attempt k+1 is sent only after the broker reports attempt k canceled."""
    b = ScriptedBroker(
        [["new"], ["new", "filled"]],
        cancel_scripts={0: [("pending_cancel", 0)] * 3 + [("canceled", 0)]},
        fills={1: (2, "-0.82")},
    )
    out = run(conn, b)
    assert out.status is ExecStatus.FILLED and out.steps_used == 1
    submit_1 = b.calls.index(("submit", 1, "-0.82"))
    canceled_0 = b.calls.index(("status", 0, "canceled"))
    assert canceled_0 < submit_1
    pending = [i for i, c in enumerate(b.calls) if c == ("status", 0, "pending_cancel")]
    assert len(pending) == 3 and max(pending) < submit_1


def test_cancel_never_confirmed_stops_the_ladder(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["new"]], cancel_scripts={0: [("pending_cancel", 0)]})
    out = run(conn, b, config=cfg(execution_cancel_confirm_seconds=10))
    assert out.status is ExecStatus.UNCONFIRMED
    assert len(b.orders) == 1
    assert ("failed", "order:cancel_unconfirmed") in journal(conn)
    ex = ExecutionRepo(conn).get(out.proposal_hash)
    assert ex is not None and ex["status"] == "unconfirmed"


def test_fill_during_cancel_stops_ladder(conn: sqlite3.Connection) -> None:
    """The cancel races a fill: record the fill, send nothing more."""
    b = ScriptedBroker(
        [["new"]],
        cancel_scripts={0: [("pending_cancel", 0), ("filled", 2)]},
        fills={0: (2, "-0.85")},
    )
    out = run(conn, b)
    assert out.status is ExecStatus.FILLED and out.steps_used == 0
    assert len(b.orders) == 1
    assert out.structure_id is not None


def test_partial_fill_then_cancel_stops(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker(
        [["new"]],
        cancel_scripts={0: [("canceled", 1)]},
        fills={0: (1, "-0.85")},
    )
    out = run(conn, b)
    assert out.status is ExecStatus.PARTIALLY_FILLED and out.filled_qty == 1
    assert len(b.orders) == 1
    (os_,) = OpenStructureRepo(conn).list_open()
    assert os_["contracts"] == 1
    states = [r[0] for r in conn.execute("SELECT to_state FROM order_events ORDER BY id")]
    assert states[-2:] == ["partially_filled", "cancelled"]


def test_no_fill_cancels_after_last_attempt(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["new"]] * 4, cancel_scripts={k: [("canceled", 0)] for k in range(4)})
    clock = Clock()
    out = run(conn, b, clock=clock)
    assert out.status is ExecStatus.CANCELLED and out.filled_qty == 0
    assert [o.limit_price for o in b.orders] == list(BAND.ladder(D("0.01")))
    assert b.orders[-1].limit_price == BAND.hi
    assert "no fill after 4 attempt(s)" in out.detail
    assert clock.t - NOW >= dt.timedelta(seconds=4 * 60)
    assert OpenStructureRepo(conn).list_open() == []


def test_broker_rejects(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["rejected"]])
    out = run(conn, b)
    assert out.status is ExecStatus.REJECTED and len(b.orders) == 1
    assert ("rejected", "order:broker_rejected") in journal(conn)


def test_submit_error_is_unconfirmed(conn: sqlite3.Connection) -> None:
    class Boom(ScriptedBroker):
        def submit_mleg(self, order: MlegOrder) -> str:
            raise RuntimeError("503")

    out = run(conn, Boom([["new"]]))
    assert out.status is ExecStatus.UNCONFIRMED


def test_cancel_error_still_polls(conn: sqlite3.Connection) -> None:
    class CancelBoom(ScriptedBroker):
        def cancel(self, broker_order_id: str) -> None:
            super().cancel(broker_order_id)
            raise RuntimeError("order is not cancelable")

    b = CancelBoom([["new"]], cancel_scripts={0: [("filled", 2)]}, fills={0: (2, "-0.85")})
    assert run(conn, b).status is ExecStatus.FILLED


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------


def test_halt_mid_ladder_stops_next_attempt(conn: sqlite3.Connection) -> None:
    class HaltOnCancel(ScriptedBroker):
        def cancel(self, broker_order_id: str) -> None:
            super().cancel(broker_order_id)
            HaltSwitch(HaltRepo(conn)).halt(actor="U1", reason="vol", now=NOW)

    b = HaltOnCancel([["new"], ["new"]], cancel_scripts={0: [("canceled", 0)]})
    out = run(conn, b)
    assert out.status is ExecStatus.REJECTED
    assert len(b.orders) == 1, "the halt is re-read before every attempt"
    assert ("rejected", "order:refused") in journal(conn)


def test_idempotent_second_call_is_noop(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
    p = S.proposal(expires_at=NOW + dt.timedelta(minutes=20))
    run(conn, b, p=p)
    again = run(conn, b, p=p)
    assert again.status is ExecStatus.ALREADY and len(b.orders) == 1


def test_arc1_token_single_attempt(conn: sqlite3.Connection) -> None:
    p = S.proposal(expires_at=NOW + dt.timedelta(minutes=20))
    b = ScriptedBroker([["new"]], cancel_scripts={0: [("canceled", 0)]})
    out = run(conn, b, p=p, decision=gated(p, band=None))
    assert out.status is ExecStatus.CANCELLED and len(b.orders) == 1
    assert out.band.max_steps == 0
    assert b.orders[0].client_order_id.startswith("arc1.")


def test_invalid_token_refused_without_broker(conn: sqlite3.Connection) -> None:
    p = S.proposal(expires_at=NOW + dt.timedelta(minutes=20))
    bad = GateDecision(proposal_hash=proposal_hash(p), passed=True, token="garbage")
    b = ScriptedBroker([["new"]])
    out = run(conn, b, p=p, decision=bad)
    assert out.status is ExecStatus.REJECTED and b.orders == []
    row = conn.execute("SELECT client_order_id FROM orders").fetchone()
    assert row[0].startswith("invalid-token.")


# ---------------------------------------------------------------------------
# Closing an open structure
# ---------------------------------------------------------------------------


def test_close_reduces_structure_and_closes_lots(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
    opened = run(conn, b)
    assert opened.structure_id is not None
    close_p = S.proposal(
        thesis="exit",
        limit_price=D("0.40"),
        expires_at=NOW + dt.timedelta(minutes=20),
    )
    seed_close = ProposalRepo(conn)
    seed_close.insert(
        candidate_id=close_p.candidate_id,
        proposal_hash=proposal_hash(close_p),
        structure_json=close_p.structure.model_dump_json(),
        thesis=close_p.thesis,
        quant_json=close_p.quant.model_dump_json(),
        sizing_json=close_p.sizing.model_dump_json(),
        expires_at=close_p.expires_at.isoformat(),
        ticker="SPY",
        kind="close",
    )
    band = PriceBand(lo=D("0.40"), hi=D("0.40"), max_steps=0)
    b2 = ScriptedBroker([["filled"]], fills={0: (2, "0.40")})
    out = run(
        conn,
        b2,
        p=close_p,
        decision=gated(close_p, band=band),
        kind="close",
        structure_id=opened.structure_id,
    )
    assert out.status is ExecStatus.FILLED
    row = OpenStructureRepo(conn).get(opened.structure_id)
    assert row is not None and row["status"] == "closed" and D(row["close_net"]) == D("0.40")
    pnl = conn.execute(
        "SELECT payload FROM decisions WHERE reason_code = 'exit:closed'"
    ).fetchone()[0]
    assert json.loads(pnl)["realized_pnl"] == "90.00"  # (0.85 - 0.40) x 100 x 2
    open_lots = conn.execute("SELECT COUNT(*) FROM tax_lots WHERE closed_at IS NULL").fetchone()
    assert open_lots[0] == 0


def test_close_without_structure_id_raises(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
    with pytest.raises(ValueError, match="needs the structure"):
        run(conn, b, kind="close")


# ---------------------------------------------------------------------------
# fill price
# ---------------------------------------------------------------------------


def test_fill_net_price_from_legs() -> None:
    s = BrokerOrderStatus(
        broker_order_id="b",
        status="filled",
        filled_qty=D(2),
        legs=[
            {"side": "sell", "filled_qty": "2", "filled_avg_price": "2.05"},
            {"side": "buy", "filled_qty": "2", "filled_avg_price": "1.23"},
        ],
    )
    assert fill_net_price(s, D("-0.85")) == D("-0.82")


def test_fill_net_price_fallbacks() -> None:
    s = BrokerOrderStatus(broker_order_id="b", status="filled", filled_qty=D(1))
    assert fill_net_price(s, D("-0.85")) == D("-0.85")
    s2 = s.model_copy(update={"filled_avg_price": D("1.10")})
    assert fill_net_price(s2, D("0")) == D("1.10")


def test_attempt_rows_carry_unique_client_ids(conn: sqlite3.Connection) -> None:
    b = ScriptedBroker(
        [["new"], ["filled"]], cancel_scripts={0: [("canceled", 0)]}, fills={1: (2, "-0.82")}
    )
    out = run(conn, b)
    ids = [a.client_order_id for a in out.attempts]
    assert len(set(ids)) == 2
    for a in out.attempts:
        row = OrderRepo(conn).get(a.order_id)
        assert row is not None and row["client_order_id"] == a.client_order_id
        assert row["broker_order_id"] == a.broker_order_id
