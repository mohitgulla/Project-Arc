"""The D24 price-band ladder: work one approved proposal to a fill or a cancel (E6.2).

:func:`execute` sends attempt 0 at the band's start (the gated mid limit), lets
it work for ``execution_step_seconds``, cancels it, waits until the broker
*confirms* the cancel, then sends the next step — ``max_steps`` improvement
steps, each ``1/N`` of the way to the band's worst price. Every attempt goes
through :func:`arc.execution.submission.submit` (halt re-check, token, band,
approval), so no step can leave the band or outlive a ``!halt``.

Invariants
- Never two working orders for one proposal: the next step is only sent after
  the previous attempt is confirmed terminal. An unconfirmed cancel stops the
  ladder with status ``unconfirmed`` (E6.3 reconcile resolves it).
- D71 unknown submit (E11.1): ``submit()`` raising anything but
  :class:`SubmitRefused` means the order *may* exist. It is looked up by its
  deterministic ``client_order_id`` (:mod:`arc.execution.resolve`): found = adopted
  and worked like any attempt; absent after a transport error = cancelled locally
  (``order:submit_failed``) and the *next* step goes out with its own id; an API
  4xx the lookup cannot find = rejected; lookups failing = one
  ``cancel_by_client_id``, ``unconfirmed``, and a ``reconcile.intraday`` event.
  The same ``client_order_id`` is never submitted twice. A status poll that
  raises is retried until the attempt's deadline, then goes down the cancel path.
- A partial fill that is then cancelled stops the ladder (the token is bound to
  the full quantity); the filled contracts are recorded.
- Idempotent: one ``executions`` row per proposal; a second call is a no-op.
- D32 order budget: before every attempt the day's order count is re-read
  (:func:`arc.budget.count_orders`, this ladder's own reservation excluded). If
  one more order would exceed the cap (opens: ``daily_max - close_reserve``,
  closes: ``daily_max``) the ladder stops with status ``cancelled`` and detail
  ``order budget exhausted``, even when the gate's view was stale.
- D34 freshness: when more than ``execution_max_quote_age_seconds`` passed
  between the proposal's pricing (*priced_at*) and the first attempt, the ladder
  re-prices at the current mid (*fresh_mid*) and re-anchors the band there
  (:meth:`arc.gate.band.PriceBand.reanchor`, pure gate code bound to the same
  token). A mid outside the signed band, or no usable mid, sends nothing: the
  execution ends ``cancelled`` with journal reason ``order:stale_band`` and the
  next loop may propose afresh. The band is never widened.

Every attempt is an ``orders`` row with its state events, fills go to ``fills``,
and the outcome updates the local position model (``open_structures``,
``tax_lots``) and the decision journal. The clock and sleep are injected so the
ladder is fully testable without waiting.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from arc.budget.orders import REFUSED_DETAIL_PREFIX, OrderBudgetConfig, can_submit, count_orders
from arc.context.ttl import to_db
from arc.execution.fills import apply_fill, fill_net_price, record_fill
from arc.execution.resolve import (
    ResolvedSubmit,
    api_status_code,
    is_transport_error,
    resolve_unknown_submit,
)
from arc.execution.submission import SubmitRefused, attempt_order_id, submit
from arc.gate.band import PriceBand
from arc.gate.rules import proposal_hash as hash_proposal
from arc.gate.ticks import TickGrid, legs_grid
from arc.gate.token import BandToken, TokenError, parse_any
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.models import OrderState
from arc.store.execution import ExecutionRepo
from arc.store.repos import OrderRepo
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from decimal import Decimal

    from arc.broker.base import BrokerAdapter, BrokerOrderStatus
    from arc.config import ArcSettings
    from arc.gate.halt import HaltSwitch
    from arc.models import ApprovalRecord, GateDecision, Proposal

__all__ = [
    "AttemptRecord",
    "ExecStatus",
    "ExecutionAdoptedError",
    "ExecutionOutcome",
    "LadderContext",
    "execute",
    "fill_net_price",
    "resume_attempt",
]

log = structlog.get_logger(__name__)

_ACTOR = "arc:execution"
_FILLED = {"filled"}
_DONE_NO_FILL = {"canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"}
_REJECTED = {"rejected"}


class ExecutionAdoptedError(RuntimeError):
    """E11.2 (D72): another run (``broker.reattach``) adopted this execution.

    Raised by the ladder's fence before any further write; the ladder stops
    without touching orders, fills or the execution row.
    """

    def __init__(self, proposal_hash: str, adopted_by: str) -> None:
        self.proposal_hash = proposal_hash
        self.adopted_by = adopted_by
        super().__init__(f"execution {proposal_hash[:12]} adopted by {adopted_by}")


class ExecStatus(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNCONFIRMED = "unconfirmed"
    ALREADY = "already_executed"


@dataclass
class AttemptRecord:
    step: int
    limit_price: Decimal
    client_order_id: str
    order_id: str
    broker_order_id: str | None = None
    status: str = "approved"
    filled_qty: int = 0
    fill_price: Decimal | None = None
    adopted: bool = False  # D71: found by client_order_id after a submit error
    detail: str = ""


@dataclass
class ExecutionOutcome:
    proposal_hash: str
    status: ExecStatus
    band: PriceBand
    ticker: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)
    filled_qty: int = 0
    fill_price: Decimal | None = None
    steps_used: int | None = None
    structure_id: str | None = None
    detail: str = ""

    def summary(self) -> str:
        prices = ", ".join(f"s{a.step} {a.limit_price:+}" for a in self.attempts) or "none"
        text = f"{self.status}: {len(self.attempts)} attempt(s) [{prices}]"
        if self.filled_qty:
            text += f"; filled {self.filled_qty} @ {self.fill_price:+}"
        return text + (f" ({self.detail})" if self.detail else "")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _band_of(decision: GateDecision, proposal: Proposal) -> tuple[PriceBand, str]:
    """The band to walk and the token version. arc1 = one attempt at the exact limit."""
    limit = (
        proposal.structure.net_debit_credit
        if proposal.limit_price is None
        else proposal.limit_price
    )
    token = decision.token or ""
    try:
        t = parse_any(token)
    except TokenError:
        # submit() refuses it with a clear reason; walk a single attempt.
        return PriceBand(lo=limit, hi=limit, max_steps=0), "invalid"
    if isinstance(t, BandToken):
        try:
            return t.band, t.version
        except ValueError:
            return PriceBand(lo=limit, hi=limit, max_steps=0), t.version
    return PriceBand(lo=limit, hi=limit, max_steps=0), t.version


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


@dataclass
class LadderContext:
    """What one ladder (or one ``broker.reattach`` adoption, E11.2) works with.

    ``proposal`` / ``decision`` / ``approval`` / ``halt`` are only needed to *send*
    an attempt; the re-attach never sends, so it builds one without them.
    """

    conn: sqlite3.Connection
    broker: BrokerAdapter
    config: ArcSettings
    clock: Callable[[], _dt.datetime]
    sleep: Callable[[float], None]
    run_id: str | None
    phash: str
    ticker: str
    halt: HaltSwitch | None = None
    proposal: Proposal | None = None
    decision: GateDecision | None = None
    approval: ApprovalRecord | None = None
    actor: str = _ACTOR
    heartbeat: Callable[[], None] | None = None
    stale_detail: str = ""

    @property
    def orders(self) -> OrderRepo:
        return OrderRepo(self.conn)

    def beat(self) -> None:
        """E11.2: tell the run's liveness record this ladder is still working."""
        if self.heartbeat is None:
            return
        try:
            self.heartbeat()
        except Exception as exc:  # noqa: BLE001 - a missed beat never stops an order
            log.warning("execution.heartbeat_failed", proposal_hash=self.phash, error=str(exc))

    def fence(self) -> None:
        """E11.2 (D72): stop before any write once another run adopted this execution."""
        row = self.conn.execute(
            "SELECT adopted_by_run_id FROM executions WHERE proposal_hash = ?", (self.phash,)
        ).fetchone()
        adopted = row[0] if row is not None else None
        if adopted and adopted != self.run_id:
            raise ExecutionAdoptedError(self.phash, str(adopted))

    def journal(self, choice: Choice, code: ReasonCode, text: str, **payload: Any) -> None:
        self.fence()
        with self.conn:
            JournalStore(self.conn).record(
                persona=JournalPersona.BROKER,
                stage=Stage.ORDER,
                subject=self.ticker,
                choice=choice,
                reason_code=code,
                reason_text=text[:2000],
                proposal_hash=self.phash,
                payload={k: v for k, v in payload.items() if v is not None},
                at=self.clock(),
                run_id=self.run_id,
            )

    def move(self, order_id: str, to: OrderState, detail: str = "") -> None:
        self.fence()
        row = self.orders.get(order_id)
        if row is not None and row["state"] == to.value:
            return  # E11.2: a re-attach finishing a half-recorded move
        self.orders.transition(
            order_id=order_id,
            to_state=to,
            actor=self.actor,
            detail=detail,
            event_at=to_db(self.clock()),
            run_id=self.run_id,
        )


