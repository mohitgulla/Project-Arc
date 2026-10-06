"""E6.3 reconciliation: broker (fake, read-only) vs the local store.

Scenario: one SPY 711/710 Oct-30 bull put (2 contracts) opened today through an
mleg order; the broker holds the two legs and reports their per-leg fills.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from arc.broker.base import AccountInfo, BrokerOrderStatus, BrokerPosition, Fill
from arc.broker.reconcile_job import broker_reconcile, reconcile_output
from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import to_db
from arc.gate import HaltSwitch
from arc.journal.reasons import ReasonCode
from arc.models import LegIntent, OrderState, Structure
from arc.pipeline.market import _group_legs
from arc.reconcile.attribution import BrokerLeg, StructureHolding, attribute, broker_legs
from arc.reconcile.engine import RECONCILE_ACTOR, MismatchKind, reconcile
from arc.reconcile.performance import DailyEquity, performance, performance_from
from arc.routines.config import RoutinesConfig
from arc.routines.handlers import JobContext
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import (
    CandidateRepo,
    FillRepo,
    HaltRepo,
    OrderRepo,
    PnlSnapshotRepo,
    ProposalRepo,
    TaxLotRepo,
)
from arc.structures import credit_vertical, parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 9, 28, 16, 30, tzinfo=ET)
OPENED = dt.datetime(2026, 9, 28, 10, 0, tzinfo=ET)
EXP = dt.date(2026, 10, 30)
BID = "brk-open-1"


def settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "gate_secret": "g" * 40, "env": "paper"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def bull_put(exp: dt.date = EXP, root: str = "SPY") -> Structure:
    return credit_vertical(
        "put", root, exp, short_strike=711, short_premium="5.60",
        long_strike=710, long_premium="4.70", as_of=dt.date(2026, 9, 25),
    )  # fmt: skip


def legs_of(st: Structure) -> tuple[str, str]:
    short = next(leg for leg in st.legs if leg.side == LegIntent.SHORT)
    long = next(leg for leg in st.legs if leg.side == LegIntent.LONG)
    return parse_occ(short.occ_symbol).format(), parse_occ(long.occ_symbol).format()


def open_position(
    conn: sqlite3.Connection,
    *,
    phash: str = "a" * 64,
    st: Structure | None = None,
    n: int = 2,
    entry: str = "-0.90",
    opened: dt.datetime = OPENED,
    bid: str | None = BID,
    fill: bool = True,
) -> dict[str, Any]:
    """Proposal + filled order + fill + open structure + lots, like the E6.2 ladder."""
    st = st or bull_put()
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7
    )
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash=phash, structure_json=st.model_dump_json(),
        thesis="t", quant_json="{}", sizing_json="{}", expires_at=to_db(opened),
        ticker="SPY",
    )  # fmt: skip
    orders = OrderRepo(conn)
    oid = orders.create(proposal_hash=phash, client_order_id=f"coid-{phash[:6]}")
    for s in (OrderState.GATED, OrderState.APPROVED, OrderState.SUBMITTED):
        orders.transition(order_id=oid, to_state=s, actor="t", event_at=to_db(opened))
    conn.execute("UPDATE orders SET broker_order_id = ? WHERE id = ?", (bid, oid))
    if fill:
        orders.transition(order_id=oid, to_state=OrderState.FILLED, actor="t",
                          event_at=to_db(opened + dt.timedelta(seconds=1)))  # fmt: skip
        FillRepo(conn).insert(order_id=oid, qty=n, price=entry, filled_at=to_db(opened),
                              broker_fill_id=bid)  # fmt: skip
    conn.commit()
    sid = OpenStructureRepo(conn).open(
        ticker="SPY", open_proposal_hash=phash, candidate_id=cid,
        structure_json=st.model_dump_json(), contracts=n, entry_net=D(entry), now=opened,
    )  # fmt: skip
    lots = TaxLotRepo(conn)
    lot_ids = [
        lots.open_lot(
            order_id=oid,
            ticker="SPY",
            occ_symbol=leg.occ_symbol,
            side=str(leg.side),
            qty=leg.ratio * n,
            open_price=str(leg.premium),
            opened_at=to_db(opened),
        )  # fmt: skip
        for leg in st.legs
    ]
    return {"sid": sid, "oid": oid, "phash": phash, "lots": lot_ids, "st": st}


class FakeBroker:
    """Read-only broker double; submit/cancel would fail the test."""

    def __init__(
        self,
        *,
        positions: list[BrokerPosition] | None = None,
        fills: list[Fill] | None = None,
        statuses: dict[str, str] | None = None,
        equity: str = "100500",
        last_equity: str | None = "100000",
        fail: set[str] | None = None,
    ) -> None:
        self._positions = positions or []
        self._fills = fills or []
        self._statuses = statuses or {}
        self._equity, self._last = equity, last_equity
        self._fail = fail or set()

    def _check(self, name: str) -> None:
        if name in self._fail:
            msg = f"{name} down"
            raise ConnectionError(msg)

    def account(self) -> AccountInfo:
        self._check("account")
        return AccountInfo(
            account_id="PA-test", equity=D(self._equity), buying_power=D(0), cash=D(0),
            last_equity=D(self._last) if self._last else None,
        )  # fmt: skip

    def positions(self) -> list[BrokerPosition]:
        self._check("positions")
        return self._positions

    def fills(self, since: dt.datetime) -> list[Fill]:
        self._check("fills")
        return [f for f in self._fills if f.filled_at >= since]

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        self._check("order_status")
        return BrokerOrderStatus(
            broker_order_id=broker_order_id, status=self._statuses.get(broker_order_id, "new")
        )


def held_positions(st: Structure, n: int = 2) -> list[BrokerPosition]:
    short, long = legs_of(st)
    return [
        BrokerPosition(symbol=short, qty=D(-n), side="short", avg_entry_price=D("5.50"),
                       unrealized_pl=D("-20")),
        BrokerPosition(symbol=long, qty=D(n), side="long", avg_entry_price=D("4.62"),
                       unrealized_pl=D("5")),
    ]  # fmt: skip


def open_fills(st: Structure, n: int = 2, bid: str = BID) -> list[Fill]:
    short, long = legs_of(st)
    return [
        Fill(broker_order_id=bid, symbol=short, side="sell", qty=D(n), price=D("5.50"),
             filled_at=OPENED),
        Fill(broker_order_id=bid, symbol=long, side="buy", qty=D(n), price=D("4.62"),
             filled_at=OPENED),
    ]  # fmt: skip


def run(conn: sqlite3.Connection, broker: FakeBroker, **kw: Any):
    return reconcile(conn, broker, settings=settings(), now=NOW, run_id="r", **kw)  # type: ignore[arg-type]


def reasons(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT reason_code FROM decisions ORDER BY rowid")]


# ---------------------------------------------------------------------------
# attribution (pure)
# ---------------------------------------------------------------------------


def test_attribute_oldest_first_and_leftovers() -> None:
    a = StructureHolding(structure_id="a", ticker="X", legs={"S1": -1, "L1": 1})
    b = StructureHolding(structure_id="b", ticker="X", legs={"S1": -2, "L1": 2})
    legs = [BrokerLeg("S1", -2), BrokerLeg("L1", 2), BrokerLeg("Z", 3)]
    att = attribute([a, b], legs)
    assert [h.structure_id for h in att.attributed] == ["a"]
    assert [h.structure_id for h in att.unmatched] == ["b"]
    assert att.unattributed == {"S1": -1, "L1": 1, "Z": 3}


def test_attribute_wrong_sign_is_unmatched() -> None:
    a = StructureHolding(structure_id="a", ticker="X", legs={"S1": -1})
    att = attribute([a], [BrokerLeg("S1", 1)])
    assert att.unmatched == [a] and att.unattributed == {"S1": 1}


def test_broker_legs_rejects_fractional_and_splits_non_options() -> None:
    st = bull_put()
    short, _ = legs_of(st)
    legs, other = broker_legs(
        [
            BrokerPosition(symbol=short, qty=D(-1), side="short"),
            BrokerPosition(symbol="SPY", qty=D(100), side="long", asset_class="us_equity"),
            BrokerPosition(symbol=short, qty=D(0), side="long"),
        ]
    )
    assert legs == [BrokerLeg(short, -1, None)] and other == ["SPY"]
    with pytest.raises(ValueError, match="non-integral"):
        broker_legs([BrokerPosition(symbol=short, qty=D("1.5"), side="long")])


def test_group_legs_values_two_structures_same_expiry_separately(
    conn: sqlite3.Connection,
) -> None:
    """Sentinel S-5: two structures on one root+expiry are two valuation groups."""
    st = bull_put()
    a = open_position(conn)
    b = open_position(conn, phash="b" * 64, n=1, bid="brk-2", opened=OPENED + dt.timedelta(hours=1))
    groups = _group_legs(conn, held_positions(st, 3))
    assert [(root, label) for root, label, _ in groups] == [("SPY", a["sid"]), ("SPY", b["sid"])]
    assert [sum(leg.ratio for leg in legs) for _, _, legs in groups] == [4, 2]
    # without local structures: one group per (root, expiration)
    (only,) = _group_legs(None, held_positions(st, 3))
    assert only[1] == f"SPY {EXP.isoformat()}"


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def test_clean_run_writes_snapshots_and_reprices_lots(conn: sqlite3.Connection) -> None:
    pos = open_position(conn)
    st = pos["st"]
    rep = run(conn, FakeBroker(positions=held_positions(st), fills=open_fills(st)))
    assert rep.clean, rep.mismatches
    assert (rep.structures_open, rep.structures_attributed) == (1, 1)
    assert (rep.fills_local, rep.fills_broker) == (1, 1)
    assert rep.day_pnl == D(500) and rep.unrealized == D(-15)
    assert not rep.halted and not HaltSwitch(HaltRepo(conn)).is_halted()
    # lots now carry broker fill prices, not the proposal-time mids
    prices = {
        parse_occ(r["occ_symbol"]).format(): r["open_price"]
        for r in TaxLotRepo(conn).with_structure()
    }
    short, long = legs_of(st)
    assert prices == {short: "5.5000", long: "4.6200"} and rep.lots_repriced == 2
    # snapshots
    (pnl,) = PnlSnapshotRepo(conn).daily()
    details = json.loads(pnl["details_json"])
    assert details["day"] == "2026-09-28" and details["equity"] == "100500" and details["clean"]
    snap = json.loads(conn.execute("SELECT positions_json FROM positions_snapshots").fetchone()[0])
    assert snap["structures"][0]["held"] is True and snap["unattributed"] == {}
    assert ReasonCode.RECONCILE_CLEAN.value in reasons(conn)
    assert ReasonCode.RECONCILE_LOT_PRICE.value in reasons(conn)
    # idempotent: a second run changes no lot price
    rep2 = run(conn, FakeBroker(positions=held_positions(st), fills=open_fills(st)))
    assert rep2.clean and rep2.lots_repriced == 0


def test_missing_position_and_unknown_fill_halt(conn: sqlite3.Connection) -> None:
    pos = open_position(conn)
    st = pos["st"]
    stray = Fill(broker_order_id="brk-x", symbol="QQQ261030C00500000", side="buy", qty=D(1),
                 price=D(1), filled_at=OPENED)  # fmt: skip
    broker = FakeBroker(
        positions=[
            BrokerPosition(symbol="QQQ261030C00500000", qty=D(1), side="long"),
            BrokerPosition(symbol="SPY", qty=D(100), side="long", asset_class="us_equity"),
        ],
        fills=[*open_fills(st), stray],
    )
    rep = run(conn, broker)
    kinds = sorted(str(m.kind) for m in rep.mismatches)
    assert kinds == sorted(
        [
            MismatchKind.POSITION_MISSING,
            MismatchKind.POSITION_UNATTRIBUTED,
            MismatchKind.POSITION_NON_OPTION,
            MismatchKind.FILL_UNKNOWN,
        ]
    )
    assert rep.halted and rep.halt_id
    (h,) = HaltSwitch(HaltRepo(conn)).state().active
    assert h.actor == RECONCILE_ACTOR and "4 mismatch" in h.reason
    # a re-run does not stack a second halt
    rep2 = run(conn, broker)
    assert rep2.halt_id == rep.halt_id
    assert len(HaltSwitch(HaltRepo(conn)).state().active) == 1
    assert ReasonCode.RECONCILE_MISMATCH.value in reasons(conn)


def test_adapter_built_option_positions_are_not_non_option(conn: sqlite3.Connection) -> None:
    """E6.3a: positions parsed by the Alpaca adapter from real alpaca-py enums reconcile clean.

    Before the fix ``str(AssetClass.US_OPTION)`` leaked ``"AssetClass.US_OPTION"`` and every
    held option leg became ``position_non_option``.
    """
    from alpaca.trading.enums import AssetClass, AssetExchange, PositionSide
    from alpaca.trading.models import Position

    from arc.broker.alpaca_paper import AlpacaPaperBroker

    pos = open_position(conn)
    st = pos["st"]
    short, long = legs_of(st)
    raw = [
        Position(asset_id=uuid.uuid4(), symbol=short, exchange=AssetExchange.EMPTY,
                 asset_class=AssetClass.US_OPTION, avg_entry_price="5.50", qty="-2",
                 side=PositionSide.SHORT, cost_basis="-1100", unrealized_pl="-20"),
        Position(asset_id=uuid.uuid4(), symbol=long, exchange=AssetExchange.EMPTY,
                 asset_class=AssetClass.US_OPTION, avg_entry_price="4.62", qty="2",
                 side=PositionSide.LONG, cost_basis="924", unrealized_pl="5"),
    ]  # fmt: skip
    client = MagicMock()
    client.get_all_positions.return_value = raw
    with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
        adapter_positions = AlpacaPaperBroker(client=client).positions()

    rep = run(conn, FakeBroker(positions=adapter_positions, fills=open_fills(st)))
    assert MismatchKind.POSITION_NON_OPTION not in {m.kind for m in rep.mismatches}
    assert rep.clean, rep.mismatches
    assert rep.structures_attributed == 1 and rep.unrealized == D(-15)

    # an equity position from the adapter is still flagged (fail-closed unchanged)
    equity = Position(asset_id=uuid.uuid4(), symbol="SPY", exchange=AssetExchange.ARCA,
                      asset_class=AssetClass.US_EQUITY, avg_entry_price="500", qty="100",
                      side=PositionSide.LONG, cost_basis="50000")  # fmt: skip
    client.get_all_positions.return_value = [*raw, equity]
    with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
        adapter_positions = AlpacaPaperBroker(client=client).positions()
    rep = run(conn, FakeBroker(positions=adapter_positions, fills=open_fills(st)), halt=False)
    assert [m.kind for m in rep.mismatches] == [MismatchKind.POSITION_NON_OPTION]


def test_fill_missing_and_qty(conn: sqlite3.Connection) -> None:
    pos = open_position(conn)
    st = pos["st"]
    rep = run(conn, FakeBroker(positions=held_positions(st)), halt=False)
    assert [m.kind for m in rep.mismatches] == [MismatchKind.FILL_MISSING]
    assert not rep.halted
    rep = run(conn, FakeBroker(positions=held_positions(st), fills=open_fills(st, n=1)),
              halt=False)  # fmt: skip
    assert [m.kind for m in rep.mismatches] == [MismatchKind.FILL_QTY]


def test_broker_error_fails_closed(conn: sqlite3.Connection) -> None:
    rep = run(conn, FakeBroker(fail={"account", "positions", "fills"}))
    assert {m.subject for m in rep.mismatches} == {"account", "positions", "fills"}
    assert rep.halted and rep.day_pnl is None


def _test_round_trip(coid: str | None) -> list[Fill]:
    """An integration test's open + close of a QQQ vertical (no local record, flat after)."""
    long, short = "QQQ261030C00500000", "QQQ261030C00505000"
    out = []
    for bid, (ls, ss) in (("brk-t-open", ("buy", "sell")), ("brk-t-close", ("sell", "buy"))):
        suffix = bid.rsplit("-", 1)[1]
        c = None if coid is None else f"{coid}arc2.tok.{suffix}.s0"
        out += [
            Fill(broker_order_id=bid, symbol=long, side=ls, qty=D(1), price=D("2.10"),
                 filled_at=OPENED, client_order_id=c),
            Fill(broker_order_id=bid, symbol=short, side=ss, qty=D(1), price=D("1.00"),
                 filled_at=OPENED, client_order_id=c),
        ]  # fmt: skip
    return out


