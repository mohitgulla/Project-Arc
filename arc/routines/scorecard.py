"""Weekly paper scorecard routine (E7.3): ``personas.scorecard`` in ``config/routines.yaml``.

Fridays 16:45 ET (after the Broker's reconcile). Deterministic, no LLM:

1. :func:`arc.journal.scorecard.build_scorecard` over the ET week containing the
   run's logical time, read from the audit store;
2. write the Markdown report to ``<report_dir>/<monday>.md`` (default
   ``docs/RESEARCH/weekly/``, relative to the repo root);
3. return the ``[Ops] Scorecard`` card, which the dispatcher posts to the
   #arc-investor day thread (``notify: card``).

The D19 hold-to-expiry shadow of early-closed positions needs the underlying's
settlement close; the dispatcher entry point prices it from Alpaca bars (the
Broker reconcile's :func:`~arc.broker.reconcile_job.settle_from_market`). Unknown settles stay
``pending`` in the report.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable
    from decimal import Decimal

    from arc.config import ArcSettings
    from arc.journal.scorecard import OrderBudgetLimits
    from arc.routines.handlers import JobContext

__all__ = ["REPO_ROOT", "budget_limits", "report_path", "scorecard", "scorecard_step"]

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_DIR = "docs/RESEARCH/weekly"


def budget_limits(settings: ArcSettings) -> OrderBudgetLimits:
    """The D32 order budget from settings (E6.5 keys), else the D32 defaults."""
    from arc.journal.scorecard import OrderBudgetLimits

    keys = {
        "daily_max": "order_budget_daily_max",
        "restrict_at": "order_budget_restrict_at",
        "close_reserve": "order_budget_close_reserve",
    }
    found = {k: getattr(settings, attr) for k, attr in keys.items() if hasattr(settings, attr)}
    return OrderBudgetLimits(**found)


def report_path(report_dir: str | Path, monday: _dt.date) -> Path:
    base = Path(report_dir)
    if not base.is_absolute():
        base = REPO_ROOT / base
    return base / f"{monday.isoformat()}.md"


def scorecard(
    ctx: JobContext,
    *,
    settle_price: Callable[[str, _dt.date], Decimal | None] | None = None,
) -> JobResult:
    from arc.journal.scorecard import (
        auto_approve_gate,
        build_scorecard,
        render_markdown,
        week_window,
    )
    from arc.slack.scorecard import scorecard_card

    start, end = week_window(ctx.now)
    sc = build_scorecard(
        ctx.conn,
        start=start,
        end=end,
        now=ctx.now,
        limits=budget_limits(ctx.settings),
        settle_price=settle_price,
        auto_approve=auto_approve_gate(ctx.conn, ctx.settings, now=ctx.now),
    )
    ctx.record_input(
        "scorecard",
        "audit_store",
        sc.model_dump(mode="json", exclude={"generated_at"}),
        as_of=ctx.now,
        count=sc.funnel.proposals,
    )
    path: Path | None = None
    if ctx.options.get("write_report", True):
        path = report_path(ctx.options.get("report_dir", DEFAULT_REPORT_DIR), start.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(sc), encoding="utf-8")
        log.info("scorecard.report_written", path=str(path))
    shown = str(path.relative_to(REPO_ROOT)) if path and path.is_relative_to(REPO_ROOT) else path
    card = scorecard_card(
        sc,
        report_path=None if shown is None else str(shown),
        run_id=ctx.run_id,
        chain_run_id=ctx.chain_run_id,
    )
    return JobResult(
        summary=(
            f"week {sc.label}: {sc.funnel.proposals} proposals, {sc.funnel.fills} fills, "
            f"{sc.pnl.closed} closed, realised {sc.pnl.realised:+,.2f}"
        ),
        card=card,
        metrics={
            "proposals": sc.funnel.proposals,
            "fills": sc.funnel.fills,
            "closed": sc.pnl.closed,
            "realised_pnl": sc.pnl.realised,
            "early_closed": len(sc.early_closed),
            "swaps": len(sc.swaps),
            "gate_fail": sc.funnel.gate_fail,
            "report": None if path is None else str(path),
        },
    )


def scorecard_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: settles for the D19 shadow come from Alpaca daily bars."""
    from arc.broker.reconcile_job import settle_from_market
    from arc.data.alpaca import AlpacaMarketData

    try:
        settle = settle_from_market(AlpacaMarketData())
    except Exception as exc:  # noqa: BLE001 - no market data: shadows stay pending
        log.warning("scorecard.no_market_data", error=str(exc))
        settle = None
    return scorecard(ctx, settle_price=settle)
