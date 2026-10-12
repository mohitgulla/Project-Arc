"""``GET /api/ops/*`` (E8.7d): the Ops & pipeline page.

Read-only like every tower route: a ``mode=ro`` connection per request and the effective
config (``routines.yaml`` + D26 overrides) read through :func:`deps.effective`.

E15.6 (D69), ``?arm=``: the arm's own rows come from its store (halts, order budget,
runs and their manifests, the context entries its chains read). Control-only inputs
and plumbing (session timeline, health strip, ops alerts, sources, LLM spend, config)
stay control's, flagged ``shared_from_control``; the universe is the arm's only when
it runs the Scout / Trending itself (``ArmPlan.arm_personas``).
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
    TimelineWindow,
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
from arc.tower.routes.deps import (  # noqa: TC001 - FastAPI dependencies
    Conn,
    ControlConn,
    ControlTower,
    Scope,
    Tower,
    effective,
    feed_config,
    open_ro,
)
from arc.tower.schemas import ErrorResponse
from arc.tower.scope import scoped, wrap

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["HEALTH_INTERVAL_S", "NOTICE_CHANNEL", "router"]

#: ``arc health check`` LaunchAgent cadence (``hermes/monitoring/install.sh`` INTERVAL:
#: 30 min, owner 2026-09-28). The health heartbeat is stale after 3x this.
HEALTH_INTERVAL_S = 1800

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    responses={503: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)

SessionOut = scoped(SessionResponse)
HealthStripOut = scoped(HealthStripResponse)
AlertsOut = scoped(AlertsResponse)
HaltsOut = scoped(HaltsResponse)
RunListOut = scoped(RunListResponse)
RunDetailOut = scoped(RunDetailResponse)
BudgetOut = scoped(BudgetResponse)
ContextOut = scoped(ContextResponse)
ContextEntryOut = scoped(ContextEntryResponse)
SourcesOut = scoped(SourcesResponse)
LlmOut = scoped(LlmResponse)
UniverseOut = scoped(UniverseResponse)
ConfigOut = scoped(ConfigResponse)


def _csv(v: str | None) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _log_path(cfg: ControlTower, log: Path) -> Path:
    return log if log.is_absolute() else cfg.db_path.resolve().parent / log


#: #arc-investor: routine notices go to its day thread (``arc.routines.heartbeat``), so
#: a manifest's ``notifications`` ts are messages there. Mirrors
#: ``arc.slack.client.CHANNEL_ARC_INVESTOR`` (a test pins them equal); importing it
#: would pull ``slack_sdk`` into the tower, which the import contract forbids.
NOTICE_CHANNEL = "C0C4NS1AL3X"


def _slack_channel() -> str:
    return NOTICE_CHANNEL


@router.get("/session", response_model=SessionOut, responses={422: {"model": ErrorResponse}})
def session(
    cfg: ControlTower,
    conn: ControlConn,
    scope: Scope,
    day: Annotated[str | None, Query()] = None,
    window: Annotated[TimelineWindow, Query()] = "day",
) -> SessionResponse:
    """Scheduled slots with each one's run, plus the loop row (D93): ``window=day`` is the
    ET day 00:00-24:00, ``window=rolling`` the last 8 h and the next 4 h (``day`` ignored)."""
    now = cfg.clock()
    try:
        d = resolve_day(day, now)
    except ValueError as exc:
        raise TowerError(422, "invalid_request", str(exc)) from exc
    _, routines = effective(cfg)
    out = load_session(conn, routines, now=now, day=d, window=window)
    return wrap(SessionOut, out, scope, shared=True)


@router.get("/health", response_model=HealthStripOut)
def health(cfg: ControlTower, conn: ControlConn, scope: Scope) -> HealthStripResponse:
    """Tick / health heartbeat ages, gateway and remote access, log size, with thresholds."""
    _, routines = effective(cfg)
    mon = routines.monitoring
    out = load_health(
        conn,
        now=cfg.clock(),
        tick_stale_s=int(mon.tick_stale_after.total_seconds()),
        health_every_s=HEALTH_INTERVAL_S,
        log_path=_log_path(cfg, mon.log.path),
        log_max_bytes=mon.log.max_bytes,
    )
    return wrap(HealthStripOut, out, scope, shared=True)


@router.get("/alerts", response_model=AlertsOut)
def alerts(cfg: ControlTower, conn: ControlConn, scope: Scope) -> AlertsResponse:
    """``ops_alerts``: open first, then resolved in the last 7 days."""
    return wrap(AlertsOut, load_alerts(conn, now=cfg.clock()), scope, shared=True)


@router.get("/halts", response_model=HaltsOut)
def halts(cfg: Tower, conn: Conn, scope: Scope) -> HaltsResponse:
    """Halts, active first, with the trades raised in each halt's window (an arm's own
    with ``?arm=``, e.g. a D69 ``book_invariant`` one)."""
    return wrap(HaltsOut, load_halts(conn, now=cfg.clock()), scope)


@router.get("/runs", response_model=RunListOut, responses={422: {"model": ErrorResponse}})
def runs(  # noqa: PLR0913 - one query parameter per filter
    cfg: Tower,
    conn: Conn,
    scope: Scope,
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
    out = load_runs(
        conn,
        now=now,
        jobs=_csv(job),
        statuses=_csv(status),
        day=d,
        chain=chain or None,
        page=page,
        size=size,
    )
    return wrap(RunListOut, out, scope)


@router.get(
    "/runs/{run_id}", response_model=RunDetailOut, responses={404: {"model": ErrorResponse}}
)
def run_detail(
    cfg: Tower, control: ControlTower, conn: Conn, scope: Scope, run_id: str
) -> RunDetailResponse:
    """The run's D27 manifest and trace (``arc context trace``), its chain and log tail."""
    _, routines = effective(cfg)
    try:
        out = load_run(
            conn,
            run_id,
            now=cfg.clock(),
            log_path=_log_path(control, routines.monitoring.log.path),
            slack_channel=_slack_channel(),
            labels=job_labels(routines),
        )
    except LookupError as exc:
        raise TowerError(404, "not_found", str(exc.args[0])) from exc
    return wrap(RunDetailOut, out, scope)


