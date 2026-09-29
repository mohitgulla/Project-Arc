"""``GET /api/snapshot`` (E8.7): the current :class:`~arc.tower.data.TowerSnapshot`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from arc.tower.data import TowerSnapshot, load_snapshot
from arc.tower.routes.deps import Conn, Tower, effective
from arc.tower.schemas import ErrorResponse

__all__ = ["router"]

router = APIRouter(tags=["snapshot"], responses={503: {"model": ErrorResponse}})


@router.get("/snapshot", response_model=TowerSnapshot)
def snapshot(
    cfg: Tower,
    conn: Conn,
    lookback_days: Annotated[int | None, Query(ge=1, le=366)] = None,
) -> TowerSnapshot:
    """Every dashboard section read in one pass (SELECT only), with the gate's caps."""
    settings, _ = effective(cfg)
    return load_snapshot(
        conn,
        now=cfg.clock(),
        db_path=str(cfg.db_path),
        lookback_days=lookback_days or cfg.lookback_days,
        delta_cap=settings.portfolio_delta_cap,
        vega_cap_pct=settings.portfolio_vega_cap_pct,
    )
