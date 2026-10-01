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

D34 in-chain Execute (:func:`execute_step`, chain step ``execute`` right after
``propose`` / ``risk.reallocate``): publishes this chain's proposals through the
approval service (the same path as the tick sweep, so cards, journal and
``approval_id`` are identical), and when ``auto_approve`` is on for the running
environment, hands every auto-approved proposal to an Investor **subprocess**
(``arc routines run investor --event <id>``) that joins the chain run but holds
its own per-event lock, never the LLM lock. A D24 ladder takes ~6 min; running it
inline would block every loop. With auto-approve off the step is a no-op
("awaiting approval"). Freshness: the ladder re-prices at the current mid when the
proposal is older than ``execution_max_quote_age_seconds`` (pure band code,
:meth:`arc.gate.band.PriceBand.reanchor`).
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.routines.handlers import JobResult, JobSkippedError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from arc.approvals.service import ApprovalService
    from arc.broker.base import BrokerAdapter
    from arc.data.base import MarketDataProvider
    from arc.execution.ladder import ExecutionOutcome
    from arc.models import GateDecision, Proposal
    from arc.routines.handlers import JobContext, RunEnv
    from arc.slack.blocks import CardView

__all__ = [
    "execute_step",
    "execution_card",
    "fresh_mid_of",
    "investor",
    "investor_step",
    "load_approved",
    "spawn_investor",
]

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


LAPSED_UNDER_HALT = "approval lapsed under halt"
# E6.2e: a dispatched approval whose Investor never started, reclaimed past its TTL.
LAPSED_NOT_STARTED = "approval lapsed: the Investor never started"


def approval_deadline(conn: sqlite3.Connection, proposal_hash: str) -> _dt.datetime | None:
    """Until when an approved proposal may still execute: its ``proposals.expires_at``.

    E6.2d: an approval event that arrives while halted waits (deferred) until this
    instant; ``None`` when the proposal is unknown or the time is unreadable.
    """
    row = conn.execute(
        "SELECT expires_at FROM proposals WHERE proposal_hash = ?", (proposal_hash,)
    ).fetchone()
    if row is None or not row["expires_at"]:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_dt.UTC)


def lapse_approval(
    conn: sqlite3.Connection,
    proposal_hash: str,
    *,
    now: _dt.datetime,
    run_id: str | None,
    settings: Any,
    slack: bool,
    chain_run_id: str | None = None,
    reason: str = LAPSED_UNDER_HALT,
) -> None:
    """E6.2d: an approval that stayed halted past its TTL: journal the refusal, update the card.

    The journal row is ``Stage.ORDER`` / ``order:refused`` with the text *reason*
    (:data:`LAPSED_UNDER_HALT`, or E6.2e :data:`LAPSED_NOT_STARTED` for a reclaimed
    dispatch); nothing is ever sent to the broker. The card edit is best-effort
    (the journal row is the record).
    """
    from arc.approvals.cli import make_service
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    row = conn.execute(
        "SELECT ticker FROM approval_requests WHERE proposal_hash = ?", (proposal_hash,)
    ).fetchone()
    ticker = row["ticker"] if row else None
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.INVESTOR,
            stage=Stage.ORDER,
            subject=ticker or "session",
            choice=Choice.REJECTED,
            reason_code=ReasonCode.ORDER_REFUSED,
            reason_text=reason,
            proposal_hash=proposal_hash,
            at=now,
            run_id=run_id,
            chain_run_id=chain_run_id,
        )
    try:
        make_service(conn, settings, slack=slack).mark_not_executed(
            proposal_hash, reason=reason, now=now
        )
    except Exception as exc:  # noqa: BLE001 - the refusal is journaled; the card edit is cosmetic
        log.warning("investor.lapse_card_failed", proposal_hash=proposal_hash, error=str(exc))
    log.info("investor.approval_lapsed", proposal_hash=proposal_hash, run_id=run_id, why=reason)


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


def priced_at_of(conn: sqlite3.Connection, proposal_hash: str) -> _dt.datetime | None:
    """When the proposal was priced (its ``proposals.created_at``), tz-aware, else None."""
    row = conn.execute(
        "SELECT created_at FROM proposals WHERE proposal_hash = ?", (proposal_hash,)
    ).fetchone()
    if row is None or not row["created_at"]:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_dt.UTC)