def test_test_prefixed_fill_is_fill_test_and_does_not_halt(conn: sqlite3.Connection) -> None:
    """E6.2c: an integration-test fill on the production account reconciles clean."""
    pos = open_position(conn)
    st = pos["st"]
    broker = FakeBroker(
        positions=held_positions(st), fills=[*open_fills(st), *_test_round_trip("test.")]
    )
    rep = run(conn, broker)
    assert rep.clean, rep.mismatches
    assert MismatchKind.FILL_UNKNOWN not in {m.kind for m in rep.mismatches}
    assert [m.kind for m in rep.notices] == [MismatchKind.FILL_TEST] * 2
    assert {m.refs[0] for m in rep.notices} == {"brk-t-open", "brk-t-close"}
    assert all(m.refs[1].startswith("test.") for m in rep.notices)
    assert not rep.halted and rep.halt_id is None
    assert HaltSwitch(HaltRepo(conn)).state().active == []
    assert "2 test fill(s) ignored" in rep.summary()
    codes = reasons(conn)
    assert ReasonCode.RECONCILE_CLEAN.value in codes
    assert codes.count(ReasonCode.RECONCILE_TEST_FILL.value) == 2
    assert ReasonCode.RECONCILE_MISMATCH.value not in codes
    out = reconcile_output(rep)
    assert out.reconciliation_status == "clean"
    assert [a.severity for a in out.anomalies] == ["info", "info"]


