"""Post-market reconciliation: broker (source of truth) vs Arc's local store (E6.3).

:func:`reconcile` is deterministic and read-only towards the broker (``account``,
``positions``, ``fills``, ``order_status``; it never submits or cancels). It:

1. **Positions.** Attributes broker option legs to ``open_structures``
   (:mod:`arc.reconcile.attribution`). A local structure the broker does not
   fully hold, a broker leg no structure accounts for, or a non-option broker
   position (e.g. shares after an assignment) is a mismatch.
2. **Orders.** Every local order still ``submitted`` / ``partially_filled`` /
   ``approved`` after the close, and every execution still ``working`` or
   ``unconfirmed`` (E6.2 stops the ladder there on purpose), is a mismatch; the
   broker's current status is in the detail.
3. **Fills.** Today's broker fills (per leg, per broker order) against today's
   local fills (per structure unit, ``fills.broker_fill_id`` = broker order id,
   expanded to legs through the proposal's structure). A fill on one side only,
   or a different quantity/side per leg, is a mismatch.
4. **Snapshots.** Writes one ``positions_snapshots`` row (structures, their
   broker legs, unattributed legs) and one ``pnl_snapshots`` row (realized today,
   broker unrealized, equity, start-of-day equity, day P&L) keyed by ET day.
5. **Tax lots.** Replaces the proposal-time mid on today's lots with the
   broker's per-leg fill price (open and close), and marks a loss lot
   ``wash_sale`` when another lot on the same underlying was opened within
   ``wash_sale_days`` before or after the loss close (§5: same underlying).

6. **Expiry (E11.4, D73).** On and after an expiration day, a structure the
   broker no longer holds is classified per leg (:func:`arc.exits.expiry.classify_expiry`):
   expired / exercised / assigned from the broker's OPEXP/OPEXC/OPASN activities,
   else from the share footprint (±100 x ratio x contracts). A fully classified
   structure is booked at intrinsic (the settle) and any share position it left
   is recorded in ``assignment_shares`` as *expected* until the owner unwinds it
   at the broker; the owner's stock fill is attributed to that row. An exercise
   or assignment raises an *opens-only* halt (``arc:expiry``); anything
   unclassified stays a mismatch and halts everything (fail closed).

``scope="intraday"`` (E11.1, D71) checks only step 2, and only for executions
left ``unconfirmed`` (optionally one proposal): no positions, fills, snapshots,
lots or settles, and it never halts by itself; the caller
(:func:`arc.broker.reconcile_job.intraday_reconcile`) decides. Both scopes adopt
an order that has no broker id by looking its ``client_order_id`` up (when the
adapter has ``order_status_by_client_id``): found = its broker id is recorded and
it is checked like any other; absent = the broker never took it, so it is
cancelled locally (``reconcile:resolved``).

Any mismatch (or a broker read failure: we can't prove the books agree) raises a
halt (``actor = arc:reconcile``) so no new entry is gated until the owner
``!resume``s; the caller posts the alert. Every outcome is journaled with a
``reconcile:*`` reason code.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.broker.base import TEST_CLIENT_ORDER_PREFIX
from arc.context.ttl import to_db
from arc.journal.outcomes import record_close_outcome
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.models import LegIntent, Structure
from arc.reconcile.attribution import attribute, broker_legs, holdings_from_rows
from arc.reconcile.baseline import Baseline, start_of_day_equity
from arc.reconcile.baseline import day_pnl as baseline_day_pnl
from arc.store.execution import OpenStructureRepo
from arc.store.repos import PnlSnapshotRepo, PositionsSnapshotRepo, TaxLotRepo
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from arc.broker.base import (
        AccountInfo,
        BrokerAdapter,
        BrokerOrderStatus,
        BrokerPosition,
        Fill,
    )
    from arc.config import ArcSettings
    from arc.exits.expiry import BrokerActivityView

__all__ = [
    "RECONCILE_ACTOR",
    "Mismatch",
    "MismatchKind",
    "ReconcileReport",
    "halt_on_mismatch",
    "reconcile",
]

log = structlog.get_logger(__name__)

RECONCILE_ACTOR = "arc:reconcile"
EXPIRY_ACTOR = "arc:expiry"  # E11.4 (D73): opens-only halts of the expiry guard
_OPEN_ORDER_STATES = ("approved", "submitted", "partially_filled")
_OPEN_EXEC_STATES = ("working", "unconfirmed")


class MismatchKind(StrEnum):
    POSITION_MISSING = "position_missing"  # local structure not (fully) held at the broker
    POSITION_UNATTRIBUTED = "position_unattributed"  # broker leg no structure accounts for
    POSITION_NON_OPTION = "position_non_option"  # e.g. shares after an assignment
    ORDER_OPEN = "order_open"  # local order / execution not terminal after the close
    FILL_UNKNOWN = "fill_unknown"  # broker fill Arc has no record of
    FILL_MISSING = "fill_missing"  # local fill the broker does not report
    FILL_QTY = "fill_qty"  # both sides have it, legs disagree
    BROKER_ERROR = "broker_error"  # could not read the broker: books unproven
    # E6.2c: an unrecorded broker fill whose client_order_id is ``test.``-prefixed
    # (an integration-test order). Reported as a notice, not a mismatch: no halt.
    FILL_TEST = "fill_test"


# Reconcile anomaly categories (AnomalyReport.category)
CATEGORY: dict[MismatchKind, str] = {
    MismatchKind.POSITION_MISSING: "position_mismatch",
    MismatchKind.POSITION_UNATTRIBUTED: "position_mismatch",
    MismatchKind.POSITION_NON_OPTION: "position_mismatch",
    MismatchKind.ORDER_OPEN: "missing_event",
    MismatchKind.FILL_UNKNOWN: "fill_discrepancy",
    MismatchKind.FILL_MISSING: "fill_discrepancy",
    MismatchKind.FILL_QTY: "fill_discrepancy",
    MismatchKind.BROKER_ERROR: "other",
    MismatchKind.FILL_TEST: "fill_discrepancy",
}


class Mismatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MismatchKind
    subject: str = Field(..., description="Ticker, OCC symbol or 'session'")
    detail: str
    refs: list[str] = Field(default_factory=list, description="Structure/order/broker ids")


class ReconcileReport(BaseModel):
    """What one reconciliation run found and wrote."""

    model_config = ConfigDict(extra="forbid")

    day: _dt.date
    at: _dt.datetime
    scope: Literal["full", "intraday"] = "full"
    mismatches: list[Mismatch] = Field(default_factory=list)
    notices: list[Mismatch] = Field(
        default_factory=list,
        description="Non-halting findings (fill_test: integration-test fills, E6.2c)",
    )
    structures_open: int = 0
    structures_attributed: int = 0
    closed_today: int = 0
    broker_legs: int = 0
    orders_checked: int = 0
    fills_local: int = 0
    fills_broker: int = 0
    equity: Decimal | None = None
    last_equity: Decimal | None = Field(
        default=None, description="Broker's raw last_equity (audit only; not the P&L basis)"
    )
    baseline: Baseline | None = Field(
        default=None, description="Start-of-day equity day_pnl is measured from (E5.9b, D43)"
    )
    day_pnl: Decimal | None = None
    realized: Decimal = Decimal(0)
    unrealized: Decimal = Decimal(0)
    lots_repriced: int = 0
    expired: list[str] = Field(default_factory=list, description="Structures settled at expiry")
    expiry_events: list[str] = Field(
        default_factory=list,
        description="E11.4: assignment/exercise lines (owner instruction included)",
    )
    unwound: list[str] = Field(
        default_factory=list, description="E11.4: assignment_shares rows unwound today"
    )
    expiry_halt_id: str | None = None
    expiry_pending: list[str] = Field(
        default_factory=list,
        description="E11.4: structures expired today, legs gone, settled at the next reconcile",
    )
    wash_sales: list[str] = Field(default_factory=list, description="Loss lots marked")
    positions_snapshot_id: str | None = None
    pnl_snapshot_id: str | None = None
    halted: bool = False
    halt_id: str | None = None

    @property
    def clean(self) -> bool:
        return not self.mismatches

    def summary(self) -> str:
        head = "clean" if self.clean else f"{len(self.mismatches)} mismatch(es)"
        if self.scope == "intraday":
            stop = "; HALTED" if self.halted else ""
            return f"intraday {head}: {self.orders_checked} order(s) checked{stop}"
        pnl = f", day P&L ${self.day_pnl:+,.2f}" if self.day_pnl is not None else ""
        text = (
            f"{head}: {self.structures_attributed}/{self.structures_open} structures held, "
            f"{self.fills_local} local / {self.fills_broker} broker fill(s){pnl}"
        )
        if self.wash_sales:
            text += f", {len(self.wash_sales)} wash sale(s)"
        if self.notices:
            text += f", {len(self.notices)} test fill(s) ignored"
        if self.expiry_events:
            text += f", {len(self.expiry_events)} assignment/exercise event(s) (opens halted)"
        if self.unwound:
            text += f", {len(self.unwound)} share unwind(s) attributed"
        return text + ("; HALTED" if self.halted else "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(text: str | None) -> _dt.datetime | None:
    if not text:
        return None
    ts = _dt.datetime.fromisoformat(text)
    return ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts


def _dec(text: Any) -> Decimal | None:
    if text is None or text == "":
        return None
    try:
        return Decimal(str(text))
    except ArithmeticError:
        return None


def _day_bounds(day: _dt.date) -> tuple[_dt.datetime, _dt.datetime]:
    start = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=ET)
    return start, start + _dt.timedelta(days=1)


def _on_day(ts: _dt.datetime | None, day: _dt.date) -> bool:
    return ts is not None and ts.astimezone(ET).date() == day


def _structure_legs(structure_json: str, units: int) -> dict[str, tuple[str, int]]:
    """``occ -> (buy|sell, qty)`` an order for *units* of the structure trades."""
    st = Structure.model_validate_json(structure_json)
    out: dict[str, tuple[str, int]] = {}
    for leg in st.legs:
        side = "buy" if leg.side == LegIntent.LONG else "sell"
        out[parse_occ(leg.occ_symbol).format()] = (side, leg.ratio * units)
    return out


# ---------------------------------------------------------------------------
# Checks (pure over their inputs)
# ---------------------------------------------------------------------------


def _signed_shares(p: BrokerPosition) -> int:
    q = abs(p.qty)
    return -int(q) if (p.side == "short" or p.qty < 0) else int(q)


def _check_positions(
    rows: list[dict[str, Any]],
    positions: list[BrokerPosition],
    report: ReconcileReport,
    expected_shares: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Broker positions vs the open structures.

    *expected_shares* (E11.4): root -> signed shares open ``assignment_shares``
    rows account for. A share position equal to it is expected, not a mismatch.
    """
    expected = expected_shares or {}
    try:
        legs, other = broker_legs(positions)
    except ValueError as exc:
        report.mismatches.append(Mismatch(kind=MismatchKind.BROKER_ERROR, subject="positions",
                                          detail=str(exc)))  # fmt: skip
        return {}
    holdings = holdings_from_rows(rows)
    att = attribute(holdings, legs)
    report.structures_open = len(holdings)
    report.structures_attributed = len(att.attributed)
    report.broker_legs = len(legs)
    for h in att.unmatched:
        exps = sorted({parse_occ(s).expiration for s in h.legs})
        want = ", ".join(f"{s} {q:+d}" for s, q in sorted(h.legs.items()))
        expired = exps[-1] < report.day if exps else False
        note = f" (expired {exps[-1]:%Y-%m-%d}: not settled)" if expired else ""
        report.mismatches.append(
            Mismatch(
                kind=MismatchKind.POSITION_MISSING,
                subject=h.ticker,
                detail=f"structure {h.structure_id} expects [{want}] but the broker does not "
                f"hold it{note}",
                refs=[h.structure_id],
            )
        )
    for sym, qty in att.unattributed.items():
        report.mismatches.append(
            Mismatch(
                kind=MismatchKind.POSITION_UNATTRIBUTED,
                subject=sym,
                detail=f"broker holds {qty:+d} {sym} that no open structure accounts for",
            )
        )
    share_qty = {p.symbol: _signed_shares(p) for p in positions if p.asset_class != "us_option"}
    for sym in other:
        if sym in expected and share_qty.get(sym) == expected[sym]:
            continue  # E11.4: assignment/exercise shares awaiting the owner's unwind
        want = f" (expected {expected[sym]:+d} from assignment/exercise)" if sym in expected else ""
        report.mismatches.append(
            Mismatch(
                kind=MismatchKind.POSITION_NON_OPTION,
                subject=sym,
                detail=f"broker holds a non-option position in {sym} "
                f"({share_qty.get(sym, 0):+d} shares; assignment/exercise?){want}",
            )
        )
    for sym, qty in expected.items():
        if sym not in share_qty:
            # Recorded shares gone with no attributed fill: the owner's unwind was not
            # seen (e.g. done on an earlier day). A human confirms (fail closed).
            report.mismatches.append(
                Mismatch(
                    kind=MismatchKind.POSITION_MISSING,
                    subject=sym,
                    detail=f"expected {qty:+d} {sym} shares from an assignment/exercise, the "
                    "broker holds none and no unwind fill was attributed",
                )
            )
    by_sym = {p.symbol: p for p in positions}
    return {
        "structures": [
            {
                "structure_id": h.structure_id,
                "ticker": h.ticker,
                "legs": dict(h.legs),
                "held": h in att.attributed,
            }
            for h in holdings
        ],
        "unattributed": att.unattributed,
        "broker": [
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "side": p.side,
                "asset_class": p.asset_class,
                "avg_entry_price": None if p.avg_entry_price is None else str(p.avg_entry_price),
                "market_value": None if p.market_value is None else str(p.market_value),
                "unrealized_pl": None if p.unrealized_pl is None else str(p.unrealized_pl),
            }
            for p in by_sym.values()
        ],
    }


