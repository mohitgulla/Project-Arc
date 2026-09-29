"""``GET /api/overview`` and ``GET /api/positions`` (E8.7a): the Overview page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from arc.tower.data import monitor_stale_after
from arc.tower.data_overview import (
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
) -> OverviewResponse:
    """Status strip, equity (by range), day P&L, positions, Greeks vs caps, proposals,
    movers and recent activity, read in one pass (SELECT only)."""
    settings, _ = effective(cfg)
    return load_overview(
        conn,
        now=cfg.clock(),
        rng=range,
        delta_cap=settings.portfolio_delta_cap,
        vega_cap_pct=settings.portfolio_vega_cap_pct,
        max_alloc_pct=settings.max_alloc_pct,
        stale_after=monitor_stale_after(conn),
    )


@router.get("/positions", response_model=PositionsResponse)
def positions(
    cfg: Tower,
    conn: Conn,
    status: Annotated[PositionStatus, Query()] = "open",
) -> PositionsResponse:
    """Open, closed or all structures with the latest broker marks."""
    return load_positions(
        conn, now=cfg.clock(), status=status, stale_after=monitor_stale_after(conn)
    )