def fresh_mid_of(
    market: MarketDataProvider, proposal: Proposal, *, as_of: _dt.date, r: float
) -> Callable[[], Decimal | None]:
    """A thunk that re-prices the proposal's legs at the current mid (D34 freshness).

    Uses :func:`arc.pipeline.market.price_structure` with ``require_iv=False``: an
    execution needs mids only. ``None`` when any leg has no usable quote.
    """
    from arc.pipeline.market import price_structure

    legs = [(leg.occ_symbol, leg.side, leg.ratio) for leg in proposal.structure.legs]

    def _mid() -> Decimal | None:
        try:
            priced = price_structure(market, legs, as_of=as_of, r=r, require_iv=False)
        except LookupError as exc:
            log.warning("investor.reprice_no_quote", error=str(exc))
            return None
        return Decimal(priced.structure.net_debit_credit)

    return _mid


def _refresh_root(ctx: JobContext, phash: str) -> None:
    """D36: after a fill (or a failed ladder) re-render the loop's root line.

    The Investor runs in its own process with no card poster of its own; with
    Slack on it edits through a :class:`SlackCardPoster`, otherwise the update
    goes to the log (nothing to edit).
    """
    from arc.approvals.cli import make_service

    try:
        make_service(ctx.conn, ctx.settings, slack=ctx.run_env.slack).refresh_loop_root(phash)
    except Exception as exc:  # noqa: BLE001 - the fill is recorded; the root edit is best-effort
        log.warning("investor.loop_root_refresh_failed", proposal_hash=phash, error=str(exc))


