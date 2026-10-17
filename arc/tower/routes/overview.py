"""``GET /api/overview`` and ``GET /api/positions`` (E8.7a): the Overview page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from arc.tower.data import _latest_heartbeat, monitor_stale_after, parse_ts
from arc.tower.data_overview import (
    ACTIVITY_MAX_HOURS,
    OverviewRange,
    OverviewResponse,
    PositionsResponse,
    PositionStatus,
    load_overview,
    load_positions,
)
from arc.tower.data_sleeves import named_sleeves
from arc.tower.routes.deps import Conn, ControlConn, Scope, Tower, effective
from arc.tower.schemas import ErrorResponse
from arc.tower.scope import scoped, wrap

__all__ = ["router"]

router = APIRouter(
    tags=["overview"], responses={503: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
)

OverviewOut = scoped(OverviewResponse)
PositionsOut = scoped(PositionsResponse)


@router.get("/overview", response_model=OverviewOut)
def overview(  # noqa: PLR0913 - one query parameter per control
    cfg: Tower,
    conn: Conn,
    control: ControlConn,
    scope: Scope,
    range: Annotated[OverviewRange, Query()] = "1D",  # noqa: A002 - the URL parameter name
    activity_hours: Annotated[
        int | None,
        Query(
            ge=1,
            le=ACTIVITY_MAX_HOURS,
            description="Recent Activity window in hours (default: "
            "config `tower.overview.activity_hours`, 24)",
        ),
    ] = None,
) -> OverviewResponse:
    """Status strip, equity (by range), day P&L, positions, Greeks vs caps, proposals,
    movers and recent activity (rolling window), read in one pass (SELECT only).

    ``?arm=``: the arm's store, so equity / P&L are its virtual account (E15.2). The
    status strip's Tick is production's (D113): arms run on production's 10-min tick
    and write no ``tick`` heartbeat of their own."""
    settings, routines = effective(cfg)
    hours = activity_hours if activity_hours is not None else routines.tower.overview.activity_hours
    out = load_overview(
        conn,
        now=cfg.clock(),
        rng=range,
        dollar_delta_cap_pct=settings.portfolio_dollar_delta_cap_pct,
        beta_delta_cap_pct=settings.portfolio_beta_delta_cap_pct,
        vega_cap_pct=settings.portfolio_vega_cap_pct,
        max_alloc_pct=settings.max_alloc_pct,
        max_alloc_pct_reduced=settings.max_alloc_pct_reduced,
        alloc_tier_threshold_pct=settings.alloc_tier_threshold_pct,
        stale_after=monitor_stale_after(conn),
        activity_hours=hours,
        greek_advisory=routines.tower.overview.greek_advisory,
        sleeves=named_sleeves(settings),
        buying_power_kind=settings.profile.buying_power.value,
    )
    if scope.is_arm:
        tick = _latest_heartbeat(control, "tick")
        status = out.status.model_copy(
            update={
                "tick_at": parse_ts(tick["at"]) if tick else None,
                "tick_status": tick["status"] if tick else None,
            }
        )
        out = out.model_copy(update={"status": status})
    return wrap(OverviewOut, out, scope)


@router.get("/positions", response_model=PositionsOut)
def positions(
    cfg: Tower,
    conn: Conn,
    scope: Scope,
    status: Annotated[PositionStatus, Query()] = "open",
) -> PositionsResponse:
    """Open, closed or all structures with the latest broker marks; open rows carry the
    E13.14 exit path (D56: always Research-managed since E13.15)."""
    effective(cfg)  # an invalid config still 503s like every page
    out = load_positions(
        conn,
        now=cfg.clock(),
        status=status,
        stale_after=monitor_stale_after(conn),
    )
    return wrap(PositionsOut, out, scope)
