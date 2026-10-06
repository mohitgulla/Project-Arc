"""Intraday position monitor (E5.3): positions, net Greeks, expiry and halt checks.

Runs every 5 min during the session (``personas.monitor`` in
``config/routines.yaml``; D35 moved it from 30 min so it is the control tower's
mark source). Deterministic and read-only: it reads the paper
account and positions, values open option positions with the same
:func:`~arc.pipeline.market.build_portfolio` the gate uses (max loss per
underlying + net Greeks), and applies the E3.3 daily-loss auto-halt.

It never submits anything. E6.2: it evaluates every open structure against the
E2.4 exit policy (:mod:`arc.execution.exits`) and *proposes* the fired exits
(gate + ``arc2`` token + approval card); the Investor executes them only after
approval. Close-to-reallocate (D19) is E6.4. The shipped config sets
``exits: false`` (E6.4's ``positions.evaluate`` chain owns exit proposals); when
on, ``propose_exits`` still proposes at most one exit per structure per day.

Heartbeat policy: the job is ``notify: quiet`` (its line folds into the next
persona heartbeat), but anything a human must see now is raised as a
:attr:`JobResult.notice`: a new daily-loss halt, positions expiring within a
session, or positions that cannot be valued. A notice is posted once per
distinct message per day, not every run.

Broker request budget (E5.3a): one run makes 1 account + 1 positions request,
then per open underlying (:func:`~arc.pipeline.market.build_portfolio`) 1 stock
quote + 1 option-chain snapshot + >=1 contracts page (open interest) + 1 raw
snapshot (volume), i.e. :func:`broker_requests` = ``2 + 4 * roots`` plus
pagination (34 at the default 8 ``max_open_positions``). At one run per 5 min
that is far below Alpaca Basic's 200 requests/min, even if the monitor, the
position manager and a trading-loop run land in the same minute. Each run
records its estimate as ``metrics.broker_requests``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

import structlog

from arc.routines.handlers import JobResult
from arc.routines.runs import RoutineStateRepo
from arc.utils.calendar import ET, dte_calendar

if TYPE_CHECKING:
    from decimal import Decimal

    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.pipeline.env import PipelineEnv
    from arc.routines.handlers import JobContext

log = structlog.get_logger(__name__)

_NOTICE_KEY = "monitor:last_notice"

# Broker request budget (see module docstring). Estimates exclude pagination.
ALPACA_BASIC_REQ_PER_MIN = 200
_BASE_REQUESTS = 2  # account + positions
_REQUESTS_PER_ROOT = 4  # stock quote + chain snapshot + contracts (OI) + raw snapshot (volume)


def broker_requests(roots: int) -> int:
    """Broker/data requests one monitor run makes for *roots* open underlyings."""
    return _BASE_REQUESTS + _REQUESTS_PER_ROOT * roots


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


def _option_roots(positions: list[BrokerPosition]) -> int:
    from arc.structures.occ import parse_occ

    return len({parse_occ(p.symbol).root for p in positions if p.asset_class == "us_option"})


def _dedupe_notice(ctx: JobContext, notices: list[str]) -> str:
    """Join the *notices* not already posted today (each distinct text once per ET day).

    Deduped per notice, not per joined message: at a 5-min cadence the mix changes
    (a halt notice one run, the expiry warning alone the next) and a whole-message
    digest would re-post the expiry warning every time the mix changed.
    """
    if not notices:
        return ""
    state = RoutineStateRepo(ctx.conn)
    day = ctx.now.astimezone(ET).date().isoformat()
    try:
        saved = json.loads(state.get(_NOTICE_KEY) or "{}")
    except ValueError:  # pre-E5.3a value: a bare digest of the joined text
        saved = {}
    if not isinstance(saved, dict):  # an all-digit legacy digest parses as a number
        saved = {}
    seen: list[str] = list(saved.get("seen", [])) if saved.get("day") == day else []
    fresh: list[str] = []
    for notice in dict.fromkeys(notices):
        digest = hashlib.sha256(f"{day}|{notice}".encode()).hexdigest()[:16]
        if digest not in seen:
            seen.append(digest)
            fresh.append(notice)
    if fresh:
        state.set(_NOTICE_KEY, json.dumps({"day": day, "seen": seen}), now=ctx.now)
    return "; ".join(fresh)


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


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
            "avg_entry_price": _s(p.avg_entry_price),
            "market_value": _s(p.market_value),
            "unrealized_pl": _s(p.unrealized_pl),
            "current_price": _s(p.current_price),  # E5.3a: intraday mark per share
            "lastday_price": _s(p.lastday_price),
            "change_today": _s(p.change_today),
        }
        for p in positions
    ]
    detail = {
        **metrics,
        # E5.3a (D35): account fields the tower shows (never re-read from the broker).
        "cash": float(info.cash),
        "buying_power": float(info.buying_power),
        "options_buying_power": _f(info.options_buying_power),
        "non_marginable_bp": _f(info.non_marginable_buying_power),
        "last_equity": _f(info.last_equity),  # raw broker value, audit only (E5.9b)
        # metrics carries day_pnl / prev_close / prev_close_source (the D43 baseline)
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
    from arc.pipeline.market import (
        PortfolioError,
        account_baseline,
        account_snapshot,
        build_portfolio,
    )
    from arc.reconcile.baseline import day_pnl
    from arc.store.repos import HaltRepo

    settings = ctx.settings
    now = ctx.now
    within = int(ctx.options.get("expiry_warn_days", 1))
    notices: list[str] = []

    info = env.account()
    switch = HaltSwitch(HaltRepo(ctx.conn))
    # E5.9b (D43): one start-of-day equity for the halt check, the summary, the
    # exits' gate snapshot and the heartbeat the tower reads.
    baseline = account_baseline(ctx.conn, info, now)
    new_halt = switch.check_daily_loss(
        account_snapshot(info, now, baseline=baseline), settings, now=now
    )
    if new_halt is not None:
        notices.append(f"daily-loss halt raised: {new_halt.reason}")
    halted = switch.is_halted()

    pnl = day_pnl(info.equity, baseline)
    pnl_text = f", day P&L ${pnl:+,.2f}" if pnl is not None else ""
    metrics: dict[str, Any] = {
        "equity": float(info.equity),
        "day_pnl": _f(pnl),
        "prev_close": _f(baseline.value) if baseline is not None else None,
        "prev_close_source": baseline.source if baseline is not None else None,
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
            spot_max_spread_pct=settings.spot_max_spread_pct,
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
            account=switch.apply(account_snapshot(info, now, baseline=baseline)),
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
    # D32: the day's order budget, for the heartbeat detail (tower) and the manifest.
    try:
        from arc.pipeline.budget import read_budget

        budget = read_budget(ctx, env, settings, now=now).budget
        metrics["order_budget"] = budget.brief()
        metrics["orders_used"] = budget.used
        metrics["orders_limit"] = budget.limit
        if budget.tier.value != "normal":
            notices.append(budget.summary())
    except Exception as exc:  # noqa: BLE001 - the heartbeat must still be written
        log.warning("routines.monitor.order_budget_failed", error=str(exc))
    if expiring:
        notices.append(f"expiring within {within} day(s): {', '.join(expiring)}")
    if halted:
        summary += "; HALTED"
    metrics["broker_requests"] = broker_requests(_option_roots(positions))
    log.info("routines.monitor", **{k: v for k, v in metrics.items() if v is not None})
    _record_heartbeat(ctx, positions, info, metrics)
    return JobResult(summary=summary, metrics=metrics, notice=_dedupe_notice(ctx, notices))


def monitor_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: live Alpaca paper account + market data (read-only)."""
    from arc.pipeline.steps import _LazyEnv

    return monitor(ctx, cast("PipelineEnv", _LazyEnv(ctx)))