def investor(
    ctx: JobContext,
    *,
    broker: BrokerAdapter,
    clock: Callable[[], _dt.datetime],
    sleep: Callable[[float], None],
    market_open: Callable[[_dt.datetime], bool],
    market: MarketDataProvider | None = None,
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
    # D34 freshness: the ladder re-prices when the proposal is older than the max
    # quote age; without a market data provider the band is walked as approved.
    fresh_mid = None
    if market is not None:
        fresh_mid = fresh_mid_of(
            market, proposal, as_of=clock().date(), r=ctx.settings.scanner_risk_free_rate
        )
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
        priced_at=priced_at_of(ctx.conn, phash) if market is not None else None,
        fresh_mid=fresh_mid,
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
        _refresh_root(ctx, phash)
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
    from arc.data.alpaca import AlpacaMarketData
    from arc.utils.calendar import is_open, now_et

    return investor(
        ctx,
        broker=AlpacaPaperBroker(),
        clock=now_et,
        sleep=time.sleep,
        market_open=is_open,
        market=AlpacaMarketData(),
    )


# -- D34: in-chain Execute -----------------------------------------------------


def chain_proposals(conn: sqlite3.Connection, chain_run_id: str) -> list[str]:
    """Proposal hashes written by any run of *chain_run_id*, highest rank first.

    Rank = the propose step's order (``proposals.rowid``): the Director's shortlist
    is ranked, and propose inserts in that order.
    """
    rows = conn.execute(
        """SELECT p.proposal_hash FROM proposals p
           JOIN routine_runs r ON r.run_id = p.run_id
           WHERE r.chain_run_id = ? ORDER BY p.rowid""",
        (chain_run_id,),
    ).fetchall()
    return [r["proposal_hash"] for r in rows]


def approval_events(conn: sqlite3.Connection, proposal_hashes: Sequence[str]) -> dict[str, str]:
    """``{proposal_hash: routine_events.id}`` of the pending ``approval`` events.

    Pending = neither consumed nor already dispatched (E6.2d).
    """
    out: dict[str, str] = {}
    rows = conn.execute(
        """SELECT id, payload FROM routine_events
           WHERE name = 'approval' AND consumed_at IS NULL AND dispatched_at IS NULL
           ORDER BY created_at, rowid"""
    ).fetchall()
    import json

    wanted = set(proposal_hashes)
    for r in rows:
        ph = str(json.loads(r["payload"]).get("proposal_hash") or "")
        if ph in wanted and ph not in out:
            out[ph] = r["id"]
    return out


def investor_command(
    env: RunEnv, event_id: str, *, chain_run_id: str, parent_run_id: str
) -> list[str]:
    """The ``arc routines run investor --event`` argv a spawned ladder runs with."""
    # Same interpreter as this process; `arc.cli:main` is the `arc` console script.
    argv = [sys.executable, "-c", "from arc.cli import main; raise SystemExit(main())"]
    argv += ["routines", "run", "investor", "--event", event_id]
    argv += ["--chain-run-id", chain_run_id, "--parent-run-id", parent_run_id]
    if env.db_path:
        argv += ["--db", env.db_path]
    if env.config_path:
        argv += ["--config", env.config_path]
    if env.lock_dir:
        argv += ["--lock-dir", env.lock_dir]
    if not env.slack:
        argv.append("--no-slack")
    return argv


def spawn_investor(argv: Sequence[str]) -> int:
    """Start the Investor subprocess detached (its own session); returns the pid."""
    proc = subprocess.Popen(  # noqa: S603 - argv is built from our own constants
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def execute_step(
    ctx: JobContext,
    *,
    spawn: Callable[[Sequence[str]], int] = spawn_investor,
    service: ApprovalService | None = None,
) -> JobResult:
    """Chain step ``execute`` (D34): publish + auto-approve, then hand off ladders.

    Deterministic (``llm: false``). Returns at once; each ladder runs in its own
    ``arc routines run investor --event <id>`` process joined to this chain.

    Without Slack (``--no-slack`` / dry run) nothing is published, like the tick's
    sweep: a card published to the log would be stranded with no button to click.
    The proposals then wait for a tick that can post. Tests pass a *service* with a
    recording poster.
    """
    from arc.approvals.cli import make_service
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    if not ctx.chain_run_id:
        msg = "execute runs only as a chain step (after propose)"
        raise JobSkippedError(msg)
    hashes = chain_proposals(ctx.conn, ctx.chain_run_id)
    settings = ctx.settings
    if service is None and not ctx.run_env.slack:
        return JobResult(
            summary=f"{len(hashes)} proposal(s) not published (no Slack); awaiting the next tick",
            metrics={"proposals": len(hashes), "published": 0, "auto_approved": 0, "dispatched": 0},
        )
    svc = service if service is not None else make_service(ctx.conn, settings, slack=True)
    # Same code path as the tick's sweep, restricted to this chain's proposals; the
    # later sweep finds nothing left for them (approval_requests exists per hash).
    report = svc.publish_pending(ctx.now, only=hashes)
    auto_on = bool(settings.auto_approve) or bool(settings.auto_exit_defined_risk)
    env = settings.env.value
    metrics: dict[str, Any] = {
        "proposals": len(hashes),
        "published": len(report.published),
        "auto_approved": len(report.auto_approved),
        "dispatched": 0,
        "auto_approve": int(bool(settings.auto_approve)),
    }
    if not hashes:
        return JobResult(summary="no proposals to execute", metrics=metrics)
    if not auto_on or not report.auto_approved:
        why = "auto-approve off" if not auto_on else "nothing auto-approved"
        if report.auto_gated:  # E7.5a
            why = f"scorecard gate held {len(report.auto_gated)} back"
        return JobResult(
            summary=f"awaiting approval ({len(hashes)} card(s); {why}, {env})", metrics=metrics
        )
    if HaltSwitch(HaltRepo(ctx.conn)).is_halted():
        # E6.2d: the approval events stay pending (not dispatched). The tick's drain
        # defers them while halted and runs the Investor after `!resume` if the
        # proposal is still inside its TTL; past it they lapse with a journal row.
        return JobResult(
            summary=(
                f"halted: {len(report.auto_approved)} auto-approved proposal(s) held "
                "until !resume (or their TTL)"
            ),
            metrics=metrics,
        )
    from arc.routines.runs import RoutineEventRepo

    repo = RoutineEventRepo(ctx.conn)
    events = approval_events(ctx.conn, report.auto_approved)
    pids: list[int] = []
    for ph in hashes:  # ranked order
        ev = events.get(ph)
        if ev is None:
            continue
        # E6.2d: claim the event before the spawn, so this tick's drain (and any
        # other dispatcher) never runs the same ladder inline. Lost claim: skip.
        if not repo.dispatch(ev, by=ctx.run_id, now=ctx.now):
            continue
        argv = investor_command(
            ctx.run_env, ev, chain_run_id=ctx.chain_run_id, parent_run_id=ctx.run_id
        )
        try:
            pid = spawn(argv)
        except Exception as exc:  # noqa: BLE001 - hand the event back to the tick's drain
            repo.release(ev)
            metrics["spawn_failed"] = int(metrics.get("spawn_failed", 0)) + 1
            log.error("execute.spawn_failed", proposal_hash=ph, event_id=ev, error=str(exc))
            continue
        pids.append(pid)
        log.info(
            "execute.dispatched",
            proposal_hash=ph,
            event_id=ev,
            pid=pid,
            chain_run_id=ctx.chain_run_id,
        )
    metrics["dispatched"] = len(pids)
    return JobResult(
        summary=(
            f"auto-approved {len(report.auto_approved)} ({env}); "
            f"{len(pids)} ladder(s) dispatched to the Investor"
        ),
        metrics=metrics,
        notice=f"Auto-approve: {len(pids)} order ladder(s) started ({env})" if pids else "",
    )
