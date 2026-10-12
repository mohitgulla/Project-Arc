"""``GET /api/experiments`` and ``GET /api/experiments/{experiment_id}`` (E10.5, D44)."""

from __future__ import annotations

from fastapi import APIRouter

from arc.tower.api import TowerError
from arc.tower.data_experiments import (
    ExperimentDetailResponse,
    ExperimentsResponse,
    load_experiment,
    load_experiments,
)
from arc.tower.routes.deps import (  # noqa: TC001 - FastAPI dependencies
    ControlConn,
    ControlTower,
    Scope,
)
from arc.tower.schemas import ErrorResponse
from arc.tower.scope import scoped, wrap

__all__ = ["router"]

router = APIRouter(
    tags=["experiments"], responses={503: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
)

# E15.6: the registry and every report live in control's store: an arm view gets
# control's, flagged ``shared_from_control``.
ExperimentsOut = scoped(ExperimentsResponse)
ExperimentDetailOut = scoped(ExperimentDetailResponse)


@router.get("/experiments", response_model=ExperimentsOut)
def experiments(cfg: ControlTower, conn: ControlConn, scope: Scope) -> ExperimentsResponse:
    """Every forward experiment with the latest stored evaluation's numbers."""
    return wrap(ExperimentsOut, load_experiments(conn, now=cfg.clock()), scope, shared=True)


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentDetailOut,
)
def experiment(
    experiment_id: str, cfg: ControlTower, conn: ControlConn, scope: Scope
) -> ExperimentDetailResponse:
    """One experiment: spec + hashes, latest report, equity curves and cumulative d_t band."""
    out = load_experiment(conn, experiment_id, now=cfg.clock())
    if out is None:
        raise TowerError(404, "not_found", f"no experiment {experiment_id}")
    return wrap(ExperimentDetailOut, out, scope, shared=True)