def test_unprefixed_stray_fill_still_halts(conn: sqlite3.Connection) -> None:
    """Only the ``test.`` prefix is exempt: an arc2/untagged stray is fill_unknown."""
    for coid in (None, "arc2."):
        c = connect(":memory:")
        migrate(c)
        rep = run(c, FakeBroker(fills=_test_round_trip(coid)))
        assert [m.kind for m in rep.mismatches] == [MismatchKind.FILL_UNKNOWN] * 2
        assert rep.halted and not rep.notices


def test_test_fill_leaving_a_leg_open_still_halts(conn: sqlite3.Connection) -> None:
    """A test order's leg still held is Arc's buying power at risk: position_unattributed."""
    fills = _test_round_trip("test.")[:2]  # opened, never closed
    held = [BrokerPosition(symbol="QQQ261030C00500000", qty=D(1), side="long"),
            BrokerPosition(symbol="QQQ261030C00505000", qty=D(-1), side="short")]  # fmt: skip
    rep = run(conn, FakeBroker(positions=held, fills=fills))
    assert [m.kind for m in rep.notices] == [MismatchKind.FILL_TEST]
    assert {m.kind for m in rep.mismatches} == {MismatchKind.POSITION_UNATTRIBUTED}
    assert rep.halted


def test_adapter_fills_carry_client_order_id() -> None:
    """The Alpaca adapter passes the order's client_order_id onto every leg fill."""
    import datetime as _d

    at = _d.datetime(2026, 9, 28, 14, tzinfo=_d.UTC)
    leg = MagicMock(symbol="QQQ261030C00500000", side="buy", filled_qty="1",
                    filled_avg_price="2.1", filled_at=at)  # fmt: skip
    order = MagicMock(id="brk-1", client_order_id="test.arc2.x.s0", legs=[leg],
                      filled_at=leg.filled_at)  # fmt: skip
    client = MagicMock()
    client.get_orders.return_value = [order]
    from arc.broker.alpaca_paper import AlpacaPaperBroker

    with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
        (f,) = AlpacaPaperBroker(client=client).fills(OPENED)
    assert f.client_order_id == "test.arc2.x.s0" and f.broker_order_id == "brk-1"