_NO_FILL_TERMINAL = {
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "expired": "expired",
    "done_for_day": "expired",
    "rejected": "rejected",
}


def _lookup_by_client_id(
    broker: BrokerAdapter, client_order_id: str | None
) -> tuple[bool, BrokerOrderStatus | None, str]:
    """``(asked, status, error)``: the broker's order under *client_order_id* (D71).

    ``asked`` is False when the adapter cannot look up by client id or the row has
    none; ``status`` None with ``asked`` True and no error = the broker has no
    such order.
    """
    lookup = getattr(broker, "order_status_by_client_id", None)
    if not client_order_id or not callable(lookup):
        return False, None, ""
    try:
        return True, lookup(client_order_id), ""
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return True, None, f"{type(exc).__name__}: {exc}"


def _check_orders(
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    report: ReconcileReport,
    journal: list[dict[str, Any]],
    now: _dt.datetime,
    run_id: str | None,
    *,
    only: set[str] | None = None,
    exec_states: tuple[str, ...] = _OPEN_EXEC_STATES,
) -> None:
    """Flag local orders/executions left open after the close.

    An order the broker reports as terminal *without any fill* is resolved
    locally (moved to that state; its execution becomes ``cancelled``) and
    journaled. Anything with a fill, or that the broker can't confirm, is a
    mismatch: a fill changes positions, which only a human may settle.

    D71: an order with no broker id is looked up by ``client_order_id``; found =
    its broker id is adopted and the rule above applies, absent = cancelled
    locally. *only* restricts the check to those proposal hashes (intraday scope);
    *exec_states* are the execution statuses checked.
    """
    from arc.models import OrderState
    from arc.store.execution import ExecutionRepo
    from arc.store.order_state import VALID_TRANSITIONS
    from arc.store.repos import OrderRepo

    orders = OrderRepo(conn)
    marks = ",".join("?" * len(_OPEN_ORDER_STATES))
    rows = conn.execute(
        f"""SELECT o.id, o.state, o.broker_order_id, o.client_order_id, o.proposal_hash,
                   p.ticker
            FROM orders o LEFT JOIN proposals p ON p.proposal_hash = o.proposal_hash
            WHERE o.state IN ({marks}) ORDER BY o.created_at, o.id""",  # noqa: S608 - fixed placeholders
        _OPEN_ORDER_STATES,
    ).fetchall()
    for r in rows:
        if only is not None and r["proposal_hash"] not in only:
            continue
        report.orders_checked += 1
        status = "no broker id"
        bid = r["broker_order_id"]
        st: BrokerOrderStatus | None = None
        frm = OrderState(r["state"])
        if not bid:
            asked, st, err = _lookup_by_client_id(broker, r["client_order_id"])
            if asked and err:
                status = f"no broker id; client-id lookup failed ({err})"
            elif asked and st is None:
                orders.transition(
                    order_id=r["id"], to_state=OrderState.CANCELLED, actor=RECONCILE_ACTOR,
                    detail="reconcile: broker has no order with this client id",
                    event_at=to_db(now), run_id=run_id,
                )  # fmt: skip
                journal.append({
                    "subject": r["ticker"] or r["client_order_id"], "choice": Choice.NOTED,
                    "code": ReasonCode.RECONCILE_RESOLVED,
                    "text": f"order {r['client_order_id']} was {r['state']} locally with no "
                    "broker id; the broker has no order under its client id: set cancelled",
                    "payload": {"order_id": r["id"]},
                })  # fmt: skip
                continue
            elif st is not None:
                bid = st.broker_order_id
                orders.set_broker_order_id(r["id"], bid)
                if frm is OrderState.APPROVED:
                    orders.transition(
                        order_id=r["id"], to_state=OrderState.SUBMITTED, actor=RECONCILE_ACTOR,
                        detail=f"reconcile: adopted broker {bid} by client id",
                        event_at=to_db(now), run_id=run_id,
                    )  # fmt: skip
                    frm = OrderState.SUBMITTED
                journal.append({
                    "subject": r["ticker"] or r["client_order_id"], "choice": Choice.NOTED,
                    "code": ReasonCode.RECONCILE_RESOLVED,
                    "text": f"order {r['client_order_id']} had no broker id; adopted {bid} "
                    f"by client id (broker says {st.status})",
                    "payload": {"order_id": r["id"], "broker_order_id": bid},
                })  # fmt: skip
        if bid:
            if st is None:
                try:
                    st = broker.order_status(bid)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    status = f"broker status unreadable ({type(exc).__name__}: {exc})"
            if st is not None:
                status = f"broker says {st.status} (filled {st.filled_qty})"
                target = _NO_FILL_TERMINAL.get(st.status)
                has_fill = conn.execute(
                    "SELECT 1 FROM fills WHERE order_id = ? LIMIT 1", (r["id"],)
                ).fetchone()
                to = OrderState(target) if target else None
                if to is not None and to not in VALID_TRANSITIONS[frm]:
                    to = OrderState.CANCELLED
                if to is not None and st.filled_qty == 0 and not has_fill:
                    orders.transition(
                        order_id=r["id"], to_state=to, actor=RECONCILE_ACTOR,
                        detail=f"reconcile: {status}", event_at=to_db(now), run_id=run_id,
                    )  # fmt: skip
                    journal.append({
                        "subject": r["ticker"] or r["client_order_id"], "choice": Choice.NOTED,
                        "code": ReasonCode.RECONCILE_RESOLVED,
                        "text": f"order {r['client_order_id']} was {r['state']} locally; "
                        f"{status}: set {to.value}",
                        "payload": {"order_id": r["id"], "broker_order_id": bid},
                    })  # fmt: skip
                    continue
        when = "intraday" if only is not None else "after the close"
        report.mismatches.append(
            Mismatch(
                kind=MismatchKind.ORDER_OPEN,
                subject=r["ticker"] or r["client_order_id"],
                detail=f"order {r['client_order_id']} is still {frm.value} locally {when}; "
                f"{status}",
                refs=[r["id"], *([bid] if bid else [])],
            )
        )
    marks = ",".join("?" * len(exec_states))
    for r in conn.execute(
        f"""SELECT e.proposal_hash, e.status, e.detail, p.ticker FROM executions e
            LEFT JOIN proposals p ON p.proposal_hash = e.proposal_hash
            WHERE e.status IN ({marks})""",  # noqa: S608 - fixed placeholders
        exec_states,
    ).fetchall():
        phash = r["proposal_hash"]
        if only is not None and phash not in only:
            continue
        order_marks = ",".join("?" * len(_OPEN_ORDER_STATES))
        still_open = conn.execute(
            f"SELECT 1 FROM orders WHERE proposal_hash = ? AND state IN ({order_marks}) LIMIT 1",  # noqa: S608
            (phash, *_OPEN_ORDER_STATES),
        ).fetchone()
        filled = conn.execute(
            """SELECT 1 FROM fills f JOIN orders o ON o.id = f.order_id
               WHERE o.proposal_hash = ? LIMIT 1""",
            (phash,),
        ).fetchone()
        if not still_open and not filled:
            ExecutionRepo(conn).finish(
                phash, status="cancelled", now=now,
                detail=f"resolved by reconcile (was {r['status']}): every attempt is terminal "
                "at the broker with no fill",
            )  # fmt: skip
            journal.append({
                "subject": r["ticker"] or phash[:12], "choice": Choice.NOTED,
                "code": ReasonCode.RECONCILE_RESOLVED,
                "text": f"execution {phash[:12]} was {r['status']}; no fill at the broker: "
                "set cancelled",
            })  # fmt: skip
            continue
        report.mismatches.append(
            Mismatch(
                kind=MismatchKind.ORDER_OPEN,
                subject=r["ticker"] or phash[:12],
                detail=f"execution {phash[:12]} is {r['status']}"
                + (" with a recorded fill" if filled else "")
                + f": {(r['detail'] or '')[:200]}",
                refs=[phash],
            )
        )


