"""``GET /api/ops/*`` (E8.7d): the Ops & pipeline page.

Read-only like every tower route: a ``mode=ro`` connection per request and the effective
config (``routines.yaml`` + D26 overrides) read through :func:`deps.effective`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query

from arc.tower.api import TowerError
from arc.tower.data_ops import (
    AlertsResponse,
    BudgetResponse,
    ConfigResponse,
    ContextEntryResponse,
    ContextResponse,
    HaltsResponse,
    HealthStripResponse,
    LlmResponse,
    RunDetailResponse,
    RunListResponse,
    SessionResponse,
    SourcesResponse,
    job_labels,
    load_alerts,
    load_budget,
    load_config,
    load_context,
    load_context_entry,
    load_halts,
    load_health,
    load_llm,
    load_run,
    load_runs,
    load_session,
    load_sources,
    resolve_day,
)
from arc.tower.data_universe import UniverseResponse, load_universe
from arc.tower.routes.deps import Conn, Tower, effective  # noqa: TC001 - FastAPI dependencies
from arc.tower.schemas import ErrorResponse

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["HEALTH_INTERVAL_S", "NOTICE_CHANNEL", "router"]

#: ``arc health check`` LaunchAgent cadence (``hermes/monitoring/install.sh`` INTERVAL:
#: 30 min, owner 2026-09-28). The health heartbeat is stale after 3x this.
HEALTH_INTERVAL_S = 1800

router = APIRouter(prefix="/ops", tags=["ops"], responses={503: {"model": ErrorResponse}})


def _csv(v: str | None) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _log_path(cfg: Tower, log: Path) -> Path:
    return log if log.is_absolute() else cfg.db_path.resolve().parent / log


#: #arc-investor: routine notices go to its day thread (``arc.routines.heartbeat``), so
#: a manifest's ``notifications`` ts are messages there. Mirrors
#: ``arc.slack.client.CHANNEL_ARC_INVESTOR`` (a test pins them equal); importing it
#: would pull ``slack_sdk`` into the tower, which the import contract forbids.
NOTICE_CHANNEL = "C0C4NS1AL3X"


def _slack_channel() -> str:
    return NOTICE_CHANNEL


@router.get("/session", response_model=SessionResponse, responses={422: {"model": ErrorResponse}})
def session(cfg: Tower, conn: Conn, day: Annotated[str | None, Query()] = None) -> SessionResponse:
    """The ET day's scheduled slots (05:00-22:00) with each one's run, plus the loop row."""
    now = cfg.clock()
    try:
        d = resolve_day(day, now)
    except ValueError as exc:
        raise TowerError(422, "invalid_request", str(exc)) from exc
    _, routines = effective(cfg)
    return load_session(conn, routines, now=now, day=d)


@router.get("/health", response_model=HealthStripResponse)
def health(cfg: Tower, conn: Conn) -> HealthStripResponse:
    """Tick / health heartbeat ages, gateway and remote access, log size, with thresholds."""
    _, routines = effective(cfg)
    mon = routines.monitoring
    return load_health(
        conn,
        now=cfg.clock(),
        tick_stale_s=int(mon.tick_stale_after.total_seconds()),
        health_every_s=HEALTH_INTERVAL_S,
        log_path=_log_path(cfg, mon.log.path),
        log_max_bytes=mon.log.max_bytes,
    )


@router.get("/alerts", response_model=AlertsResponse)
def alerts(cfg: Tower, conn: Conn) -> AlertsResponse:
    """``ops_alerts``: open first, then resolved in the last 7 days."""
    return load_alerts(conn, now=cfg.clock())


@router.get("/halts", response_model=HaltsResponse)
def halts(cfg: Tower, conn: Conn) -> HaltsResponse:
    """Halts, active first, with the trades raised in each halt's window."""
    return load_halts(conn, now=cfg.clock())