def test_open_order_resolved_when_broker_cancelled_without_fill(
    conn: sqlite3.Connection,
) -> None:
    pos = open_position(conn, phash="c" * 64, bid="brk-c", fill=False)
    ExecutionRepo(conn).start(
        proposal_hash=pos["phash"], kind="open", token_version="arc2", band_lo=D("-0.9"),
        band_hi=D("-0.8"), max_steps=3, contracts=2, now=OPENED,
    )  # fmt: skip
    conn.execute("UPDATE executions SET status = 'unconfirmed' WHERE proposal_hash = ?",
                 (pos["phash"],))  # fmt: skip
    conn.execute("UPDATE open_structures SET status = 'closed'")  # never opened
    conn.commit()
    rep = run(conn, FakeBroker(statuses={"brk-c": "canceled"}))
    assert rep.clean, rep.mismatches
    assert OrderRepo(conn).get(pos["oid"])["state"] == "cancelled"
    assert ExecutionRepo(conn).get(pos["phash"])["status"] == "cancelled"
    assert ReasonCode.RECONCILE_RESOLVED.value in reasons(conn)


def test_open_order_still_working_is_mismatch(conn: sqlite3.Connection) -> None:
    open_position(conn, phash="c" * 64, bid="brk-c", fill=False)
    conn.execute("UPDATE open_structures SET status = 'closed'")
    conn.commit()
    rep = run(conn, FakeBroker(statuses={"brk-c": "new"}), halt=False)
    assert [m.kind for m in rep.mismatches] == [MismatchKind.ORDER_OPEN]
    assert "broker says new" in rep.mismatches[0].detail


