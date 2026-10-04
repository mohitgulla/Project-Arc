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
from arc.tower.routes.deps import Conn, Tower  # noqa: TC001 - FastAPI dependencies
from arc.tower.schemas import ErrorResponse

__all__ = ["router"]

router = APIRouter(tags=["experiments"], responses={503: {"model": ErrorResponse}})


@router.get("/experiments", response_model=ExperimentsResponse)
def experiments(cfg: Tower, conn: Conn) -> ExperimentsResponse:
    """Every forward experiment with the latest stored evaluation's numbers."""
    return load_experiments(conn, now=cfg.clock())


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
def experiment(experiment_id: str, cfg: Tower, conn: Conn) -> ExperimentDetailResponse:
    """One experiment: spec + hashes, latest report, equity curves and cumulative d_t band."""
    out = load_experiment(conn, experiment_id, now=cfg.clock())
    if out is None:
        raise TowerError(404, "not_found", f"no experiment {experiment_id}")
    return out
