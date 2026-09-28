"""Investor: execute an approved proposal (E6.2; ``personas.investor``, ``trigger: approval``).

The approval service queues a routine event ``approval`` with the proposal hash
when a proposal is approved (by a click or auto-approve). The dispatcher runs
this handler for it. Deterministic (``llm: false``): it loads what the gate and
the approver saw, then hands everything to :func:`arc.execution.ladder.execute`,
which calls ``submit()`` for every step. The Investor persona never talks to the
broker any other way.

Orders are only worked while the regular session is open: an approval that
lands outside RTH (e.g. a late click) is recorded as ``rejected`` (market
closed) and never sent. An exit (``kind='close'``) closes the open structure
it was proposed for.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.routines.handlers import JobResult, JobSkippedError

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.execution.ladder import ExecutionOutcome
    from arc.models import GateDecision, Proposal
    from arc.routines.handlers import JobContext
    from arc.slack.blocks import CardView

__all__ = ["execution_card", "investor", "investor_step", "load_approved"]

log = structlog.get_logger(__name__)


def load_approved(
    conn: sqlite3.Connection, proposal_hash: str
) -> tuple[Proposal, GateDecision | None, str, str | None, str | None]:
    """``(proposal, gate decision, kind, ticker, structure_id)`` for an approved proposal.

    The proposal is the one on the approval card (re-hashed: a mismatch raises).
    """
    from arc.approvals.service import _gate_decision, _load_proposal

    req = conn.execute(
        """SELECT r.proposal_json, r.ticker, p.kind FROM approval_requests r
           JOIN proposals p ON p.proposal_hash = r.proposal_hash
           WHERE r.proposal_hash = ?""",
        (proposal_hash,),
    ).fetchone()
    if req is None:
        msg = f"no approval request for {proposal_hash[:12]}"
        raise LookupError(msg)
    proposal = _load_proposal(req["proposal_json"], proposal_hash)
    g = conn.execute(
        """SELECT proposal_hash, passed AS gate_passed, violations_json AS gate_violations,
                  token AS gate_token
           FROM gate_decisions WHERE proposal_hash = ?
           ORDER BY decided_at DESC, rowid DESC LIMIT 1""",
        (proposal_hash,),
    ).fetchone()
    decision = _gate_decision(g) if g else None
    sid = None
    if req["kind"] == "close":
        row = conn.execute(
            "SELECT id FROM open_structures WHERE exit_proposal_hash = ?", (proposal_hash,)
        ).fetchone()
        sid = row["id"] if row else None
    return proposal, decision, str(req["kind"]), req["ticker"], sid


def _refuse_closed(ctx: JobContext, phash: str, ticker: str | None, why: str) -> JobResult:
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    with ctx.conn:
        JournalStore(ctx.conn).record(
            persona=JournalPersona.INVESTOR,
            stage=Stage.ORDER,
            subject=ticker or "session",
            choice=Choice.REJECTED,
            reason_code=ReasonCode.ORDER_REFUSED,
            reason_text=why,
            proposal_hash=phash,
            at=ctx.now,
            run_id=ctx.run_id,
        )
    return JobResult(summary=f"{ticker or phash[:12]}: not executed ({why})", metrics={"orders": 0})


def execution_card(
    proposal: Proposal,
    out: ExecutionOutcome,
    *,
    ticker: str,
    kind: str,
    step_seconds: int,
    run_id: str | None = None,
) -> CardView:
    """``[Investor] Order`` card (E5.5 layout) with the ladder's :class:`ExecutionResult`.

    ``steps_used`` is the index of the filling attempt (0 = filled at mid), so the
    card reads "Filled on attempt k+1 of max_steps+1" (D28: attempts, not steps).
    """
    from arc.personas.schemas import ImprovementStep, InvestorPlan
    from arc.slack.digests import ExecutionResult, investor_card

    tick = Decimal("0.01")
    ladder = out.band.ladder(tick)
    sk = proposal.structure.kind
    structure = "close_position" if kind == "close" else (sk.value if sk else "custom")
    plan = InvestorPlan(
        ticker=ticker,
        structure_type=structure,
        order_type="limit",
        initial_limit_price=float(ladder[0]),
        improvement_steps=[
            ImprovementStep(step_number=k, price=float(p), wait_seconds=step_seconds)
            for k, p in enumerate(ladder)
            if k > 0
        ],
        timeout_seconds=step_seconds * len(ladder),
        contracts=proposal.sizing.contracts,
        notes=(
            f"One approval authorises the band {out.band.lo:+} .. {out.band.hi:+} "
            f"({out.band.max_steps} improvement steps, {step_seconds}s each); every attempt "
            "went through the gate token, the approval and a halt check."
        ),
    )
    result = ExecutionResult(
        status=out.status.value,  # type: ignore[arg-type]
        filled_qty=out.filled_qty,
        fill_price=float(out.fill_price) if out.fill_price is not None else None,
        mid_at_submit=float(ladder[0]),
        steps_used=out.steps_used or 0,
        detail=out.summary(),
    )
    return investor_card(plan, result, run_id=run_id)


def investor(
    ctx: JobContext,
    *,
    broker: BrokerAdapter,
    clock: Callable[[], _dt.datetime],
    sleep: Callable[[float], None],
    market_open: Callable[[_dt.datetime], bool],
) -> JobResult:
    from arc.approvals.service import approval_record
    from arc.execution.ladder import ExecStatus, execute
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    payload: dict[str, Any] = dict(ctx.event.payload) if ctx.event else {}
    phash = str(payload.get("proposal_hash") or "")
    if not phash:
        msg = "no approval event (investor runs on `approval` only)"
        raise JobSkippedError(msg)
    proposal, decision, kind, ticker, sid = load_approved(ctx.conn, phash)
    if not market_open(clock()):
        return _refuse_closed(ctx, phash, ticker, "market closed: orders are only worked in RTH")
    if decision is None:
        return _refuse_closed(ctx, phash, ticker, "no gate decision")
    if kind == "close" and sid is None:
        return _refuse_closed(ctx, phash, ticker, "exit has no open structure to close")
    out: ExecutionOutcome = execute(
        proposal,
        decision,
        approval_record(ctx.conn, phash),
        conn=ctx.conn,
        broker=broker,
        config=ctx.settings,
        halt=HaltSwitch(HaltRepo(ctx.conn)),
        clock=clock,
        sleep=sleep,
        kind=kind,
        structure_id=sid,
        ticker=ticker,
        run_id=ctx.run_id,
    )
    what = "exit" if kind == "close" else "entry"
    card = None
    if out.status is not ExecStatus.ALREADY:  # a replayed event posts nothing new
        card = execution_card(
            proposal,
            out,
            ticker=ticker or out.ticker,
            kind=kind,
            step_seconds=ctx.settings.execution_step_seconds,
            run_id=ctx.run_id,
        )
    return JobResult(
        card=card,
        summary=f"{ticker} {what}: {out.summary()}",
        metrics={
            "orders": len(out.attempts),
            "filled_qty": out.filled_qty,
            "status": str(out.status),
            "steps_used": out.steps_used if out.steps_used is not None else -1,
        },
        notice=f"{ticker} {what} {out.status}" + (f": {out.detail}" if out.detail else ""),
    )


def investor_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: Alpaca paper broker, wall clock, real sleep."""
    from arc.broker.alpaca_paper import AlpacaPaperBroker
    from arc.utils.calendar import is_open, now_et

    return investor(
        ctx,
        broker=AlpacaPaperBroker(),
        clock=now_et,
        sleep=time.sleep,
        market_open=is_open,
    )
