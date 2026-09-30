"""Auditor post-market routine (E6.3): reconcile, snapshot, alert, Auditor card.

``personas.auditor`` in ``config/routines.yaml`` (16:30 ET, ``halt_exempt``).
Deterministic: it runs :func:`arc.reconcile.engine.reconcile` against the paper
broker (read-only calls only), then posts the ``[Auditor] Journal`` card with
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
    from arc.personas.schemas import AuditorOutput
    from arc.reconcile.engine import ReconcileReport
    from arc.routines.handlers import JobContext

__all__ = ["auditor", "auditor_output", "auditor_step", "settle_from_market"]

log = structlog.get_logger(__name__)

_MAX_NOTICE_ITEMS = 8


def settle_from_market(market: MarketDataProvider) -> Callable[[str, _dt.date], Decimal | None]:
    """``(root, day) -> close`` from the day's bar; ``None`` when unavailable."""

    def settle(root: str, day: _dt.date) -> Decimal | None:
        try:
            bars = market.history_bars(root, day, day)
        except Exception as exc:  # noqa: BLE001 - unknown settle keeps the structure open
            log.warning(
                "auditor.settle_unavailable", root=root, day=day.isoformat(), error=str(exc)
            )
            return None
        from arc.utils.calendar import ET

        on_day = [b for b in bars if b.timestamp.astimezone(ET).date() == day]
        return Decimal(str(on_day[-1].close)) if on_day else None

    return settle


def auditor_output(report: ReconcileReport) -> AuditorOutput:
    """The Auditor card's data, straight from the reconciliation report (no LLM)."""
    from arc.personas.schemas import AnomalyReport, AuditorOutput
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
    if report.halted:
        lines.append("Trading is halted until the owner checks the mismatches and runs !resume.")
    return AuditorOutput(
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
    if report.clean:
        return ""
    items = [f"{m.kind}: {m.detail}" for m in report.mismatches[:_MAX_NOTICE_ITEMS]]
    more = len(report.mismatches) - len(items)
    text = (
        f"reconciliation mismatch ({len(report.mismatches)}); trading HALTED "
        "until the owner checks and runs !resume\n• " + "\n• ".join(items)
    )
    return text + (f"\n• …and {more} more (arc journal show)" if more > 0 else "")


def auditor(
    ctx: JobContext,
    *,
    broker: BrokerAdapter,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
) -> JobResult:
    from arc.reconcile.engine import reconcile
    from arc.reconcile.performance import performance
    from arc.slack.digests import auditor_card

    report = reconcile(
        ctx.conn,
        broker,
        settings=ctx.settings,
        now=ctx.now,
        run_id=ctx.run_id,
        halt=bool(ctx.options.get("halt_on_mismatch", True)),
        settle_price=settle_price,
    )
    out = auditor_output(report)
    ctx.write("journal", report.day.isoformat(), out)
    card = auditor_card(
        out,
        performance=performance(ctx.conn, report.day),
        run_id=ctx.run_id,
        chain_run_id=ctx.chain_run_id,
    )
    return JobResult(
        summary=report.summary(),
        card=card,
        notice=_notice(report),
        metrics={
            "clean": report.clean,
            "mismatches": len(report.mismatches),
            "halted": report.halted,
            "structures_open": report.structures_open,
            "fills_local": report.fills_local,
            "fills_broker": report.fills_broker,
            "wash_sales": len(report.wash_sales),
            "expired": len(report.expired),
            "day_pnl": float(report.day_pnl) if report.day_pnl is not None else None,
        },
    )


def auditor_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: Alpaca paper broker (read-only) + Alpaca bars for settles."""
    from arc.broker.alpaca_paper import AlpacaPaperBroker
    from arc.data.alpaca import AlpacaMarketData

    return auditor(
        ctx,
        broker=AlpacaPaperBroker(),
        settle_price=settle_from_market(AlpacaMarketData()),
    )
