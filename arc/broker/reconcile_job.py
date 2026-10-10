"""Broker post-market reconcile (E6.3): reconcile, snapshot, alert, reconcile card.

D56 (E13.2): was the Auditor (``arc.routines.auditor``); the old job name ``auditor``
loads as a logged alias. ``personas.broker.reconcile`` in ``config/routines.yaml``
(16:30 ET, ``halt_exempt``). E11.1 (D71): ``reconcile.intraday`` (event-driven) checks
an unconfirmed ladder's orders right away instead of at 16:30.
Deterministic: it runs :func:`arc.reconcile.engine.reconcile` against the paper
broker (read-only calls only), then posts the ``🏦 [Broker] Reconcile`` card with
Day / MTD / YTD performance from ``pnl_snapshots`` (D28).

Any mismatch has already raised a halt (``arc:reconcile``) inside ``reconcile``;
here it becomes an immediate :attr:`JobResult.notice`, which the dispatcher
posts to the #arc-investor day thread, listing what disagreed and how to resume.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.data.base import MarketDataProvider
    from arc.personas.schemas import ReconcileOutput
    from arc.reconcile.engine import ReconcileReport
    from arc.routines.handlers import JobContext
    from arc.slack.blocks import CardView

__all__ = [
    "broker_reconcile",
    "broker_reconcile_step",
    "intraday_reconcile",
    "intraday_reconcile_step",
    "reconcile_output",
    "settle_from_market",
]

log = structlog.get_logger(__name__)

_MAX_NOTICE_ITEMS = 8


def settle_from_market(market: MarketDataProvider) -> Callable[[str, _dt.date], Decimal | None]:
    """``(root, day) -> close`` from the day's bar; ``None`` when unavailable."""

    def settle(root: str, day: _dt.date) -> Decimal | None:
        try:
            bars = market.history_bars(root, day, day)
        except Exception as exc:  # noqa: BLE001 - unknown settle keeps the structure open
            log.warning(
                "reconcile.settle_unavailable", root=root, day=day.isoformat(), error=str(exc)
            )
            return None
        from arc.utils.calendar import ET

        on_day = [b for b in bars if b.timestamp.astimezone(ET).date() == day]
        return Decimal(str(on_day[-1].close)) if on_day else None

    return settle


def reconcile_output(report: ReconcileReport) -> ReconcileOutput:
    """The reconcile card's data, straight from the reconciliation report (no LLM)."""
    from arc.personas.schemas import AnomalyReport, ReconcileOutput
    from arc.reconcile.engine import CATEGORY, MismatchKind

    anomalies = [
        AnomalyReport(
            category=CATEGORY[m.kind],
            severity="critical" if m.kind is not MismatchKind.ORDER_OPEN else "warning",
            description=m.detail,
            affected_orders=m.refs,
        )
        for m in report.mismatches
    ]
    anomalies += [
        AnomalyReport(
            category="other",
            severity="info",
            description=f"wash sale: loss lot {lot} has a replacement lot within the window",
        )
        for lot in report.wash_sales
    ]
    anomalies += [
        AnomalyReport(
            category=CATEGORY[m.kind],
            severity="info",
            description=m.detail,
            affected_orders=m.refs,
        )
        for m in report.notices
    ]
    lines = [f"Reconciliation {report.day}: {report.summary()}."]
    if report.expired:
        lines.append(f"Settled {len(report.expired)} expired structure(s) at intrinsic value.")
    if report.lots_repriced:
        lines.append(f"{report.lots_repriced} tax lot(s) set to broker fill prices.")
    if report.expiry_events:
        lines.append(
            f"{len(report.expiry_events)} assignment/exercise event(s); new opens halted "
            "until the owner unwinds the shares at the broker and runs !resume."
        )
    if report.halted:
        lines.append("Trading is halted until the owner checks the mismatches and runs !resume.")
    return ReconcileOutput(
        journal_date=report.day.isoformat(),
        daily_pnl=float(report.day_pnl) if report.day_pnl is not None else 0.0,
        open_positions=report.structures_open,
        closed_today=report.closed_today,
        fills_reviewed=report.fills_local + report.fills_broker,
        anomalies=anomalies,
        lessons=[],
        journal_narrative=" ".join(lines),
        reconciliation_status="clean" if report.clean else "discrepancies_found",
    )


