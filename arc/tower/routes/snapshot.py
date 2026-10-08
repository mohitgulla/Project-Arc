"""``GET /api/snapshot`` (E8.7): the current :class:`~arc.tower.data.TowerSnapshot`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query

from arc.tower.data import TowerSnapshot, load_snapshot
from arc.tower.routes.deps import Conn, Tower, effective
from arc.tower.schemas import ErrorResponse

if TYPE_CHECKING:
    import sqlite3

    from arc.tower.api import TowerConfig

__all__ = ["read_snapshot", "router"]

router = APIRouter(tags=["snapshot"], responses={503: {"model": ErrorResponse}})


@router.get("/snapshot", response_model=TowerSnapshot)
def snapshot(
    cfg: Tower,
    conn: Conn,
    lookback_days: Annotated[int | None, Query(ge=1, le=366)] = None,
) -> TowerSnapshot:
    """Every dashboard section read in one pass (SELECT only), with the gate's caps."""
    return read_snapshot(cfg, conn, lookback_days)


def read_snapshot(
    cfg: TowerConfig, conn: sqlite3.Connection, lookback_days: int | None = None
) -> TowerSnapshot:
    """The snapshot ``/api/snapshot`` serves; ``arc tower snapshot`` calls this too.

    Gate caps come from the effective config (D26 overrides in the store applied),
    so the CLI and the API can't disagree on scale.
    """
    settings, _ = effective(cfg)
    return load_snapshot(
        conn,
        now=cfg.clock(),
        db_path=str(cfg.db_path),
        lookback_days=lookback_days or cfg.lookback_days,
        dollar_delta_cap_pct=settings.portfolio_dollar_delta_cap_pct,
        beta_delta_cap_pct=settings.portfolio_beta_delta_cap_pct,
        vega_cap_pct=settings.portfolio_vega_cap_pct,
    )
