"""``GET /api/health`` and ``GET /api/meta`` (E8.7): shell data for every page."""

from __future__ import annotations

import functools
import importlib.metadata
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter

from arc.tower.api import TowerError
from arc.tower.routes.deps import Conn, Tower, effective
from arc.tower.schemas import (
    STALE_FACTOR,
    AppInfo,
    Cadence,
    ErrorResponse,
    GateCaps,
    HealthResponse,
    MetaResponse,
)

if TYPE_CHECKING:
    import datetime as _dt

    from arc.routines.config import JobSpec, RoutinesConfig

__all__ = ["cadences", "router"]

router = APIRouter(tags=["meta"], responses={503: {"model": ErrorResponse}})

REPO_ROOT = Path(__file__).resolve().parents[3]
CADENCE_JOBS = ("monitor", "auditor")
_DAY = 86_400


@functools.cache
def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH, fixed argv
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@functools.cache
def _version() -> str:
    try:
        return importlib.metadata.version("arc")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - always installed
        return "unknown"


def _every_s(spec: JobSpec) -> int:
    """Nominal seconds between runs: ``every``, or the smallest gap between schedule times."""
    if spec.every is not None:
        return int(spec.every.total_seconds())
    if spec.schedule:
        secs = sorted(t.hour * 3600 + t.minute * 60 + t.second for t in spec.schedule)
        gaps = [b - a for a, b in zip(secs, secs[1:], strict=False)]
        gaps.append(_DAY - secs[-1] + secs[0])  # wrap to the next day
        return min(gaps)
    return _DAY  # event-driven: no cadence to be late against; a day is the loosest bound


def cadences(routines: RoutinesConfig) -> dict[str, Cadence]:
    """``monitor``, ``auditor`` and ``tick`` cadences with stale = 3 x cadence (§3, §7)."""
    out: dict[str, Cadence] = {}
    for name in CADENCE_JOBS:
        found = routines.job(name)
        if found is None:
            continue
        spec = found[1]
        every = _every_s(spec)
        out[name] = Cadence(
            job=name,
            label=spec.cadence,
            every_s=every,
            stale_after_s=STALE_FACTOR * every,
            window=str(spec.window) if spec.window else None,
            days=spec.days_label,
        )
    tick = int(routines.tick.interval.total_seconds())
    out["tick"] = Cadence(
        job="tick",
        label=f"every {tick // 60}m (Hermes cron)",
        every_s=tick,
        stale_after_s=STALE_FACTOR * tick,
    )
    # E8.8b: the Overview status row judges the health heartbeat like Ops does.
    from arc.tower.routes.ops import HEALTH_INTERVAL_S

    out["health"] = Cadence(
        job="health",
        label=f"every {HEALTH_INTERVAL_S // 60}m (LaunchAgent)",
        every_s=HEALTH_INTERVAL_S,
        stale_after_s=STALE_FACTOR * HEALTH_INTERVAL_S,
    )
    return out


def _now(cfg: Tower) -> _dt.datetime:
    return cfg.clock()


@router.get("/health", response_model=HealthResponse)
def health(cfg: Tower, conn: Conn) -> HealthResponse:
    """200 when the audit store opens read-only and answers a query."""
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - db_conn already maps open failures
        raise TowerError(503, "db_error", str(exc)) from exc
    return HealthResponse(status="ok", db=str(cfg.db_path), as_of=_now(cfg))


@router.get("/meta", response_model=MetaResponse)
def meta(cfg: Tower) -> MetaResponse:
    """App/env info, cadences with stale thresholds, gate caps and client defaults."""
    if not cfg.db_path.is_file():
        raise TowerError(503, "db_unavailable", f"audit store not found: {cfg.db_path}")
    settings, routines = effective(cfg)
    return MetaResponse(
        as_of=_now(cfg),
        app=AppInfo(version=_version(), git_sha=_git_sha()),
        env=settings.env.value,
        account_profile=settings.account_profile,
        config_version=int(getattr(settings, "config_version", 0) or 0),
        refresh_interval_s=cfg.refresh_s,
        lookback_days=cfg.lookback_days,
        cadences=cadences(routines),
        gate_caps=GateCaps(
            portfolio_delta_cap=settings.portfolio_delta_cap,
            portfolio_vega_cap_pct=settings.portfolio_vega_cap_pct,
        ),
    )