def _notice(report: ReconcileReport) -> str:
    expiry = ""
    if report.expiry_events:
        # E11.4 (D73): a classified assignment/exercise is an owner action, not a mismatch.
        expiry = (
            f"assignment/exercise ({len(report.expiry_events)}); new opens HALTED, exits "
            "still run\n• " + "\n• ".join(report.expiry_events[:_MAX_NOTICE_ITEMS])
        )
    if report.clean:
        return expiry
    items = [f"{m.kind}: {m.detail}" for m in report.mismatches[:_MAX_NOTICE_ITEMS]]
    more = len(report.mismatches) - len(items)
    text = (
        f"reconciliation mismatch ({len(report.mismatches)}); trading HALTED "
        "until the owner checks and runs !resume\n• " + "\n• ".join(items)
    )
    text += f"\n• …and {more} more (arc journal show)" if more > 0 else ""
    return f"{expiry}\n{text}" if expiry else text


def broker_reconcile(
    ctx: JobContext,
    *,
    broker: BrokerAdapter,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
) -> JobResult:
    from arc.reconcile.engine import reconcile
    from arc.reconcile.performance import performance
    from arc.slack.digests import reconcile_card

    report = reconcile(
        ctx.conn,
        broker,
        settings=ctx.settings,
        now=ctx.now,
        run_id=ctx.run_id,
        halt=bool(ctx.options.get("halt_on_mismatch", True)),
        settle_price=settle_price,
    )
    out = reconcile_output(report)
    ctx.write("journal", report.day.isoformat(), out)
    slots_line = _slots_line(ctx)
    approvals_line = _approvals_line(ctx)
    ops_line = "\n".join(x for x in (slots_line, approvals_line) if x) or None
    card = reconcile_card(
        out,
        performance=performance(ctx.conn, report.day),
        run_id=ctx.run_id,
        chain_run_id=ctx.chain_run_id,
        ops_line=ops_line,
    )
    recap = _day_recap(ctx, report)
    return JobResult(
        summary=report.summary(),
        card=card,
        notice=_notice(report),
        extra_cards=[recap] if recap else [],
        metrics={
            "clean": report.clean,
            "mismatches": len(report.mismatches),
            "halted": report.halted,
            "structures_open": report.structures_open,
            "fills_local": report.fills_local,
            "fills_broker": report.fills_broker,
            "wash_sales": len(report.wash_sales),
            "expired": len(report.expired),
            "expiry_events": len(report.expiry_events),
            "expiry_pending": len(report.expiry_pending),
            "shares_unwound": len(report.unwound),
            "day_pnl": float(report.day_pnl) if report.day_pnl is not None else None,
            "slots": slots_line,
            "approvals": approvals_line,
        },
    )