def test_expired_structure_settles_at_intrinsic(conn: sqlite3.Connection) -> None:
    exp = dt.date(2026, 9, 25)
    pos = open_position(conn, st=bull_put(exp), opened=OPENED - dt.timedelta(days=10),
                        entry="-0.90")  # fmt: skip
    # SPY settled at 710.50: short 711 put is 0.50 ITM, long 710 put OTM -> close debit 0.50
    rep = run(conn, FakeBroker(), settle_price=lambda root, day: D("710.50"))
    assert rep.clean, rep.mismatches
    assert rep.expired == [pos["sid"]]
    row = OpenStructureRepo(conn).get(pos["sid"])
    assert row["status"] == "closed" and D(row["close_net"]) == D("0.50")
    lots = TaxLotRepo(conn).with_structure()
    assert all(lot["closed_at"] for lot in lots)
    assert sum(D(lot["realized_pnl"]) for lot in lots) == D("80")  # (0.90 - 0.50) x 100 x 2
    assert rep.realized == D("80")
    assert ReasonCode.RECONCILE_EXPIRED.value in reasons(conn)


def test_expired_without_settle_price_is_mismatch(conn: sqlite3.Connection) -> None:
    open_position(conn, st=bull_put(dt.date(2026, 9, 25)), opened=OPENED - dt.timedelta(days=10))
    rep = run(conn, FakeBroker(), halt=False)
    (m,) = rep.mismatches
    assert m.kind is MismatchKind.POSITION_MISSING and "not settled" in m.detail