@router.get("/budget", response_model=BudgetOut | None)
def budget(cfg: Tower, conn: Conn, scope: Scope) -> BudgetResponse | None:
    """Today's D32 options-order budget from the local store (``null`` without E6.5);
    an arm's own orders with ``?arm=``."""
    settings, _ = effective(cfg)
    out = load_budget(conn, settings, now=cfg.clock())
    return None if out is None else wrap(BudgetOut, out, scope)


@router.get("/context", response_model=ContextOut)
def context(cfg: Tower, conn: Conn, scope: Scope) -> ContextResponse:
    """Active ``context_entries`` per kind, TTL left, latest entry, expired in 24 h (with
    ``?arm=``: the arm store's entries, i.e. what its chains read and wrote)."""
    _, routines = effective(cfg)
    return wrap(ContextOut, load_context(conn, routines, now=cfg.clock()), scope)


@router.get(
    "/context/{entry_id}",
    response_model=ContextEntryOut,
)
def context_entry(cfg: Tower, conn: Conn, scope: Scope, entry_id: str) -> ContextEntryResponse:
    """One context entry with its payload."""
    try:
        out = load_context_entry(conn, entry_id, now=cfg.clock())
    except LookupError as exc:
        raise TowerError(404, "not_found", str(exc.args[0])) from exc
    return wrap(ContextEntryOut, out, scope)


@router.get("/sources", response_model=SourcesOut)
def sources(cfg: ControlTower, conn: ControlConn, scope: Scope) -> SourcesResponse:
    """Per registry source: last fetch, docs today, budget skips, backoff, error rate.
    Sources run once, in control (shared by every arm)."""
    _, routines = effective(cfg)
    return wrap(SourcesOut, load_sources(conn, routines, now=cfg.clock()), scope, shared=True)


@router.get("/llm", response_model=LlmOut)
def llm(
    cfg: ControlTower,
    conn: ControlConn,
    scope: Scope,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> LlmResponse:
    """LLM calls, tokens and cost per day / persona / model (control's spend)."""
    return wrap(LlmOut, load_llm(conn, now=cfg.clock(), days=days), scope, shared=True)


@router.get("/universe", response_model=UniverseOut)
def universe(control: ControlTower, scope: Scope) -> UniverseResponse:
    """E12.6 (D51): the stored active list by tier, tier feeds, drops, ignored override.

    Control's list, unless the arm runs the Scout / Trending itself (then its own)."""
    cfg = feed_config(control, scope, "scout", "trending")
    shared = cfg is control
    settings, routines = effective(cfg)
    from arc.universe.config import universe_config

    yjob = routines.job("youtube.briefs")
    labels = {
        str(c["slug"]): str(c.get("label") or c["slug"])
        for c in ((yjob[1].options.get("channels") if yjob else None) or [])
        if isinstance(c, dict) and c.get("slug")
    }
    with open_ro(cfg) as conn:
        out = load_universe(
            conn,
            settings,
            now=cfg.clock(),
            director_diversification=routines.director_diversification.mode,
            velocity=universe_config(settings).trending.velocity,  # E20.4: universe.yaml
            channel_labels=labels,
        )
    return wrap(UniverseOut, out, scope, shared=shared)


@router.get("/config", response_model=ConfigOut)
def config(cfg: ControlTower, conn: ControlConn, scope: Scope) -> ConfigResponse:
    """The D26 effective config and override log (read-only). Control's: an arm runs
    control's overrides plus its spec overlay (shown on the Experiments page)."""
    base = cfg.settings
    if base is None:
        from arc.config import ArcSettings

        base = ArcSettings()
    _, routines = effective(cfg)
    out = load_config(conn, base, now=cfg.clock(), actor_names=routines.tower.actor_names)
    return wrap(ConfigOut, out, scope, shared=True)
