"""``GET /api/ml/models``, ``/api/ml/models/{family}/{version}``, ``/api/ml/studies`` and
``/api/ml/studies/{study_id}`` (D133).

The Experiments page's Model tab: the model registry and the ``ML-<n>`` studies, read
from files (``data/ml/``, ``config/ml_studies/``, the shared study queue and the Arc
Analyst's proposed drafts). Never the training code, never a model load.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from arc.tower.api import TowerError
from arc.tower.data_ml import (
    MlModelDetailResponse,
    MlModelsResponse,
    MlPaths,
    MlStudiesResponse,
    MlStudyDetailResponse,
    load_ml_model,
    load_ml_models,
    load_ml_studies,
    load_ml_study,
)
from arc.tower.routes.deps import (  # noqa: TC001 - FastAPI dependencies
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

# Models and studies are Production-level research: an arm view gets the same data.
ModelsOut = scoped(MlModelsResponse)
ModelDetailOut = scoped(MlModelDetailResponse)
StudiesOut = scoped(MlStudiesResponse)
StudyDetailOut = scoped(MlStudyDetailResponse)


def _paths(cfg: ControlTower) -> tuple[MlPaths, str, float]:
    from arc.routines.config import REPO_ROOT

    _, routines = effective(cfg)
    bs = routines.backtest_studies
    ms = bs.ml_studies
    root = cfg.backtest_root if cfg.backtest_root is not None else REPO_ROOT
    backlog = cfg.analyst_home / "xp_backlog.json" if cfg.analyst_home is not None else None
    paths = MlPaths(
        root=Path(root),
        models=ms.models_dir,
        health=str(Path(ms.models_dir).parent / "health"),
        ledger=ms.ledger,
        runs=ms.runs_dir,
        queue=bs.queue,
        backlog=backlog,
    )
    return paths, bs.window_start.strftime("%H:%M"), float(ms.default_rung_s)


@router.get("/ml/models", response_model=ModelsOut)
def ml_models(cfg: ControlTower, scope: Scope) -> MlModelsResponse:
    """Every model family: champion / shadow / candidate versions and the strip numbers."""
    paths, start, _ = _paths(cfg)
    out = load_ml_models(paths, now=cfg.clock(), window_start=start)
    return wrap(ModelsOut, out, scope, shared=True)


@router.get("/ml/models/{family}/{version}", response_model=ModelDetailOut)
def ml_model(family: str, version: str, cfg: ControlTower, scope: Scope) -> MlModelDetailResponse:
    """One version: spec, rule table, calibration, loss vs baseline, importance, live."""
    paths, _, _ = _paths(cfg)
    out = load_ml_model(paths, family, version, now=cfg.clock())
    if out is None:
        raise TowerError(404, "not_found", f"no model {family}/{version}")
    return wrap(ModelDetailOut, out, scope, shared=True)


@router.get("/ml/studies", response_model=StudiesOut)
def ml_studies(cfg: ControlTower, scope: Scope) -> MlStudiesResponse:
    """Every ``ML-<n>``: proposed drafts, queued / running studies and verdicts."""
    paths, start, rung_s = _paths(cfg)
    out = load_ml_studies(paths, now=cfg.clock(), window_start=start, default_rung_s=rung_s)
    return wrap(StudiesOut, out, scope, shared=True)


@router.get("/ml/studies/{study_id}", response_model=StudyDetailOut)
def ml_study(study_id: str, cfg: ControlTower, scope: Scope) -> MlStudyDetailResponse:
    """One study: its ladder, spec and evidence lines."""
    paths, _, rung_s = _paths(cfg)
    out = load_ml_study(paths, study_id, now=cfg.clock(), default_rung_s=rung_s)
    if out is None:
        raise TowerError(404, "not_found", f"no ML study {study_id}")
    return wrap(StudyDetailOut, out, scope, shared=True)