_Ctx = LadderContext


def _poll(c: _Ctx, broker_id: str, seconds: float) -> BrokerOrderStatus | None:
    """Poll until the order is terminal or *seconds* pass; return the last status read.

    D71: a poll that raises (timeout, dropped connection, API error) is logged and
    retried until the deadline; ``None`` when no poll in the window answered.
    E11.2: every iteration beats the run's heartbeat and checks the adoption fence.
    """
    deadline = c.clock() + _dt.timedelta(seconds=seconds)
    last: BrokerOrderStatus | None = None
    while True:
        c.beat()
        c.fence()
        try:
            last = c.broker.order_status(broker_id)
        except Exception as exc:  # noqa: BLE001 - retried within the deadline
            log.warning(
                "execution.poll_error",
                broker_order_id=broker_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if last.status in _FILLED | _DONE_NO_FILL:
                return last
        if c.clock() >= deadline:
            return last
        c.sleep(c.config.execution_poll_seconds)


def _record_fill(c: _Ctx, a: AttemptRecord, st: BrokerOrderStatus) -> None:
    c.fence()
    qty, price = record_fill(
        c.conn,
        order_id=a.order_id,
        status=st,
        limit_price=a.limit_price,
        now=c.clock(),
        run_id=c.run_id,
    )
    if qty:
        a.filled_qty, a.fill_price = qty, price


def _attempt(c: _Ctx, step: int, price: Decimal) -> tuple[AttemptRecord, str]:
    """Send and work one attempt. Returns the record and a verdict:
    ``filled`` | ``partial`` | ``next`` | ``refused`` | ``rejected`` | ``unconfirmed`` |
    ``submit_failed`` (D71)."""
    if c.proposal is None or c.decision is None or c.halt is None:
        msg = "a ladder attempt needs the proposal, its gate decision and the halt switch"
        raise ValueError(msg)
    c.beat()
    token = c.decision.token or ""
    try:
        coid = attempt_order_id(token, step)
    except TokenError:
        coid = f"invalid-token.{c.phash[:16]}.s{step}"
    order_id = c.orders.create(
        proposal_hash=c.phash,
        client_order_id=coid,
        run_id=c.run_id,
        created_at=to_db(c.clock()),
    )
    a = AttemptRecord(step=step, limit_price=price, client_order_id=coid, order_id=order_id)
    c.move(order_id, OrderState.GATED, "gate passed (token issued)")
    c.move(order_id, OrderState.APPROVED, f"approved; step {step} limit {price:+}")
    ExecutionRepo(c.conn).attempt(c.phash)
    c.fence()  # E11.2: never send once another run adopted this execution
    try:
        broker_id = submit(
            c.proposal,
            c.decision,
            c.approval,
            broker=c.broker,
            config=c.config,
            now=c.clock(),
            halt=c.halt,
            step=step,
            limit_price=price,
        )
    except SubmitRefused as exc:
        c.move(order_id, OrderState.CANCELLED, f"{REFUSED_DETAIL_PREFIX}: {exc}")
        a.status = "refused"
        c.journal(
            Choice.REJECTED,
            ReasonCode.ORDER_REFUSED,
            str(exc),
            step=step,
            limit=str(price),
            refusal=str(exc.code),
        )
        return a, "refused"
    except Exception as exc:  # noqa: BLE001 - D71: the order may exist; resolve it
        return a, _unknown_submit(c, a, exc)
    return a, _work(c, a, broker_id)


def _work(c: _Ctx, a: AttemptRecord, broker_id: str, first: BrokerOrderStatus | None = None) -> str:
    """Record the broker id, let the attempt work, cancel it if needed, settle it."""
    a.broker_order_id = broker_id
    c.orders.set_broker_order_id(a.order_id, broker_id)
    how = "adopted after submit error; " if a.adopted else ""
    c.move(a.order_id, OrderState.SUBMITTED, f"{how}broker {broker_id}")
    c.journal(
        Choice.SUBMITTED,
        ReasonCode.ORDER_STEP,
        f"step {a.step}: limit {a.limit_price:+} ({a.client_order_id[-4:]})"
        + (" adopted by client id after a submit error" if a.adopted else ""),
        step=a.step,
        limit=str(a.limit_price),
        broker_order_id=broker_id,
        client_order_id=a.client_order_id,
        adopted=True if a.adopted else None,
    )

    terminal = _FILLED | _DONE_NO_FILL
    st = first if first is not None and first.status in terminal else None
    if st is None:
        st = _poll(c, broker_id, c.config.execution_step_seconds)
    if st is None or st.status not in terminal:
        try:
            c.broker.cancel(broker_id)
        except Exception as exc:  # noqa: BLE001 - it may have filled/closed meanwhile
            log.warning("execution.cancel_error", broker_order_id=broker_id, error=str(exc))
        st = _poll(c, broker_id, c.config.execution_cancel_confirm_seconds)
    if st is None:
        a.status = "unconfirmed"
        a.detail = f"order {broker_id} status unreadable after the cancel; ladder stopped"
        c.journal(
            Choice.FAILED,
            ReasonCode.ORDER_UNCONFIRMED,
            a.detail,
            step=a.step,
            broker_order_id=broker_id,
        )
        return "unconfirmed"
    return _settle(c, a, st)


def _unknown_submit(c: _Ctx, a: AttemptRecord, exc: Exception) -> str:
    """D71: ``submit()`` raised after the halt/token/approval checks; find out what happened.

    Verdicts: the normal ones for an adopted order; ``next`` (absent after a
    transport error), ``submit_failed`` (absent after any other error),
    ``rejected`` (API 4xx the broker does not hold) or ``unconfirmed``.
    """
    error = f"{type(exc).__name__}: {exc}"[:500]
    code = api_status_code(exc)
    transport = is_transport_error(exc)
    rejected_4xx = code is not None and 400 <= code < 500
    log.error(
        "execution.submit_error",
        proposal_hash=c.phash,
        step=a.step,
        error=error,
        status_code=code,
        transport=transport,
    )
    # A 4xx is an answer (e.g. 422 "client_order_id must be unique" when a retried
    # POST already landed): one lookup tells which; no lookup loop.
    res: ResolvedSubmit = resolve_unknown_submit(
        c.broker,
        client_order_id=a.client_order_id,
        attempts=1 if rejected_4xx else c.config.execution_unknown_submit_lookups,
        sleep=c.sleep,
        wait_seconds=c.config.execution_poll_seconds,
    )
    facts: dict[str, Any] = {
        "step": a.step,
        "limit": str(a.limit_price),
        "client_order_id": a.client_order_id,
        "lookups": res.lookups,
        "outcome": res.outcome,
        "submit_error": error,
        "lookup_error": res.error or None,
        "status_code": code,
    }
    if res.outcome == "accepted" and res.broker_order_id:
        a.adopted = True
        log.warning(
            "execution.submit_adopted",
            proposal_hash=c.phash,
            step=a.step,
            broker_order_id=res.broker_order_id,
        )
        return _work(c, a, res.broker_order_id, first=res.status)
    if res.outcome == "absent" and rejected_4xx:
        a.status = "rejected"
        a.detail = f"broker rejected step {a.step} on submit: {error}"
        c.move(a.order_id, OrderState.CANCELLED, a.detail)
        c.journal(Choice.REJECTED, ReasonCode.ORDER_REJECTED, a.detail, **facts)
        return "rejected"
    if res.outcome == "absent":
        a.status = "cancelled"
        a.detail = f"submit failed (broker has no order {a.client_order_id[-4:]}): {error}"
        c.move(a.order_id, OrderState.CANCELLED, a.detail)
        c.journal(Choice.FAILED, ReasonCode.ORDER_SUBMIT_FAILED, a.detail, **facts)
        return "next" if transport else "submit_failed"
    a.status = "unconfirmed"
    a.detail = (
        f"broker error on submit ({error}); order state unknown after {res.lookups} "
        f"client-id lookup(s); cancel by client id {'sent' if res.cancel_sent else 'not sent'}"
    )
    c.journal(
        Choice.FAILED,
        ReasonCode.ORDER_UNCONFIRMED,
        a.detail,
        **facts,
        cancel_sent=res.cancel_sent,
    )
    return "unconfirmed"


def _settle(c: _Ctx, a: AttemptRecord, st: BrokerOrderStatus) -> str:
    a.status = st.status
    qty = int(st.filled_qty)
    if st.status in _FILLED:
        _record_fill(c, a, st)
        c.move(a.order_id, OrderState.FILLED, f"filled {qty} @ {a.fill_price}")
        return "filled"
    if st.status not in _DONE_NO_FILL:
        c.journal(
            Choice.FAILED,
            ReasonCode.ORDER_UNCONFIRMED,
            f"cancel not confirmed (broker status {st.status}); ladder stopped",
            step=a.step,
            broker_order_id=a.broker_order_id,
        )
        return "unconfirmed"
    if qty > 0:
        _record_fill(c, a, st)
        c.move(a.order_id, OrderState.PARTIALLY_FILLED, f"filled {qty} @ {a.fill_price}")
        c.move(a.order_id, OrderState.CANCELLED, f"remainder {st.status}")
        return "partial"
    if st.status in _REJECTED:
        c.move(a.order_id, OrderState.REJECTED, "broker rejected")
        c.journal(
            Choice.REJECTED,
            ReasonCode.ORDER_REJECTED,
            f"broker rejected step {a.step} at {a.limit_price:+}",
            step=a.step,
        )
        return "rejected"
    c.move(a.order_id, OrderState.CANCELLED, f"no fill in time ({st.status})")
    c.journal(
        Choice.CANCELLED,
        ReasonCode.ORDER_TIMEOUT,
        f"step {a.step} at {a.limit_price:+}: no fill, cancelled",
        step=a.step,
    )
    return "next"


def _fresh_band(
    c: _Ctx,
    band: PriceBand,
    grid: TickGrid,
    *,
    priced_at: _dt.datetime | None,
    fresh_mid: Callable[[], Decimal | None] | None,
) -> PriceBand | None:
    """D34: the band to walk; ``None`` (nothing sent) when the re-priced mid left it."""
    if priced_at is None or fresh_mid is None:
        return band
    age = (c.clock() - priced_at).total_seconds()
    max_age = c.config.execution_max_quote_age_seconds
    if age <= max_age:
        return band
    try:
        mid = fresh_mid()
    except Exception as exc:  # noqa: BLE001 - no quote = fail closed, never widen
        log.warning("execution.reprice_failed", proposal_hash=c.phash, error=str(exc))
        mid = None
    walk = band.reanchor(mid, grid) if mid is not None else None
    if walk is None:
        why = f"mid {mid:+}" if mid is not None else "no usable mid"
        c.stale_detail = (
            f"stale band: quotes {age:.0f}s old (> {max_age}s), {why} outside the gate band "
            f"{band.lo:+} .. {band.hi:+}; not sent"
        )
        c.journal(
            Choice.NO_TRADE,
            ReasonCode.ORDER_STALE_BAND,
            c.stale_detail,
            quote_age_s=int(age),
            mid=str(mid) if mid is not None else None,
            band_lo=str(band.lo),
            band_hi=str(band.hi),
        )
        log.warning("execution.stale_band", proposal_hash=c.phash, age_s=int(age), mid=str(mid))
        return None
    if walk != band:
        c.journal(
            Choice.SELECTED,
            ReasonCode.ORDER_STEP,
            f"re-priced at mid {mid:+} after {age:.0f}s: band now {walk.lo:+} .. {walk.hi:+}",
            quote_age_s=int(age),
            mid=str(mid),
            band_lo=str(walk.lo),
            band_hi=str(walk.hi),
        )
        log.info("execution.repriced", proposal_hash=c.phash, age_s=int(age), lo=str(walk.lo))
    return walk


def _budget_blocks(c: _Ctx, kind: str) -> str | None:
    """D32: re-count the day's orders; the reason the next attempt may not be sent, or None."""
    cfg = OrderBudgetConfig.from_settings(c.config)
    day = c.clock().astimezone(ET).date()
    count = count_orders(c.conn, c.broker, day, exclude_proposal_hash=c.phash)
    if can_submit(count.used, cfg, kind=kind, attempts=1):
        return None
    cap = cfg.daily_max if kind == "close" else cfg.open_limit
    return f"order budget exhausted ({count.used} of {cap} {kind} orders used today)"


def execute(
    proposal: Proposal,
    decision: GateDecision,
    approval: ApprovalRecord | None,
    *,
    conn: sqlite3.Connection,
    broker: BrokerAdapter,
    config: ArcSettings,
    halt: HaltSwitch,
    clock: Callable[[], _dt.datetime],
    sleep: Callable[[float], None],
    kind: str = "open",
    structure_id: str | None = None,
    ticker: str | None = None,
    run_id: str | None = None,
    priced_at: _dt.datetime | None = None,
    fresh_mid: Callable[[], Decimal | None] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> ExecutionOutcome:
    """Work *proposal* through its price band; record orders, fills and the position.

    ``kind='close'`` closes ``structure_id`` (an ``open_structures`` row) on a fill.
    ``priced_at`` + ``fresh_mid`` enable the D34 re-price (see the module docstring).
    ``heartbeat`` (E11.2) is called before each attempt and on every poll.

    Raises :class:`ExecutionAdoptedError` (after writing nothing more) when a
    ``broker.reattach`` run adopted this execution while it was being worked.
    """
    phash = hash_proposal(proposal)
    band, version = _band_of(decision, proposal)
    t = ticker or parse_occ(proposal.structure.legs[0].occ_symbol).root
    c = _Ctx(
        conn=conn,
        broker=broker,
        config=config,
        clock=clock,
        sleep=sleep,
        run_id=run_id,
        phash=phash,
        ticker=t,
        halt=halt,
        proposal=proposal,
        decision=decision,
        approval=approval,
        heartbeat=heartbeat,
    )
    try:
        return _execute(c, band, version, kind, structure_id, priced_at, fresh_mid)
    except ExecutionAdoptedError as exc:
        log.warning("execution.adopted_elsewhere", proposal_hash=phash, adopted_by=exc.adopted_by)
        raise


def _execute(
    c: _Ctx,
    band: PriceBand,
    version: str,
    kind: str,
    structure_id: str | None,
    priced_at: _dt.datetime | None,
    fresh_mid: Callable[[], Decimal | None] | None,
) -> ExecutionOutcome:
    assert c.proposal is not None  # noqa: S101 - execute() always sets it
    conn, config, clock, run_id = c.conn, c.config, c.clock, c.run_id
    phash, t, proposal = c.phash, c.ticker, c.proposal
    execs = ExecutionRepo(conn)
    claimed = execs.start(
        proposal_hash=phash,
        kind=kind,
        token_version=version,
        band_lo=band.lo,
        band_hi=band.hi,
        max_steps=band.max_steps,
        contracts=proposal.sizing.contracts,
        now=clock(),
        structure_id=structure_id,
        run_id=run_id,
    )
    if not claimed:
        return ExecutionOutcome(phash, ExecStatus.ALREADY, band, t, detail="already executed")

    out = ExecutionOutcome(phash, ExecStatus.CANCELLED, band, t)
    grid = legs_grid(proposal.structure.legs, config.ticks)  # D66: the order's exchange grid
    walk = _fresh_band(c, band, grid, priced_at=priced_at, fresh_mid=fresh_mid)
    ladder = walk.ladder(grid) if walk is not None else ()
    if walk is None:
        out.detail = c.stale_detail
    elif not ladder:
        out.detail = (
            f"band {walk.lo:+} .. {walk.hi:+} holds no exchange-valid price "
            f"({grid.describe(walk.lo)}); not sent"
        )
    for step, price in enumerate(ladder):
        blocked = _budget_blocks(c, kind)
        if blocked is not None:
            out.status, out.detail = ExecStatus.CANCELLED, blocked
            c.journal(Choice.NO_TRADE, ReasonCode.ORDER_BUDGET_STOP, blocked, step=step, kind=kind)
            log.warning("execution.order_budget_stop", proposal_hash=phash, step=step, kind=kind)
            break
        a, verdict = _attempt(c, step, price)
        out.attempts.append(a)
        if verdict == "next":
            continue
        if verdict in ("filled", "partial"):
            out.status = ExecStatus.FILLED if verdict == "filled" else ExecStatus.PARTIALLY_FILLED
            out.filled_qty, out.fill_price, out.steps_used = a.filled_qty, a.fill_price, step
        elif verdict == "unconfirmed":
            out.status = ExecStatus.UNCONFIRMED
            out.detail = a.detail
        elif verdict == "submit_failed":
            out.status, out.detail = ExecStatus.CANCELLED, a.detail
        else:  # refused / rejected
            out.status = ExecStatus.REJECTED
        break
    else:
        if ladder:
            out.detail = f"no fill after {len(ladder)} attempt(s); last {ladder[-1]:+}"

    c.fence()
    if out.filled_qty:
        out.structure_id = _apply_fill(c, out, kind=kind, structure_id=structure_id)
    execs.finish(
        phash,
        status=str(out.status),
        now=clock(),
        filled_qty=out.filled_qty,
        fill_price=out.fill_price,
        steps_used=out.steps_used,
        structure_id=out.structure_id,
        detail=out.detail or out.summary(),
    )
    if out.filled_qty:
        full = out.status is ExecStatus.FILLED
        c.journal(
            Choice.FILLED,
            ReasonCode.ORDER_FILLED if full else ReasonCode.ORDER_PARTIAL,
            out.summary(),
            filled_qty=out.filled_qty,
            fill_price=str(out.fill_price),
            steps_used=out.steps_used,
            band_lo=str(band.lo),
            band_hi=str(band.hi),
        )
    if out.status is ExecStatus.UNCONFIRMED and config.execution_intraday_reconcile:
        from arc.reconcile.intraday import queue_intraday_reconcile

        queue_intraday_reconcile(conn, phash, reason=out.detail or out.summary(), now=clock())
    log.info("execution.done", proposal_hash=phash, kind=kind, summary=out.summary())
    return out


def _apply_fill(c: _Ctx, out: ExecutionOutcome, *, kind: str, structure_id: str | None) -> str:
    assert out.fill_price is not None  # noqa: S101 - only called with a fill
    assert c.proposal is not None  # noqa: S101 - execute() always sets it
    return apply_fill(
        c.conn,
        kind=kind,
        phash=c.phash,
        ticker=c.ticker,
        candidate_id=c.proposal.candidate_id,
        structure=c.proposal.structure,
        order_id=out.attempts[-1].order_id,
        filled_qty=out.filled_qty,
        fill_price=out.fill_price,
        structure_id=structure_id,
        now=c.clock(),
        run_id=c.run_id,
    )


# ---------------------------------------------------------------------------
# E11.2 (D72): finish an attempt whose ladder process died
# ---------------------------------------------------------------------------


def resume_attempt(c: _Ctx, a: AttemptRecord, status: BrokerOrderStatus | None) -> str:
    """Finish attempt *a* where it stands, exactly as the ladder would; never submits.

    *status* is the broker's current view of the order (``None`` = unreadable).
    A terminal status is settled at once; a working one is cancelled and the
    cancel confirmed within ``execution_cancel_confirm_seconds``. Returns the
    ladder's verdict: ``filled`` | ``partial`` | ``next`` (cancelled, no fill) |
    ``rejected`` | ``unconfirmed``.
    """
    terminal = _FILLED | _DONE_NO_FILL
    bid = a.broker_order_id or (status.broker_order_id if status is not None else None)
    if bid is None:
        msg = "resume_attempt needs the broker order id"
        raise ValueError(msg)
    a.broker_order_id = bid
    st = status
    if st is None or st.status not in terminal:
        try:
            c.broker.cancel(bid)
        except Exception as exc:  # noqa: BLE001 - it may have filled/closed meanwhile
            log.warning("execution.cancel_error", broker_order_id=bid, error=str(exc))
        polled = _poll(c, bid, c.config.execution_cancel_confirm_seconds)
        st = polled if polled is not None else st
    if st is None:
        a.status = "unconfirmed"
        a.detail = f"order {bid} status unreadable after the cancel"
        c.journal(
            Choice.FAILED, ReasonCode.ORDER_UNCONFIRMED, a.detail, step=a.step, broker_order_id=bid
        )
        return "unconfirmed"
    if st.status not in terminal:
        a.status = st.status
        a.detail = f"cancel not confirmed (broker status {st.status})"
    return _settle(c, a, st)