@router.get("/runs", response_model=RunListResponse, responses={422: {"model": ErrorResponse}})
def runs(  # noqa: PLR0913 - one query parameter per filter
    cfg: Tower,
    conn: Conn,
    job: Annotated[str | None, Query(description="Comma list of jobs")] = None,
    status: Annotated[
        str | None, Query(description="Comma list: ok, failed, skipped, running, no_change")
    ] = None,
    day: Annotated[str | None, Query(description="today | yesterday | YYYY-MM-DD")] = None,
    chain: Annotated[str | None, Query(description="Chain id (or its root run id)")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunListResponse:
    """``routine_runs``, newest first, filtered and paged."""
    now = cfg.clock()
    try:
        d = resolve_day(day, now) if day else None
    except ValueError as exc:
        raise TowerError(422, "invalid_request", str(exc)) from exc
    return load_runs(
        conn,
        now=now,
        jobs=_csv(job),
        statuses=_csv(status),
        day=d,
        chain=chain or None,
        page=page,
        size=size,
    )


@router.get(
    "/runs/{run_id}", response_model=RunDetailResponse, responses={404: {"model": ErrorResponse}}
)
def run_detail(cfg: Tower, conn: Conn, run_id: str) -> RunDetailResponse:
    """The run's D27 manifest and trace (``arc context trace``), its chain and log tail."""
    _, routines = effective(cfg)
    try:
        return load_run(
            conn,
            run_id,
            now=cfg.clock(),
            log_path=_log_path(cfg, routines.monitoring.log.path),
            slack_channel=_slack_channel(),
            labels=job_labels(routines),
        )
    except LookupError as exc:
        raise TowerError(404, "not_found", str(exc.args[0])) from exc


@router.get("/budget", response_model=BudgetResponse | None)
def budget(cfg: Tower, conn: Conn) -> BudgetResponse | None:
    """Today's D32 options-order budget from the local store (``null`` without E6.5)."""
    settings, _ = effective(cfg)
    return load_budget(conn, settings, now=cfg.clock())


@router.get("/context", response_model=ContextResponse)
def context(cfg: Tower, conn: Conn) -> ContextResponse:
    """Active ``context_entries`` per kind, TTL left, latest entry, expired in 24 h."""
    _, routines = effective(cfg)
    return load_context(conn, routines, now=cfg.clock())


@router.get(
    "/context/{entry_id}",
    response_model=ContextEntryResponse,
    responses={404: {"model": ErrorResponse}},
)
def context_entry(cfg: Tower, conn: Conn, entry_id: str) -> ContextEntryResponse:
    """One context entry with its payload."""
    try:
        return load_context_entry(conn, entry_id, now=cfg.clock())
    except LookupError as exc:
        raise TowerError(404, "not_found", str(exc.args[0])) from exc


@router.get("/sources", response_model=SourcesResponse)
def sources(cfg: Tower, conn: Conn) -> SourcesResponse:
    """Per registry source: last fetch, docs today, budget skips, backoff, error rate."""
    _, routines = effective(cfg)
    return load_sources(conn, routines, now=cfg.clock())


@router.get("/llm", response_model=LlmResponse)
def llm(cfg: Tower, conn: Conn, days: Annotated[int, Query(ge=1, le=365)] = 30) -> LlmResponse:
    """LLM calls, tokens and cost per day / persona / model."""
    return load_llm(conn, now=cfg.clock(), days=days)


@router.get("/universe", response_model=UniverseResponse)
def universe(cfg: Tower, conn: Conn) -> UniverseResponse:
    """E12.6 (D51): the stored active list by tier, tier feeds, drops, ignored override."""
    settings, routines = effective(cfg)
    return load_universe(
        conn,
        settings,
        now=cfg.clock(),
        director_diversification=routines.director_diversification.mode,
    )


@router.get("/config", response_model=ConfigResponse)
def config(cfg: Tower, conn: Conn) -> ConfigResponse:
    """The D26 effective config and override log (read-only)."""
    base = cfg.settings
    if base is None:
        from arc.config import ArcSettings

        base = ArcSettings()
    _, routines = effective(cfg)
    return load_config(conn, base, now=cfg.clock(), actor_names=routines.tower.actor_names)