def test_wash_sale_marks_loss_lot_with_replacement(conn: sqlite3.Connection) -> None:
    old = open_position(conn, phash="d" * 64, bid="brk-d",
                        opened=OPENED - dt.timedelta(days=20))  # fmt: skip
    lots = TaxLotRepo(conn)
    closed_at = (OPENED - dt.timedelta(days=3)).astimezone(dt.UTC).isoformat()
    lots.close_lot(old["lots"][0], close_price="", realized_pnl="-120", closed_at=closed_at)
    lots.close_lot(old["lots"][1], close_price="", realized_pnl="0", closed_at=closed_at)
    conn.execute("UPDATE open_structures SET status = 'closed'")
    conn.commit()
    new = open_position(conn)
    st = new["st"]
    rep = run(conn, FakeBroker(positions=held_positions(st), fills=open_fills(st)))
    assert rep.wash_sales == [old["lots"][0]]
    flags = {r["id"]: r["wash_sale"] for r in lots.with_structure()}
    assert flags[old["lots"][0]] == 1 and flags[old["lots"][1]] == 0
    assert ReasonCode.RECONCILE_WASH_SALE.value in reasons(conn)
    # re-run does not re-mark
    assert (
        run(conn, FakeBroker(positions=held_positions(st), fills=open_fills(st))).wash_sales == []
    )