def _local_fills(conn: sqlite3.Connection, day: _dt.date) -> list[dict[str, Any]]:
    start, end = _day_bounds(day)
    rows = conn.execute(
        """SELECT f.id, f.qty, f.price, f.filled_at, f.broker_fill_id, o.id AS order_id,
                  o.broker_order_id, o.proposal_hash, p.structure_json, p.ticker
           FROM fills f JOIN orders o ON o.id = f.order_id
           LEFT JOIN proposals p ON p.proposal_hash = o.proposal_hash
           WHERE f.filled_at >= ? AND f.filled_at < ?""",
        (to_db(start), to_db(end)),
    ).fetchall()
    return [dict(r) for r in rows]


def _is_option_symbol(sym: str) -> bool:
    try:
        parse_occ(sym)
    except ValueError:
        return False
    return True


def _check_fills(
    local: list[dict[str, Any]],
    broker_fills: list[Fill],
    report: ReconcileReport,
    unwinds: list[dict[str, Any]] | None = None,
) -> None:
    """Broker fills vs local fills. Equity fills (E11.4) are matched to open
    ``assignment_shares`` rows by the caller and passed in *unwinds*; every other
    equity fill is ``fill_unknown``."""
    got: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(lambda: ["", 0]))
    coids: dict[str, str] = {}
    matched = {u["broker_order_id"] for u in unwinds or []}
    for f in broker_fills:
        if not _is_option_symbol(f.symbol):
            if f.broker_order_id not in matched:
                report.mismatches.append(
                    Mismatch(kind=MismatchKind.FILL_UNKNOWN, subject=f.symbol,
                             refs=[f.broker_order_id],
                             detail=f"broker equity order {f.broker_order_id} filled {f.side} "
                             f"{f.qty} {f.symbol} @ {f.price} with no local record and no "
                             "open assignment/exercise share row")
                )  # fmt: skip
            continue
        leg = got[f.broker_order_id][parse_occ(f.symbol).format()]
        leg[0] = f.side
        leg[1] += int(f.qty)
        if f.client_order_id:
            coids[f.broker_order_id] = f.client_order_id
    want: dict[str, dict[str, tuple[str, int]]] = {}
    who: dict[str, dict[str, Any]] = {}
    for f in local:
        bid = f["broker_fill_id"] or f["broker_order_id"] or f"local:{f['id']}"
        legs = _structure_legs(f["structure_json"], int(f["qty"])) if f["structure_json"] else {}
        acc = want.setdefault(bid, {})
        for sym, (side, qty) in legs.items():
            prev = acc.get(sym, (side, 0))[1]
            acc[sym] = (side, prev + qty)
        who[bid] = f
    report.fills_local = len(local)
    report.fills_broker = len(got) + len(matched)
    for bid in sorted(set(got) - set(want)):
        legs = ", ".join(f"{s} {sd} {q}" for s, (sd, q) in sorted(got[bid].items()))
        root = parse_occ(next(iter(got[bid]))).root
        coid = coids.get(bid, "")
        if coid.startswith(TEST_CLIENT_ORDER_PREFIX):
            # E6.2c: an integration-test order hit this account. Not Arc's trade and
            # not an unexplained one: report it, don't halt. Any leg it left open is
            # still a position_unattributed mismatch (a human must close it).
            report.notices.append(
                Mismatch(kind=MismatchKind.FILL_TEST, subject=root, refs=[bid, coid],
                         detail=f"integration-test order {coid} ({bid}) filled [{legs}] "
                         "on this account; tests must use the dedicated test account")
            )  # fmt: skip
            continue
        report.mismatches.append(
            Mismatch(kind=MismatchKind.FILL_UNKNOWN, subject=root, refs=[bid],
                     detail=f"broker order {bid} filled [{legs}] with no local record")
        )  # fmt: skip
    for bid in sorted(set(want) - set(got)):
        f = who[bid]
        report.mismatches.append(
            Mismatch(kind=MismatchKind.FILL_MISSING, subject=f["ticker"] or bid,
                     refs=[f["order_id"], bid],
                     detail=f"local fill {f['qty']} @ {f['price']} ({bid}) is not reported "
                     "by the broker")
        )  # fmt: skip
    for bid in sorted(set(want) & set(got)):
        exp = want[bid]
        act = {s: (sd, q) for s, (sd, q) in got[bid].items()}
        if exp != act:
            f = who[bid]
            report.mismatches.append(
                Mismatch(
                    kind=MismatchKind.FILL_QTY,
                    subject=f["ticker"] or bid,
                    refs=[f["order_id"], bid],
                    detail=f"broker order {bid}: local legs {sorted(exp.items())} vs broker "
                    f"{sorted(act.items())}",
                )
            )