def _day_recap(ctx: JobContext, report: ReconcileReport) -> CardView | None:
    """D65: ``:rolled_up_newspaper: Thu Oct 8 • Day Recap``, a day-thread reply also sent to
    #arc-investor (Slack's "Also send to" checkbox).

    Presentation only: a failure is logged and the reconcile card still posts.
    Arm stores (E10.2) don't post one; their reports are the experiment lines.
    """
    import sqlite3

    from arc.routines.headline import day_recap
    from arc.slack.blocks import CardView
    from arc.slack.loop import bold_italic

    try:
        try:
            arm = ctx.conn.execute("SELECT 1 FROM arm_identity WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            arm = None
        if arm:
            return None
        lines = day_recap(
            ctx.conn,
            report.day,
            day_pnl=float(report.day_pnl) if report.day_pnl is not None else None,
            equity_start=float(report.baseline.value) if report.baseline else None,
        )
    except Exception as exc:  # noqa: BLE001 - the recap must never block the reconcile
        log.warning("reconcile.recap_failed", error=str(exc))
        return None
    if not lines:
        return None
    text = f":rolled_up_newspaper: *{report.day:%a %b} {report.day.day} • Day Recap*\n" + "\n".join(
        bold_italic(line) for line in lines[:1]
    )
    return CardView(text=text, blocks=[], broadcast=True)


def _slots_line(ctx: JobContext) -> str | None:
    """E8.2a: today's slot coverage for the card's Ops section (never fails the job)."""
    from arc.monitoring.checks import rollup_line, slot_rollup

    try:
        return rollup_line(slot_rollup(ctx.conn, ctx.routines, ctx.now), ctx.routines)
    except Exception as exc:  # noqa: BLE001 - ops detail must not block the reconcile card
        log.warning("reconcile.slots_unavailable", error=str(exc))
        return None


def _approvals_line(ctx: JobContext) -> str | None:
    """E6.1b: today's approval sweep / card-post failures for the Ops section."""
    from arc.monitoring.checks import approvals_line

    try:
        return approvals_line(ctx.conn, ctx.now)
    except Exception as exc:  # noqa: BLE001 - ops detail must not block the reconcile card
        log.warning("reconcile.approvals_unavailable", error=str(exc))
        return None


def intraday_reconcile(ctx: JobContext, *, broker: BrokerAdapter) -> JobResult:
    """E11.1 (D71): job ``reconcile.intraday``, run for a ``reconcile.intraday`` event.

    The Broker queues the event when a ladder ends ``unconfirmed``. This checks
    that proposal's orders/executions only (``scope="intraday"``: no positions,
    fills, snapshots or settles). Policy, decided here and not in the engine:
    an order still unresolved (no broker id the lookup could find, a fill, or not
    terminal) raises the ``arc:reconcile`` halt and alerts; a resolved one is
    journaled ``reconcile:resolved`` and trading carries on.
    """
    from arc.reconcile.engine import halt_on_mismatch, reconcile

    payload = dict(ctx.event.payload) if ctx.event else {}
    phash = str(payload.get("proposal_hash") or "")
    report = reconcile(
        ctx.conn,
        broker,
        settings=ctx.settings,
        now=ctx.now,
        run_id=ctx.run_id,
        scope="intraday",
        proposal_hashes={phash} if phash else None,
    )
    if report.mismatches and bool(ctx.options.get("halt_on_mismatch", True)):
        halt_on_mismatch(ctx.conn, report, now=ctx.now, run_id=ctx.run_id)
    who = phash[:12] or "all unconfirmed"
    notice = ""
    if report.mismatches:
        items = [f"{m.kind}: {m.detail}" for m in report.mismatches[:_MAX_NOTICE_ITEMS]]
        stop = (
            "trading HALTED until the owner checks and runs !resume"
            if report.halted
            else "not halted"
        )
        notice = (
            f"intraday reconcile ({who}): {len(report.mismatches)} order(s) still unknown; "
            f"{stop}\n• " + "\n• ".join(items)
        )
    return JobResult(
        summary=f"{who}: {report.summary()}",
        notice=notice,
        metrics={
            "clean": report.clean,
            "mismatches": len(report.mismatches),
            "orders_checked": report.orders_checked,
            "halted": report.halted,
        },
    )


def intraday_reconcile_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point for ``reconcile.intraday``: the store's broker (read-only)."""
    from arc.experiments.broker import trading_broker

    return intraday_reconcile(ctx, broker=trading_broker(ctx.conn, ctx.settings))


def broker_reconcile_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: Alpaca paper broker (read-only) + Alpaca bars for settles.

    E10.2: the store's broker (an arm store reconciles its own account, virtually).
    """
    from arc.data.alpaca import AlpacaMarketData
    from arc.experiments.broker import trading_broker

    return broker_reconcile(
        ctx,
        broker=trading_broker(ctx.conn, ctx.settings),
        settle_price=settle_from_market(AlpacaMarketData()),
    )