def test_no_wash_sale_outside_window(conn: sqlite3.Connection) -> None:
    old = open_position(conn, phash="d" * 64, bid="brk-d",
                        opened=OPENED - dt.timedelta(days=90))  # fmt: skip
    closed_at = (OPENED - dt.timedelta(days=45)).astimezone(dt.UTC).isoformat()
    TaxLotRepo(conn).close_lot(old["lots"][0], close_price="", realized_pnl="-5",
                               closed_at=closed_at)  # fmt: skip
    conn.execute("UPDATE open_structures SET status = 'closed'")
    conn.commit()
    new = open_position(conn)
    rep = run(conn, FakeBroker(positions=held_positions(new["st"]), fills=open_fills(new["st"])))
    assert rep.wash_sales == []


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------


def test_performance_from_series() -> None:
    s = [
        DailyEquity(dt.date(2025, 12, 31), D("90000")),
        DailyEquity(dt.date(2026, 8, 31), D("100000")),
        DailyEquity(dt.date(2026, 9, 25), D("101000")),
        DailyEquity(dt.date(2026, 9, 28), D("101500")),
    ]
    p = performance_from(s, dt.date(2026, 9, 28))
    assert p is not None
    assert p.day_pnl == 500.0 and p.day_pct == pytest.approx(500 / 101000)
    assert p.mtd_pnl == 1500.0 and p.mtd_pct == pytest.approx(0.015)
    assert p.ytd_pnl == 11500.0 and p.equity == 101500.0
    assert performance_from(s, dt.date(2025, 12, 31)) is None  # no prior history
    assert performance_from(s, dt.date(2026, 9, 29)) is None  # no snapshot that day
    # history starting mid-period: since inception
    p2 = performance_from(s[2:], dt.date(2026, 9, 28))
    assert p2 is not None and p2.mtd_pnl == 500.0


def test_performance_month_and_year_rollover() -> None:
    s = [
        DailyEquity(dt.date(2026, 12, 30), D("100000")),
        DailyEquity(dt.date(2026, 12, 31), D("101000")),
        DailyEquity(dt.date(2027, 1, 4), D("102010")),
    ]
    p = performance_from(s, dt.date(2027, 1, 4))
    assert p is not None
    # new month and new year both start from Dec-31 close
    assert p.day_pnl == p.mtd_pnl == p.ytd_pnl == 1010.0
    assert p.mtd_pct == pytest.approx(0.01) and p.ytd_pct == pytest.approx(0.01)
    # Dec-31: MTD/YTD since inception (history began inside the month)
    p2 = performance_from(s, dt.date(2026, 12, 31))
    assert p2 is not None and p2.day_pnl == 1000.0 and p2.mtd_pnl == 1000.0


