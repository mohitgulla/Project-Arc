"""D101 (E24.1): re-gate on band move between ladder steps.

The ladder re-reads the legs' quotes before every step k >= 1. A fresh mid past
the band's worst price stops the walk; with a re-gate left the Broker job's
re-gate (here a scripted fake that runs the real token mint) returns a new band
and a new token, walked next under the same ``executions`` row, or refuses.

Baseline (tests/test_execution_ladder.py): SPY 570/565 bull put x2 credit, band
[-0.85, -0.76] with 3 steps -> -0.85 -0.82 -0.79 -0.76.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.control.registry import lookup
from arc.execution.ladder import (
    ExecStatus,
    ExecutionOutcome,
    MarketRead,
    Regated,
    RegateResult,
    reprice_note,
)
from arc.gate import issue_token, proposal_hash
from arc.gate.band import PriceBand
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import GateDecision
from arc.store.db import connect
from arc.store.execution import ExecutionRepo
from arc.store.migrate import migrate
from tests import test_execution_ladder as L

if TYPE_CHECKING:
    import sqlite3

    from arc.models import ApprovalRecord, Proposal

NEW_BAND = PriceBand(lo=D("-0.80"), hi=D("-0.72"), max_steps=2)  # -0.80 -0.76 -0.72


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def cfg(**kw: object) -> ArcSettings:
    return L.cfg(**kw)


class Quotes:
    """Scripted between-step quote reads: one mid per call (the last repeats)."""

    def __init__(self, *mids: str | None) -> None:
        self.mids = list(mids)
        self.calls = 0

    def __call__(self) -> MarketRead | None:
        self.calls += 1
        m = self.mids.pop(0) if len(self.mids) > 1 else self.mids[0]
        return None if m is None else MarketRead(mid=D(m), far=D(m) + D("0.10"), quote_age_s=1.0)


class Regate:
    """A scripted re-gate: re-prices the structure at *band* and mints a real arc2 token."""

    def __init__(self, *results: str | PriceBand, approval: ApprovalRecord | None = None) -> None:
        self.results = list(results)
        self.calls: list[tuple[PriceBand, MarketRead, int]] = []
        self.proposal: Proposal | None = None
        self.approval = approval

    def __call__(self, approved: PriceBand, read: MarketRead, n: int) -> RegateResult:
        self.calls.append((approved, read, n))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, str):
            if r == "boom":
                raise RuntimeError("market data down")
            payload: dict[str, Any] = {}
            if r == "gate_failed":
                payload = {"violations": ["spread_too_wide: leg spread 40% > 25%"]}
            if r == "ev":
                return RegateResult("gate_failed", "Net EV floor", payload={"ev_floor": "ev<0"})
            return RegateResult(r, f"scripted {r}", payload=payload)
        assert self.proposal is not None
        p = self.proposal.model_copy(update={"limit_price": r.lo})
        d = issue_token(
            GateDecision(proposal_hash=proposal_hash(p), passed=True),
            p,
            secret=L.SECRET.encode(),
            now=L.NOW,
            band=r,
        )
        appr = L.approved(self.proposal).model_copy(update={"proposal_hash": proposal_hash(p)})
        return RegateResult(
            "passed", "ok", regated=Regated(proposal=p, decision=d, approval=appr, band=r)
        )


def run(conn: sqlite3.Connection, broker: Any, quotes: Any, regate: Any, **kw: Any) -> Any:
    p = L.S.proposal(expires_at=L.NOW + dt.timedelta(minutes=20))
    if isinstance(regate, Regate):
        regate.proposal = p
    kw.setdefault("config", cfg())
    return L.run(conn, broker, p=p, quotes=quotes, regate=regate, **kw), p


def codes(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return L.journal(conn)


def payloads(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM decisions WHERE reason_code = ? ORDER BY rowid", (code,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def never(*_: Any) -> Any:
    raise AssertionError("must not be called")


# ---------------------------------------------------------------------------
# Settings / registry / reason code
# ---------------------------------------------------------------------------


class TestKnobs:
    def test_defaults(self) -> None:
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.execution_regate_max == 1
        assert s.execution_regate_max_move_pct == pytest.approx(0.05)

    @pytest.mark.parametrize(
        ("field", "bad"),
        [("execution_regate_max", -1), ("execution_regate_max", 4),
         ("execution_regate_max_move_pct", -0.01), ("execution_regate_max_move_pct", 0.21)],
    )  # fmt: skip
    def test_bounds(self, field: str, bad: object) -> None:
        with pytest.raises(ValueError, match=field):
            ArcSettings(_env_file=None, **{field: bad})  # type: ignore[arg-type]

    def test_registry_keys(self) -> None:
        assert lookup("execution.regate_max").hard_ceiling == 3
        assert lookup("execution.regate_max_move_pct").hard_ceiling == pytest.approx(0.20)

    def test_reason_label(self) -> None:
        assert ReasonCode.ORDER_BAND_MOVED.value == "order:band_moved"
        assert REASON_LABELS[ReasonCode.ORDER_BAND_MOVED]


# ---------------------------------------------------------------------------
# Ladder behaviour
# ---------------------------------------------------------------------------


def _three_misses_then(fill_step: int | None = None, price: str = "-0.76") -> L.ScriptedBroker:
    n = 6
    scripts = [["new"] for _ in range(n)]
    fills: dict[int, tuple[int, str]] = {}
    if fill_step is not None:
        scripts[fill_step] = ["new", "filled"]
        fills[fill_step] = (2, price)
    cancels = {k: [("canceled", 0)] for k in range(n)}
    return L.ScriptedBroker(scripts, cancel_scripts=cancels, fills=fills)


class TestInBand:
    def test_quote_in_band_walks_as_approved(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then(2, "-0.79")
        q = Quotes("-0.84", "-0.80")
        out, _ = run(conn, b, q, never)
        assert out.status is ExecStatus.FILLED and out.steps_used == 2
        assert [o.limit_price for o in b.orders] == [D("-0.85"), D("-0.82"), D("-0.79")]
        assert q.calls == 2, "checked before steps 1 and 2, never before step 0"
        assert out.regated == [] and "re-priced" not in out.summary()
        assert ("selected", "order:band_moved") not in codes(conn)

    def test_unreadable_quote_walks_on(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then(1, "-0.82")
        out, _ = run(conn, b, Quotes(None), never)
        assert out.status is ExecStatus.FILLED and out.steps_used == 1

    def test_quote_error_walks_on(self, conn: sqlite3.Connection) -> None:
        def boom() -> MarketRead:
            raise RuntimeError("quote feed down")

        b = _three_misses_then(1, "-0.82")
        out, _ = run(conn, b, boom, never)
        assert out.status is ExecStatus.FILLED

    def test_regate_max_zero_is_old_behaviour(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        q = Quotes("-0.50")
        out, _ = run(conn, b, q, never, config=cfg(execution_regate_max=0))
        assert q.calls == 0
        assert len(b.orders) == 4 and out.status is ExecStatus.CANCELLED
        assert out.detail.startswith("no fill after 4 attempt(s)")

    def test_without_quote_reader_is_old_behaviour(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        out, _ = run(conn, b, None, never)
        assert len(b.orders) == 4


class TestRegatePasses:
    def test_walks_new_band_under_new_token(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then(3, "-0.76")
        rg = Regate(NEW_BAND)
        out, p = run(conn, b, Quotes("-0.84", "-0.70", "-0.79"), rg)
        assert out.status is ExecStatus.FILLED
        prices = [o.limit_price for o in b.orders]
        # step 0 and 1 of the approved band, then the re-gated band from its start
        assert prices == [D("-0.85"), D("-0.82"), D("-0.80"), D("-0.76")]
        assert [a.regate for a in out.attempts] == [0, 0, 1, 1]
        old_tok = b.orders[0].client_order_id.rsplit(".", 1)[0]
        new_tok = b.orders[2].client_order_id.rsplit(".", 1)[0]
        assert old_tok != new_tok, "a re-gated band is sent under a new token"
        assert b.orders[2].client_order_id.endswith(".s0")
        assert out.regated == [NEW_BAND] and len(rg.calls) == 1
        approved, read, n = rg.calls[0]
        assert approved == L.BAND and read.mid == D("-0.70") and n == 1
        # the record stays on the approved proposal
        ex = ExecutionRepo(conn).get(proposal_hash(p))
        assert ex is not None and ex["status"] == "filled"
        assert D(ex["band_lo"]) == L.BAND.lo and D(ex["band_hi"]) == L.BAND.hi
        assert ex["attempts"] == 4 and ex["max_steps"] == 2 + NEW_BAND.max_steps
        assert "re-priced once: 0.85–0.76 → 0.80–0.72, filled 0.76" in ex["detail"]
        rows = conn.execute("SELECT proposal_hash FROM orders").fetchall()
        assert {r[0] for r in rows} == {proposal_hash(p)}
        j = codes(conn)
        assert ("selected", "order:band_moved") in j and ("passed", "gate:pass") in j
        (moved,) = payloads(conn, "order:band_moved")
        assert moved["regate"] == "passed" and moved["unsent"] == 2 and moved["step"] == 2
        assert moved["new_band_lo"] == "-0.80" and moved["mid"] == "-0.70"
        (filled,) = payloads(conn, "order:filled")
        assert filled["band_hi"] == "-0.72" and filled["approved_band_hi"] == "-0.76"
        assert filled["regates"] == 1

    def test_all_new_prices_inside_new_band(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        out, _ = run(conn, b, Quotes("-0.70", "-0.79"), Regate(NEW_BAND))
        assert [a.regate for a in out.attempts] == [0, 1, 1, 1]
        assert all(L.BAND.contains(o.limit_price) for o in b.orders[:1])
        assert all(NEW_BAND.contains(o.limit_price) for o in b.orders[1:])
        assert out.status is ExecStatus.CANCELLED and "re-priced once" in out.summary()
        assert reprice_note(out).endswith(", missed")

    def test_second_move_with_none_left_stops(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        rg = Regate(NEW_BAND)
        out, _ = run(conn, b, Quotes("-0.70", "-0.60"), rg)
        assert len(rg.calls) == 1
        assert [a.regate for a in out.attempts] == [0, 1]
        assert out.status is ExecStatus.CANCELLED and "no re-gate left" in out.detail
        assert [p["regate"] for p in payloads(conn, "order:band_moved")] == ["passed", "none_left"]

    def test_regate_max_two_regates_twice(self, conn: sqlite3.Connection) -> None:
        third = PriceBand(lo=D("-0.70"), hi=D("-0.66"), max_steps=1)
        b = _three_misses_then(3, "-0.66")
        out, _ = run(
            conn,
            b,
            Quotes("-0.70", "-0.60", "-0.69"),
            Regate(NEW_BAND, third),
            config=cfg(execution_regate_max=2),
        )
        assert out.status is ExecStatus.FILLED and out.regated == [NEW_BAND, third]
        assert "re-priced twice: 0.85–0.76 → 0.80–0.72 → 0.70–0.66, filled 0.66" in out.summary()


class TestRegateRefused:
    @pytest.mark.parametrize("status", ["refused_move", "gate_failed", "ttl_expired", "no_quote"])
    def test_refusal_stops_without_sending(self, conn: sqlite3.Connection, status: str) -> None:
        b = _three_misses_then()
        out, p = run(conn, b, Quotes("-0.70"), Regate(status))
        assert len(b.orders) == 1, "nothing is sent after the band moved"
        assert out.status is ExecStatus.CANCELLED
        assert f"re-gate {status}" in out.detail and "market moved past the band" in out.detail
        ex = ExecutionRepo(conn).get(proposal_hash(p))
        assert ex is not None and ex["status"] == "cancelled" and ex["attempts"] == 1
        (moved,) = payloads(conn, "order:band_moved")
        assert moved["regate"] == status and moved["unsent"] == 3
        assert ("no_trade", "order:band_moved") in codes(conn)

    def test_gate_violation_journalled_as_gate_row(self, conn: sqlite3.Connection) -> None:
        run(conn, _three_misses_then(), Quotes("-0.70"), Regate("gate_failed"))
        rows = conn.execute(
            "SELECT persona, stage, reason_code FROM decisions WHERE choice = 'failed'"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("gate", "gate", "gate:spread_too_wide")]

    def test_ev_floor_journalled(self, conn: sqlite3.Connection) -> None:
        run(conn, _three_misses_then(), Quotes("-0.70"), Regate("ev"))
        assert ("failed", "net_ev_floor") in codes(conn)

    def test_regate_exception_fails_closed(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        out, _ = run(conn, b, Quotes("-0.70"), Regate("boom"))
        assert len(b.orders) == 1 and out.status is ExecStatus.CANCELLED
        assert "re-gate error: re-gate failed: RuntimeError: market data down" in out.detail

    def test_old_token_cannot_send_new_band(self, conn: sqlite3.Connection) -> None:
        """A 'passed' re-gate that kept the old token never sends (fails closed)."""

        class Stale(Regate):
            def __call__(self, approved: PriceBand, read: MarketRead, n: int) -> RegateResult:
                res = super().__call__(approved, read, n)
                assert res.regated is not None and self.proposal is not None
                old = L.gated(self.proposal)
                return RegateResult(
                    "passed",
                    "stale token",
                    regated=Regated(
                        proposal=self.proposal,
                        decision=old,
                        approval=L.approved(self.proposal),
                        band=res.regated.band,
                    ),
                )

        b = _three_misses_then()
        out, _ = run(conn, b, Quotes("-0.70"), Stale(NEW_BAND))
        assert len(b.orders) == 1
        assert out.status is ExecStatus.CANCELLED and "no new token" in out.detail


def test_reprice_note_empty_without_regate() -> None:
    out = ExecutionOutcome("h", ExecStatus.FILLED, L.BAND)
    assert reprice_note(out) == ""


def test_headline_reprice_parse() -> None:
    from arc.routines.headline import _reprice

    assert _reprice("filled: 3 attempt(s); re-priced once: 4.65–4.75 → 4.82–4.95, filled 4.88") == (
        "re-priced once: 4.65–4.75 → 4.82–4.95, filled 4.88"
    )
    assert _reprice("x; re-priced twice: 1.00–1.10 → 1.20–1.30 → 1.40–1.50, missed (y)") == (
        "re-priced twice: 1.00–1.10 → 1.20–1.30 → 1.40–1.50, missed"
    )
    assert _reprice(None) == "" and _reprice("no fill after 4 attempt(s)") == ""


def test_store_regate_extends_reservation(conn: sqlite3.Connection) -> None:
    repo = ExecutionRepo(conn)
    p = L.S.proposal(expires_at=L.NOW + dt.timedelta(minutes=20))
    L.seed(conn, p)
    h = proposal_hash(p)
    assert repo.start(
        proposal_hash=h, kind="open", token_version="arc2", band_lo=L.BAND.lo,
        band_hi=L.BAND.hi, max_steps=3, contracts=2, now=L.NOW, structure_id=None, run_id="r",
    )  # fmt: skip
    repo.attempt(h)
    repo.attempt(h)
    repo.regate(h, max_steps=2)
    row = repo.get(h)
    assert row is not None and row["max_steps"] == 4 and row["attempts"] == 2


class TestSafety:
    def test_halt_during_regate_sends_nothing(self, conn: sqlite3.Connection) -> None:
        from arc.store.repos import HaltRepo

        class HaltingRegate(Regate):
            def __call__(self, approved: PriceBand, read: MarketRead, n: int) -> RegateResult:
                HaltRepo(conn).halt(reason="owner halt", actor="test")
                return super().__call__(approved, read, n)

        b = _three_misses_then()
        out, _ = run(conn, b, Quotes("-0.70"), HaltingRegate(NEW_BAND))
        assert len(b.orders) == 1, "the halt check at submit refuses the re-gated band"
        assert out.status is ExecStatus.REJECTED
        assert ("rejected", "order:refused") in codes(conn)

    def test_budget_exhausted_after_regate_stops(self, conn: sqlite3.Connection) -> None:
        b = _three_misses_then()
        out, _ = run(
            conn,
            b,
            Quotes("-0.70", "-0.79"),
            Regate(NEW_BAND),
            config=cfg(
                order_budget_daily_max=2, order_budget_close_reserve=0, order_budget_restrict_at=2
            ),
        )
        assert len(b.orders) == 2, "the re-gated attempts count against D32 like any other"
        assert out.status is ExecStatus.CANCELLED and "order budget exhausted" in out.detail
        assert ("no_trade", "order:budget_exhausted") in codes(conn)


def _orders_golden(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT client_order_id, state FROM orders ORDER BY created_at, rowid"
        ).fetchall()
    ]


@pytest.mark.parametrize("quotes", ["in_band", "off"])
def test_identical_orders_to_main(quotes: str) -> None:
    """In-band quotes (or regate_max 0) leave the orders sequence exactly as before D101."""

    def one(**kw: Any) -> tuple[list[tuple[Any, ...]], list[D]]:
        c = connect(":memory:")
        migrate(c)
        b = _three_misses_then(3, "-0.76")
        L.run(c, b, p=L.S.proposal(expires_at=L.NOW + dt.timedelta(minutes=20)), **kw)
        return _orders_golden(c), [o.limit_price for o in b.orders]

    main = one()
    if quotes == "in_band":
        got = one(quotes=Quotes("-0.84"), regate=never)
    else:
        got = one(quotes=Quotes("-0.50"), regate=never, config=cfg(execution_regate_max=0))
    assert got == main