# ---------------------------------------------------------------------------
# Tax lots
# ---------------------------------------------------------------------------


def _leg_prices(broker_fills: list[Fill]) -> dict[tuple[str, str], Decimal]:
    """``(broker order id, occ) -> qty-weighted fill price`` per leg."""
    tot: dict[tuple[str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for f in broker_fills:
        acc = tot[(f.broker_order_id, parse_occ(f.symbol).format())]
        acc[0] += f.qty * f.price
        acc[1] += f.qty
    return {k: (v[0] / v[1]).quantize(Decimal("0.0001")) for k, v in tot.items() if v[1] > 0}


def _update_lots(
    conn: sqlite3.Connection,
    broker_fills: list[Fill],
    report: ReconcileReport,
    settings: ArcSettings,
    journal: list[dict[str, Any]],
) -> None:
    repo = TaxLotRepo(conn)
    prices = _leg_prices(broker_fills)
    lots = repo.with_structure()
    order_bid = {
        r["id"]: r["broker_order_id"]
        for r in conn.execute("SELECT id, broker_order_id FROM orders").fetchall()
    }
    # closing broker order per opening proposal: executions(kind=close) -> structure
    close_bid: dict[str, str] = {}
    for r in conn.execute(
        """SELECT s.open_proposal_hash, o.broker_order_id FROM executions e
           JOIN open_structures s ON s.id = e.structure_id
           JOIN orders o ON o.proposal_hash = e.proposal_hash
           WHERE e.kind = 'close' AND o.broker_order_id IS NOT NULL
             AND o.state IN ('filled', 'partially_filled', 'cancelled')
           ORDER BY o.created_at"""
    ).fetchall():
        close_bid[r["open_proposal_hash"]] = r["broker_order_id"]

    repriced: dict[str, list[str]] = defaultdict(list)
    for lot in lots:
        sym = parse_occ(lot["occ_symbol"]).format()
        new_open = prices.get((order_bid.get(lot["order_id"]) or "", sym))
        new_close = None
        if lot["closed_at"] and lot["structure_hash"] in close_bid:
            new_close = prices.get((close_bid[lot["structure_hash"]], sym))
        o = new_open if new_open is not None and _dec(lot["open_price"]) != new_open else None
        c = new_close if new_close is not None and _dec(lot["close_price"]) != new_close else None
        if o is None and c is None:
            continue
        repo.set_prices(
            lot["id"],
            open_price=None if o is None else str(o),
            close_price=None if c is None else str(c),
            commit=False,
        )
        report.lots_repriced += 1
        repriced[lot["ticker"]].append(
            f"{sym}" + (f" open {o}" if o is not None else "") + (f" close {c}" if c else "")
        )
    for ticker, items in repriced.items():
        journal.append({
            "subject": ticker, "choice": Choice.NOTED, "code": ReasonCode.RECONCILE_LOT_PRICE,
            "text": "tax lots set to broker fill prices: " + "; ".join(items),
        })  # fmt: skip

    # Wash sale (§5, same underlying): a loss lot with another lot on the ticker
    # opened within ±wash_sale_days of the loss close.
    window = _dt.timedelta(days=settings.wash_sale_days)
    for lot in lots:
        pnl = _dec(lot["realized_pnl"])
        closed = _parse_ts(lot["closed_at"])
        if pnl is None or pnl >= 0 or closed is None or lot["wash_sale"]:
            continue
        repl = [
            other
            for other in lots
            if other["ticker"] == lot["ticker"]
            and other["structure_hash"] != lot["structure_hash"]
            and (opened := _parse_ts(other["opened_at"])) is not None
            and abs(opened - closed) <= window
        ]
        if not repl:
            continue
        repo.mark_wash_sale(lot["id"], commit=False)
        report.wash_sales.append(lot["id"])
        journal.append({
            "subject": lot["ticker"], "choice": Choice.NOTED,
            "code": ReasonCode.RECONCILE_WASH_SALE,
            "text": f"loss {pnl} closed {closed:%Y-%m-%d} with {len(repl)} replacement lot(s) "
            f"within {settings.wash_sale_days}d: loss disallowed (wash sale)",
            "payload": {"lot_id": lot["id"], "replacements": [r["id"] for r in repl]},
        })  # fmt: skip


def _open_assignment_shares(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM assignment_shares WHERE unwound_at IS NULL ORDER BY detected_at"
    ).fetchall()
    return [dict(r) for r in rows]


def _expected_shares(open_rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in open_rows:
        out[r["root"]] += int(r["qty"])
    return dict(out)


def _activities(
    broker: BrokerAdapter, since: _dt.date | None
) -> tuple[list[BrokerActivityView] | None, str | None]:
    """The broker's option NTAs since *since* (``None`` = no feed / unreadable)."""
    from arc.exits.expiry import BrokerActivityView

    fetch = getattr(broker, "activities", None)
    if since is None or not callable(fetch):
        return None, None
    try:
        rows = fetch(since, types=("OPASN", "OPEXC", "OPEXP", "OPTRD"))
    except Exception as exc:  # noqa: BLE001 - the share footprint decides then (fail closed)
        log.warning("reconcile.activities_failed", error=str(exc))
        return None, f"{type(exc).__name__}: {exc}"
    return [
        BrokerActivityView.model_validate(a.model_dump() if hasattr(a, "model_dump") else a)
        for a in rows
    ], None


def _book_close(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    close_net: Decimal,
    spot: Decimal,
    now: _dt.datetime,
) -> Decimal:
    """Close *row* at *close_net* (per share), realise P&L on its first lot; returns it."""
    repo = OpenStructureRepo(conn)
    lots = TaxLotRepo(conn)
    n = int(row["contracts"])
    pnl = -(Decimal(row["entry_net"]) + close_net) * 100 * n
    repo.reduce(row["id"], closed_qty=n, close_net=close_net, now=now, commit=False)
    record_close_outcome(conn, row["id"], expired=True, settlement=spot)  # E7.4b
    open_lots = conn.execute(
        """SELECT l.id FROM tax_lots l JOIN orders o ON o.id = l.order_id
           WHERE o.proposal_hash = ? AND l.closed_at IS NULL ORDER BY l.rowid""",
        (row["open_proposal_hash"],),
    ).fetchall()
    for i, lot in enumerate(open_lots):
        lots.close_lot(
            lot["id"],
            close_price="0",
            realized_pnl=str(pnl if i == 0 else Decimal(0)),
            closed_at=now.astimezone(_dt.UTC).isoformat(),
        )
    return pnl


def _settle_expiry(
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    rows: list[dict[str, Any]],
    positions: list[BrokerPosition],
    report: ReconcileReport,
    journal: list[dict[str, Any]],
    now: _dt.datetime,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None,
    open_shares: list[dict[str, Any]],
    run_id: str | None,
) -> list[dict[str, Any]]:
    """Settle structures whose every leg has expired (E11.4: on and after expiry day).

    Returns the rows still open. A structure is settled only when the broker holds
    none of its legs and ``settle_price(root, expiration)`` knows the close:

    - no unexplained shares of the root and no activity for its legs: closed at
      intrinsic, as before E11.4 (cash-equivalent expiry; the ladder sign
      convention, + paid / - received; realised -(entry + close) x 100 x n on
      the first lot);
    - otherwise :func:`arc.exits.expiry.classify_expiry` decides per leg from the
      broker's activities or the share footprint. Only a ``confident``
      classification is booked (same intrinsic close) and its share position is
      recorded in ``assignment_shares``; an unclassified one stays open and
      ``_check_positions`` reports it (total halt, status quo).
    """
    from arc.exits.expiry import classify_expiry, intrinsic

    if settle_price is None:
        return rows
    held = {parse_occ(p.symbol).format() for p in positions if p.asset_class == "us_option"}
    shares_now = {p.symbol: _signed_shares(p) for p in positions if p.asset_class != "us_option"}
    expected = _expected_shares(open_shares)
    candidates: list[tuple[dict[str, Any], Structure, list[Any], _dt.date]] = []
    still: list[dict[str, Any]] = []
    for row in rows:
        st = Structure.model_validate_json(row["structure_json"])
        occs = [parse_occ(leg.occ_symbol) for leg in st.legs]
        exp = max((o.expiration for o in occs), default=report.day)
        if exp > report.day or any(o.format() in held for o in occs):
            still.append(row)
            continue
        candidates.append((row, st, occs, exp))
    if not candidates:
        return still
    activities, act_error = _activities(broker, min(c[3] for c in candidates))
    acted = {
        parse_occ(a.symbol).format()
        for a in activities or []
        if a.activity_type != "OPTRD" and _is_option_symbol(a.symbol)
    }
    for row, st, occs, exp in candidates:
        root = row["ticker"]
        spot = settle_price(root, exp)
        if spot is None:
            still.append(row)
            continue
        free = shares_now.get(root, 0) - expected.get(root, 0)
        legs_acted = any(o.format() in acted for o in occs)
        if free == 0 and not legs_acted:
            close_net = Decimal(0)
            for leg, occ in zip(st.legs, occs, strict=True):
                sign = -1 if leg.side == LegIntent.LONG else 1
                close_net += sign * leg.ratio * intrinsic(occ.kind, occ.strike, spot)
            if exp == report.day and any(intrinsic(o.kind, o.strike, spot) > 0 for o in occs):
                # Expiry day, an ITM leg and no activity/shares yet: the broker books
                # exercise/assignment overnight. Settle tomorrow, not at a guess.
                report.expiry_pending.append(row["id"])
                still.append(row)
                continue
            pnl = _book_close(conn, row, close_net, spot, now)
            report.expired.append(row["id"])
            journal.append({
                "subject": root, "choice": Choice.NOTED,
                "code": ReasonCode.RECONCILE_EXPIRED,
                "text": f"structure {row['id']} expired {exp:%Y-%m-%d} ({root} settled "
                f"{spot}); closed at {close_net:+}, realized {pnl:+.2f}",
                "payload": {"structure_id": row["id"], "realized_pnl": str(pnl),
                            "settle": str(spot), "close_net": str(close_net)},
            })  # fmt: skip
            continue
        cls = classify_expiry(
            structure_id=row["id"],
            structure=st,
            contracts=int(row["contracts"]),
            held=held,
            shares=free,
            activities=activities,
            settle=spot,
        )
        if not cls.confident or cls.close_net is None:
            if exp == report.day:
                # The broker is still processing today's expiry: classify tomorrow.
                report.expiry_pending.append(row["id"])
                still.append(row)
                continue
            journal.append({
                "subject": root, "choice": Choice.FAILED,
                "code": ReasonCode.RECONCILE_MISMATCH,
                "text": f"structure {row['id']} expired {exp:%Y-%m-%d}: legs not classified "
                f"({', '.join(f'{e.occ} {e.kind}' for e in cls.legs)}; {free:+d} unexplained "
                f"{root} shares" + (f"; activities unreadable: {act_error}" if act_error else "")
                + ")",
                "payload": {"structure_id": row["id"],
                            "classification": cls.model_dump(mode="json")},
            })  # fmt: skip
            still.append(row)
            continue
        pnl = _book_close(conn, row, cls.close_net, spot, now)
        report.expired.append(row["id"])
        if cls.shares_net:
            via = "activity" if any(e.via == "activity" for e in cls.events) else "inferred"
            conn.execute(
                """INSERT INTO assignment_shares (id, structure_id, root, qty, via, from_occ,
                       basis, detected_at, evidence_json, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"asn_{row['id']}",
                    row["id"],
                    root,
                    cls.shares_net,
                    via,
                    ",".join(e.occ for e in cls.events),
                    str(cls.basis if cls.basis is not None else spot),
                    to_db(now),
                    json.dumps(cls.model_dump(mode="json"), default=str),
                    run_id,
                ),
            )
        for ev in cls.events:
            side = "sell" if cls.shares_net > 0 else "buy"
            instr = (
                f"{side} {abs(cls.shares_net)} {root} at the broker, then !resume"
                if cls.shares_net
                else "no net shares (the legs offset); !resume once checked"
            )
            line = (
                f"{root}: {ev.occ} {ev.kind} ({ev.via}); structure {row['id'][:8]} booked at "
                f"settle {spot}, realized {pnl:+.2f}; shares {cls.shares_net:+d}. You: {instr}"
            )
            report.expiry_events.append(line)
            journal.append({
                "subject": root, "choice": Choice.NOTED,
                "code": ReasonCode.RECONCILE_ASSIGNMENT if ev.kind == "assigned"
                else ReasonCode.RECONCILE_EXERCISE,
                "text": line,
                "payload": {"structure_id": row["id"], "realized_pnl": str(pnl),
                            "settle": str(spot), "close_net": str(cls.close_net),
                            "leg": ev.model_dump(mode="json"),
                            "shares_net": cls.shares_net},
            })  # fmt: skip
        if not cls.events:
            journal.append({
                "subject": root, "choice": Choice.NOTED,
                "code": ReasonCode.RECONCILE_EXPIRED,
                "text": f"structure {row['id']} expired {exp:%Y-%m-%d} (classified from "
                f"activities); closed at {cls.close_net:+}, realized {pnl:+.2f}",
                "payload": {"structure_id": row["id"], "realized_pnl": str(pnl),
                            "settle": str(spot), "close_net": str(cls.close_net)},
            })  # fmt: skip
        expected[root] = expected.get(root, 0) + cls.shares_net
        open_shares.append({"root": root, "qty": cls.shares_net, "id": f"asn_{row['id']}",
                            "basis": str(cls.basis or spot), "unwound_at": None})  # fmt: skip
    return still


def _attribute_unwinds(
    conn: sqlite3.Connection,
    broker_fills: list[Fill],
    open_shares: list[dict[str, Any]],
    report: ReconcileReport,
    journal: list[dict[str, Any]],
    now: _dt.datetime,
) -> list[dict[str, Any]]:
    """E11.4 rule 6: match equity fills to open ``assignment_shares`` rows.

    A broker equity order in the row's root, opposite side, with the row's |qty|
    (summed over the order's fills) unwinds it: ``unwound_at``/``unwind_price``
    are set and the realised share P&L ``(unwind - basis) x qty`` is journaled
    (``reconcile:resolved``). Returns the matched ``{broker_order_id, row id}``.
    """
    orders: dict[str, dict[str, Any]] = {}
    for f in broker_fills:
        if _is_option_symbol(f.symbol):
            continue
        o = orders.setdefault(
            f.broker_order_id,
            {"symbol": f.symbol, "side": f.side, "qty": Decimal(0), "notional": Decimal(0),
             "at": f.filled_at},
        )  # fmt: skip
        o["qty"] += f.qty
        o["notional"] += f.qty * f.price
    out: list[dict[str, Any]] = []
    for row in open_shares:
        if row.get("unwound_at") is not None:
            continue
        qty = int(row["qty"])
        want_side = "sell" if qty > 0 else "buy"
        for bid, o in sorted(orders.items()):
            if any(m["broker_order_id"] == bid for m in out):
                continue
            if o["symbol"] != row["root"] or o["side"] != want_side or o["qty"] != abs(qty):
                continue
            price = (o["notional"] / o["qty"]).quantize(Decimal("0.0001"))
            basis = Decimal(str(row["basis"]))
            pnl = (price - basis) * qty
            conn.execute(
                "UPDATE assignment_shares SET unwound_at = ?, unwind_price = ? WHERE id = ?",
                (to_db(now), str(price), row["id"]),
            )
            row["unwound_at"] = to_db(now)
            out.append({"broker_order_id": bid, "row_id": row["id"]})
            report.unwound.append(row["id"])
            journal.append({
                "subject": row["root"], "choice": Choice.NOTED,
                "code": ReasonCode.RECONCILE_RESOLVED,
                "text": f"{row['root']}: owner unwound {qty:+d} assignment/exercise shares "
                f"({o['side']} {o['qty']} @ {price}, broker order {bid}); basis {basis}, "
                f"realized {pnl:+.2f}",
                "payload": {"assignment_shares_id": row["id"], "broker_order_id": bid,
                            "unwind_price": str(price), "basis": str(basis),
                            "realized_pnl": str(pnl)},
            })  # fmt: skip
            break
    return out


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------


def _realized_today(conn: sqlite3.Connection, day: _dt.date) -> tuple[Decimal, int]:
    realized = Decimal(0)
    for r in conn.execute(
        "SELECT closed_at, realized_pnl FROM tax_lots WHERE closed_at IS NOT NULL"
    ).fetchall():
        if _on_day(_parse_ts(r["closed_at"]), day):
            realized += _dec(r["realized_pnl"]) or Decimal(0)
    closed = sum(
        1
        for r in conn.execute(
            "SELECT closed_at FROM open_structures WHERE status = 'closed'"
        ).fetchall()
        if _on_day(_parse_ts(r["closed_at"]), day)
    )
    return realized, closed


def _pnl_details(
    report: ReconcileReport, info: AccountInfo | None, *, virtual: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "day": report.day.isoformat(),
        "equity": None if info is None else str(info.equity),
        "last_equity": None if info is None or info.last_equity is None else str(info.last_equity),
        "prev_close": None if report.baseline is None else str(report.baseline.value),
        "prev_close_source": None if report.baseline is None else report.baseline.source,
        "day_pnl": None if report.day_pnl is None else str(report.day_pnl),
        "cash": None if info is None else str(info.cash),
        "open_structures": report.structures_open,
        "closed_today": report.closed_today,
        "clean": report.clean,
    }
    if virtual:
        # E10.2/E10.3 (D44): an experiment arm's account() is its virtual account;
        # the evaluator reads only this key (arc.experiments.evaluate.TREATMENT_EQUITY_FIELD)
        out["virtual_equity"] = None if info is None else str(info.equity)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def reconcile(
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    *,
    settings: ArcSettings,
    now: _dt.datetime,
    run_id: str | None = None,
    halt: bool = True,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
    scope: Literal["full", "intraday"] = "full",
    proposal_hashes: set[str] | None = None,
) -> ReconcileReport:
    """Reconcile *broker* against the local store for the ET day of *now* (see module doc).

    ``settle_price(root, day)`` (optional) returns the underlying's close on an
    expiration day, so expired structures can be settled at intrinsic value.
    ``scope="intraday"`` (D71) checks only unconfirmed executions (all, or those in
    *proposal_hashes*) and never stops trading; *halt* is ignored there.
    """
    day = now.astimezone(ET).date()
    if scope == "intraday":
        return _reconcile_intraday(conn, broker, day, now, run_id, proposal_hashes)
    report = ReconcileReport(day=day, at=now)
    info: AccountInfo | None = None
    positions: list[BrokerPosition] | None = None
    broker_fills: list[Fill] = []

    try:
        info = broker.account()
        report.equity, report.last_equity = info.equity, info.last_equity
        # E5.9b (D43): day P&L against Arc's prior-session close (read before this
        # run writes today's snapshot), the broker's last_equity only as fallback.
        report.baseline = start_of_day_equity(conn, day, broker_last_equity=info.last_equity)
        report.day_pnl = baseline_day_pnl(info.equity, report.baseline)
    except Exception as exc:  # noqa: BLE001 - reported as a mismatch (fail closed)
        report.mismatches.append(Mismatch(kind=MismatchKind.BROKER_ERROR, subject="account",
                                          detail=f"{type(exc).__name__}: {exc}"))  # fmt: skip
    try:
        positions = broker.positions()
    except Exception as exc:  # noqa: BLE001
        report.mismatches.append(Mismatch(kind=MismatchKind.BROKER_ERROR, subject="positions",
                                          detail=f"{type(exc).__name__}: {exc}"))  # fmt: skip
    fills_ok = True
    try:
        broker_fills = broker.fills(_day_bounds(day)[0])
        broker_fills = [f for f in broker_fills if _on_day(f.filled_at, day)]
    except Exception as exc:  # noqa: BLE001
        fills_ok = False
        report.mismatches.append(Mismatch(kind=MismatchKind.BROKER_ERROR, subject="fills",
                                          detail=f"{type(exc).__name__}: {exc}"))  # fmt: skip

    rows = OpenStructureRepo(conn).list_open()
    journal: list[dict[str, Any]] = []
    open_shares = _open_assignment_shares(conn)
    if positions is not None:
        rows = _settle_expiry(conn, broker, rows, positions, report, journal, now,
                              settle_price, open_shares, run_id)  # fmt: skip
    unwinds = _attribute_unwinds(conn, broker_fills, open_shares, report, journal, now)
    snapshot: dict[str, Any] = {}
    if positions is not None:
        expected = _expected_shares([r for r in open_shares if r.get("unwound_at") is None])
        pending = set(report.expiry_pending)
        snapshot = _check_positions(
            [r for r in rows if r["id"] not in pending], positions, report, expected
        )
        report.unrealized = sum(
            (p.unrealized_pl or Decimal(0) for p in positions if p.asset_class == "us_option"),
            start=Decimal(0),
        )
    _check_orders(conn, broker, report, journal, now, run_id)
    local = _local_fills(conn, day)
    if fills_ok:
        _check_fills(local, broker_fills, report, unwinds)
    else:
        report.fills_local = len(local)
    report.realized, report.closed_today = _realized_today(conn, day)

    with conn:
        _update_lots(conn, broker_fills, report, settings, journal)
        report.positions_snapshot_id = PositionsSnapshotRepo(conn).insert(
            positions_json=json.dumps({"day": day.isoformat(), **snapshot}, default=str),
            snapshot_at=to_db(now),
            run_id=run_id,
            commit=False,
        )
        report.pnl_snapshot_id = PnlSnapshotRepo(conn).insert(
            realized=str(report.realized),
            unrealized=str(report.unrealized),
            total=str(report.realized + report.unrealized),
            details_json=json.dumps(
                _pnl_details(report, info, virtual=getattr(broker, "is_virtual", False) is True)
            ),
            snapshot_at=to_db(now),
            run_id=run_id,
            commit=False,
        )
        store = JournalStore(conn)
        for j in journal:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject=j["subject"],
                choice=j["choice"], reason_code=j["code"], reason_text=j["text"][:2000],
                payload=j.get("payload"), at=now, run_id=run_id,
            )  # fmt: skip
        if report.clean:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject="session",
                choice=Choice.PASSED, reason_code=ReasonCode.RECONCILE_CLEAN,
                reason_text=report.summary(), at=now, run_id=run_id,
                payload={"pnl_snapshot_id": report.pnl_snapshot_id},
            )  # fmt: skip
        for m in report.mismatches:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject=m.subject,
                choice=Choice.FAILED, reason_code=ReasonCode.RECONCILE_MISMATCH,
                reason_text=m.detail[:2000], at=now, run_id=run_id,
                payload={"kind": str(m.kind), "refs": m.refs},
            )  # fmt: skip
        for m in report.notices:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject=m.subject,
                choice=Choice.NOTED, reason_code=ReasonCode.RECONCILE_TEST_FILL,
                reason_text=m.detail[:2000], at=now, run_id=run_id,
                payload={"kind": str(m.kind), "refs": m.refs},
            )  # fmt: skip

    conn.commit()  # the settle/unwind writes above share the journal's transaction
    if report.expiry_events and halt:
        _halt_opens_on_expiry(conn, report, now=now, run_id=run_id)
    if report.mismatches and halt:
        halt_on_mismatch(conn, report, now=now, run_id=run_id)
    log.info("reconcile.done", day=day.isoformat(), summary=report.summary())
    return report


def _halt_opens_on_expiry(
    conn: sqlite3.Connection, report: ReconcileReport, *, now: _dt.datetime, run_id: str | None
) -> None:
    """E11.4 (D73) Q3: a classified assignment/exercise stops new opens only."""
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    rec = HaltSwitch(HaltRepo(conn)).halt_opens_once(
        actor=EXPIRY_ACTOR,
        reason=f"assignment/exercise at reconcile {report.day}",
        now=now,
        run_id=run_id,
    )
    if rec is not None:
        report.expiry_halt_id = rec.id


def _reconcile_intraday(
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    day: _dt.date,
    now: _dt.datetime,
    run_id: str | None,
    proposal_hashes: set[str] | None,
) -> ReconcileReport:
    """D71: orders/executions of the unconfirmed ladders only; journal, never stop trading."""
    report = ReconcileReport(day=day, at=now, scope="intraday")
    rows = conn.execute(
        "SELECT proposal_hash FROM executions WHERE status = 'unconfirmed'"
    ).fetchall()
    only = {r["proposal_hash"] for r in rows}
    if proposal_hashes is not None:
        only &= proposal_hashes
    journal: list[dict[str, Any]] = []
    _check_orders(conn, broker, report, journal, now, run_id, only=only,
                  exec_states=("unconfirmed",))  # fmt: skip
    with conn:
        store = JournalStore(conn)
        for j in journal:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject=j["subject"],
                choice=j["choice"], reason_code=j["code"], reason_text=j["text"][:2000],
                payload={**(j.get("payload") or {}), "scope": "intraday"}, at=now,
                run_id=run_id,
            )  # fmt: skip
        for m in report.mismatches:
            store.record(
                persona=JournalPersona.BROKER, stage=Stage.RECONCILE, subject=m.subject,
                choice=Choice.FAILED, reason_code=ReasonCode.RECONCILE_MISMATCH,
                reason_text=m.detail[:2000], at=now, run_id=run_id,
                payload={"kind": str(m.kind), "refs": m.refs, "scope": "intraday"},
            )  # fmt: skip
    log.info("reconcile.intraday_done", checked=sorted(only), summary=report.summary())
    return report


def halt_on_mismatch(
    conn: sqlite3.Connection, report: ReconcileReport, *, now: _dt.datetime, run_id: str | None
) -> None:
    """Raise (or join) the ``arc:reconcile`` halt for *report*'s mismatches.

    One active reconcile halt at a time: a second call records the existing one.
    """
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    switch = HaltSwitch(HaltRepo(conn))
    active = [h for h in switch.state().active if h.actor == RECONCILE_ACTOR]
    if active:
        report.halt_id = active[0].id
    else:
        kinds = sorted({str(m.kind) for m in report.mismatches})
        what = "intraday reconciliation" if report.scope == "intraday" else "reconciliation"
        rec = switch.halt(
            actor=RECONCILE_ACTOR,
            reason=f"{what} {report.day}: {len(report.mismatches)} mismatch(es) "
            f"({', '.join(kinds)})",
            now=now,
            run_id=run_id,
        )
        report.halt_id = rec.id
    report.halted = True
