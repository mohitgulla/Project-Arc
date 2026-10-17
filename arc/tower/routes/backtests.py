"""``GET /api/backtests`` and ``GET /api/backtests/{study_id}`` (D124).

The backtest studies on the Experiments page: the research ledger, study specs and the
nightly runner's queue, read from files (never the audit store, never run). The live
account's profile (``/api/meta`` ``account_profile``, D26 overrides applied) picks the
profile each headline is on.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from arc.tower.api import TowerError
from arc.tower.data_backtests import (
    BacktestDetailResponse,
    BacktestPaths,
    BacktestsResponse,
    load_backtest,
    load_backtests,
)
from arc.tower.routes.deps import (  # noqa: TC001 - FastAPI dependencies
    ControlConn,
    ControlTower,
    Scope,
    effective,
)
from arc.tower.schemas import ErrorResponse
from arc.tower.scope import scoped, wrap

__all__ = ["router"]

router = APIRouter(
    tags=["experiments"], responses={503: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
)

# The studies are Production-level research: an arm view gets the same list.
BacktestsOut = scoped(BacktestsResponse)
BacktestDetailOut = scoped(BacktestDetailResponse)


def _paths(cfg: ControlTower) -> tuple[BacktestPaths, str, object]:
    from arc.routines.config import REPO_ROOT

    settings, routines = effective(cfg)
    bs = routines.backtest_studies
    root = cfg.backtest_root if cfg.backtest_root is not None else REPO_ROOT
    paths = BacktestPaths(root=Path(root), ledger=bs.ledger, queue=bs.queue)
    return paths, bs.window_start.strftime("%H:%M"), settings


@router.get("/backtests", response_model=BacktestsOut)
def backtests(cfg: ControlTower, scope: Scope) -> BacktestsResponse:
    """Every backtest study: furthest stage, the best treatment on the live account's profile."""
    paths, start, settings = _paths(cfg)
    out = load_backtests(paths, settings=settings, now=cfg.clock(), window_start=start)
    return wrap(BacktestsOut, out, scope, shared=True)


@router.get("/backtests/{study_id}", response_model=BacktestDetailOut)
def backtest(
    study_id: str, cfg: ControlTower, conn: ControlConn, scope: Scope
) -> BacktestDetailResponse:
    """One study: decision rule, arms, every run's per-profile metrics and verdicts."""
    paths, _, settings = _paths(cfg)
    out = load_backtest(paths, study_id, settings=settings, now=cfg.clock(), conn=conn)
    if out is None:
        raise TowerError(404, "not_found", f"no backtest {study_id}")
    return wrap(BacktestDetailOut, out, scope, shared=True)
