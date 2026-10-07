"""``GET /api/overview`` and ``GET /api/positions`` (E8.7a): the Overview page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from arc.tower.data import monitor_stale_after
from arc.tower.data_overview import (
    ACTIVITY_MAX_HOURS,
    OverviewRange,
    OverviewResponse,
    PositionsResponse,
    PositionStatus,
    load_overview,
    load_positions,
)
from arc.tower.routes.deps import Conn, Tower, effective
from arc.tower.schemas import ErrorResponse

__all__ = ["router"]

router = APIRouter(tags=["overview"], responses={503: {"model": ErrorResponse}})


@router.get("/overview", response_model=OverviewResponse)
def overview(
    cfg: Tower,
    conn: Conn,
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
    movers and recent activity (rolling window), read in one pass (SELECT only)."""
    settings, routines = effective(cfg)
    hours = activity_hours if activity_hours is not None else routines.tower.overview.activity_hours
    return load_overview(
        conn,
        now=cfg.clock(),
        rng=range,
        delta_cap=settings.portfolio_delta_cap,
        vega_cap_pct=settings.portfolio_vega_cap_pct,
        max_alloc_pct=settings.max_alloc_pct,
        stale_after=monitor_stale_after(conn),
        activity_hours=hours,
    )


@router.get("/positions", response_model=PositionsResponse)
def positions(
    cfg: Tower,
    conn: Conn,
    status: Annotated[PositionStatus, Query()] = "open",
) -> PositionsResponse:
    """Open, closed or all structures with the latest broker marks; open rows carry the
    E13.14 exit path under ``personas.exit_path`` shadow | research."""
    _, routines = effective(cfg)
    return load_positions(
        conn,
        now=cfg.clock(),
        status=status,
        stale_after=monitor_stale_after(conn),
        exit_mode=routines.exit_path.mode,
    )