def test_performance_first_day_of_history_is_na(conn: sqlite3.Connection) -> None:
    assert performance(conn, dt.date(2026, 9, 28)) is None
    PnlSnapshotRepo(conn).insert(realized="0", unrealized="0", total="0",
                                 details_json=json.dumps({"day": "2026-09-28",
                                                          "equity": "100000"}))  # fmt: skip
    assert performance(conn, dt.date(2026, 9, 28)) is None


def test_group_legs_orphan_leg_does_not_hide_structures(conn: sqlite3.Connection) -> None:
    """S-5 / E6.3: an unattributed (bounded) leg is its own group; structures still valued."""
    st = bull_put()
    a = open_position(conn)
    extra = BrokerPosition(symbol="SPY261120P00700000", qty=D(1), side="long")
    groups = _group_legs(conn, [*held_positions(st), extra])
    assert [label for _, label, _ in groups] == [a["sid"], "SPY 2026-11-20"]


def test_performance_reads_latest_snapshot_per_day(conn: sqlite3.Connection) -> None:
    repo = PnlSnapshotRepo(conn)
    for day, eq in (("2026-09-25", "100000"), ("2026-09-28", "99000"), ("2026-09-28", "100250")):
        repo.insert(realized="0", unrealized="0", total="0",
                    details_json=json.dumps({"day": day, "equity": eq}))  # fmt: skip
    p = performance(conn, dt.date(2026, 9, 28))
    assert p is not None and p.day_pnl == 250.0


# ---------------------------------------------------------------------------
# Auditor routine
# ---------------------------------------------------------------------------


def _ctx(conn: sqlite3.Connection) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"personas": {"broker.reconcile": {"schedule": ["16:30"], "llm": False, "notify": "card",
                                  "halt_exempt": True, "ttl": "6h",
                                  "writes": ["journal", "note"]}}}
    )  # fmt: skip
    kind, step = routines.step("broker.reconcile")
    return JobContext(
        job="broker.reconcile", kind=kind, spec=step, run_id="run-a", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=lambda: settings(),
    )  # fmt: skip


def test_auditor_clean_card(conn: sqlite3.Connection) -> None:
    pos = open_position(conn)
    st = pos["st"]
    PnlSnapshotRepo(conn).insert(realized="0", unrealized="0", total="0",
                                 details_json=json.dumps({"day": "2026-09-25",
                                                          "equity": "100000"}))  # fmt: skip
    res = broker_reconcile(_ctx(conn), broker=FakeBroker(positions=held_positions(st),
                                                fills=open_fills(st)))  # fmt: skip
    assert res.metrics["clean"] and not res.notice
    assert res.card is not None and "[Broker] Reconcile" in res.card.text
    assert "+$500" in res.card.text


def test_auditor_mismatch_notice(conn: sqlite3.Connection) -> None:
    open_position(conn)
    res = broker_reconcile(_ctx(conn), broker=FakeBroker())
    assert res.metrics["halted"] and "HALTED" in res.notice and "!resume" in res.notice
    assert res.metrics["mismatches"] == 2  # position missing + fill missing


def test_auditor_output_maps_categories(conn: sqlite3.Connection) -> None:
    open_position(conn)
    rep = run(conn, FakeBroker(), halt=False)
    out = reconcile_output(rep)
    assert out.reconciliation_status == "discrepancies_found"
    assert {a.category for a in out.anomalies} == {"position_mismatch", "fill_discrepancy"}
    assert all(a.severity == "critical" for a in out.anomalies)
