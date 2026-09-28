"""Intraday position monitor (E5.3): positions, net Greeks, expiry and halt checks.

Runs every 30 min during the session (``personas.monitor`` in
``config/routines.yaml``). Deterministic and read-only: it reads the paper
account and positions, values open option positions with the same
:func:`~arc.pipeline.market.build_portfolio` the gate uses (max loss per
underlying + net Greeks), and applies the E3.3 daily-loss auto-halt.

It never submits anything. E6.2: it evaluates every open structure against the
E2.4 exit policy (:mod:`arc.execution.exits`) and *proposes* the fired exits
(gate + ``arc2`` token + approval card); the Investor executes them only after
approval. Close-to-reallocate (D19) is E6.4.

Heartbeat policy: the job is ``notify: quiet`` (its line folds into the next
persona heartbeat), but anything a human must see now is raised as a
:attr:`JobResult.notice`: a new daily-loss halt, positions expiring within a
session, or positions that cannot be valued. A notice is posted once per
distinct message per day, not every 30 min.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from typing import TYPE_CHECKING, Any, cast

import structlog

from arc.routines.handlers import JobResult
from arc.routines.runs import RoutineStateRepo
from arc.utils.calendar import ET, dte_calendar

if TYPE_CHECKING:
    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.pipeline.env import PipelineEnv
    from arc.routines.handlers import JobContext

log = structlog.get_logger(__name__)

_NOTICE_KEY = "monitor:last_notice"


def _expiring(positions: list[BrokerPosition], ctx: JobContext, within_days: int) -> list[str]:
    from arc.structures.occ import parse_occ

    today = ctx.now.astimezone(ET).date()
    out: set[str] = set()
    for p in positions:
        if p.asset_class != "us_option":
            continue
        occ = parse_occ(p.symbol)
        if dte_calendar(today, occ.expiration) <= within_days:
            out.add(f"{occ.root} {occ.expiration:%m-%d}")
    return sorted(out)


def _dedupe_notice(ctx: JobContext, notice: str) -> str:
    """Return *notice* unless the same text was already posted today."""
    if not notice:
        return ""
    state = RoutineStateRepo(ctx.conn)
    day = ctx.now.astimezone(ET).date().isoformat()
    digest = hashlib.sha256(f"{day}|{notice}".encode()).hexdigest()[:16]
    if state.get(_NOTICE_KEY) == digest:
        return ""
    state.set(_NOTICE_KEY, digest, now=ctx.now)
    return notice


def _record_heartbeat(
    ctx: JobContext, positions: list[BrokerPosition], info: AccountInfo, metrics: dict[str, Any]
) -> None:
    """E8.3: persist what this run saw (net Greeks, marks) for the control tower.

    One append-only ``heartbeats`` row, ``component = monitor``: ``ok`` when the
    positions were valued, ``degraded`` when not. ``detail`` holds the metrics plus
    the broker's per-leg marks, so the read-only dashboard never calls the broker.
    """
    from arc.monitoring.store import HeartbeatRepo

    legs = [
        {
            "symbol": p.symbol,
            "qty": str(p.qty),
            "side": p.side,
            "asset_class": p.asset_class,
            "avg_entry_price": None if p.avg_entry_price is None else str(p.avg_entry_price),
            "market_value": None if p.market_value is None else str(p.market_value),
            "unrealized_pl": None if p.unrealized_pl is None else str(p.unrealized_pl),
        }
        for p in positions
    ]
    detail = {
        **metrics,
        "last_equity": None if info.last_equity is None else float(info.last_equity),
        "legs": legs,
    }
    HeartbeatRepo(ctx.conn).record(
        "monitor",
        "ok" if metrics.get("valued") else "degraded",
        at=ctx.now,
        correlation={"run_id": ctx.run_id},
        detail=detail,
    )


def monitor(ctx: JobContext, env: PipelineEnv) -> JobResult:
    from arc.gate.halt import HaltSwitch
    from arc.pipeline.market import PortfolioError, account_snapshot, build_portfolio
    from arc.store.repos import HaltRepo

    settings = ctx.settings
    now = ctx.now
    within = int(ctx.options.get("expiry_warn_days", 1))
    notices: list[str] = []

    info = env.account()
    switch = HaltSwitch(HaltRepo(ctx.conn))
    new_halt = switch.check_daily_loss(account_snapshot(info, now), settings, now=now)
    if new_halt is not None:
        notices.append(f"daily-loss halt raised: {new_halt.reason}")
    halted = switch.is_halted()

    pnl = info.equity - info.last_equity if info.last_equity is not None else None
    pnl_text = f", day P&L ${pnl:+,.2f}" if pnl is not None else ""
    metrics: dict[str, Any] = {
        "equity": float(info.equity),
        "halted": halted,
        "halt_raised": new_halt is not None,
    }

    positions = env.positions()
    try:
        portfolio = build_portfolio(
            ctx.conn,
            positions,
            env.market,
            now=now,
            wash_sale_days=settings.wash_sale_days,
            r=settings.scanner_risk_free_rate,
        )
    except PortfolioError as exc:
        notices.append(f"cannot value open positions: {exc}")
        metrics.update({"positions": None, "valued": False})
        summary = f"equity ${info.equity:,.2f}{pnl_text}; positions NOT valued ({exc})"
        portfolio = None
    else:
        g = portfolio.greeks
        n = len(portfolio.positions)
        max_loss = sum((p.max_loss for p in portfolio.positions), start=0)
        metrics.update(
            {
                "positions": n,
                "valued": True,
                "max_loss": float(max_loss),
                "delta": g.delta,
                "gamma": g.gamma,
                "vega": g.vega,
                "theta": g.theta,
            }
        )
        roots = ", ".join(p.underlying for p in portfolio.positions)
        summary = (
            f"equity ${info.equity:,.2f}{pnl_text}; {n} position(s)"
            + (f" [{roots}], max loss ${max_loss:,.0f}" if n else "")
            + (f"; Δ {g.delta:+.1f} Γ {g.gamma:+.3f} ν {g.vega:+.1f} Θ {g.theta:+.1f}" if n else "")
        )

    if portfolio is not None and ctx.options.get("exits", True):
        from arc.execution.exits import propose_exits

        eod_from = _dt.time.fromisoformat(str(ctx.options.get("eod_marks_from", "15:30")))
        run = propose_exits(
            ctx.conn,
            market=env.market,
            settings=settings,
            account=switch.apply(account_snapshot(info, now)),
            portfolio=portfolio,
            switch=switch,
            now=now,
            run_id=ctx.run_id,
            write_context=ctx.write,
            mint=env.mint_tokens,
            eod_from=eod_from,
        )
        metrics["exits_evaluated"] = run.evaluated
        metrics["exits_proposed"] = len(run.proposed)
        if run.lines:
            summary += "; exits: " + "; ".join(run.lines)
            notices.extend(f"exit proposed: {line}" for line in run.lines if "gate PASS" in line)
        notices.extend(run.errors)

    expiring = _expiring(positions, ctx, within)
    metrics["expiring"] = len(expiring)
    if expiring:
        notices.append(f"expiring within {within} day(s): {', '.join(expiring)}")
    if halted:
        summary += "; HALTED"
    log.info("routines.monitor", **{k: v for k, v in metrics.items() if v is not None})
    _record_heartbeat(ctx, positions, info, metrics)
    return JobResult(
        summary=summary, metrics=metrics, notice=_dedupe_notice(ctx, "; ".join(notices))
    )


def monitor_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: live Alpaca paper account + market data (read-only)."""
    from arc.pipeline.steps import _LazyEnv

    return monitor(ctx, cast("PipelineEnv", _LazyEnv(ctx)))
