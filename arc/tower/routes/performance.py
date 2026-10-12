"""``GET /api/performance`` and ``GET /api/performance/breakdown`` (E8.7c): the Performance page.

Responses are cached in memory for 60 s per (query, DB file mtime + size), so the SPA's
poll does not re-scan the journal every time; any write to the store (a new mtime)
misses the cache. The cache lives on ``app.state`` (no module globals).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - FastAPI reads annotations at runtime
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from arc.tower.api import TowerError
from arc.tower.data_funnel import FunnelRange, FunnelReport, funnel_bounds, load_funnel_report
from arc.tower.data_performance import (
    BreakdownBy,
    BreakdownResponse,
    Compare,
    PerformanceResponse,
    PeriodError,
    Preset,
    load_breakdown,
    load_performance,
)
from arc.tower.data_sleeves import OTHER, named_sleeves
from arc.tower.routes.deps import Conn, ControlConn, ControlTower, Scope, Tower, effective
from arc.tower.schemas import ErrorResponse
from arc.tower.scope import scoped, wrap
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from arc.account_profiles import DteSleeve
    from arc.tower.api import TowerConfig

__all__ = ["CACHE_TTL_S", "ResponseCache", "router"]

_SLEEVE_RE = r"^[a-z][a-z0-9_]*$"

CACHE_TTL_S = 60.0
_MAX_ENTRIES = 64

router = APIRouter(
    tags=["performance"], responses={503: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
)

# E15.6: Performance and its breakdown read the scoped store (an arm's virtual-account
# equity and its own closed trades); the funnel counts control's shared inputs (docs,
# Scout/Scalp candidates), so an arm view gets control's, flagged shared_from_control.
PerformanceOut = scoped(PerformanceResponse)
BreakdownOut = scoped(BreakdownResponse)
FunnelOut = scoped(FunnelReport)


@dataclass
class ResponseCache:
    """(key, db stamp) -> response, for *ttl* seconds (monotonic clock)."""

    ttl: float = CACHE_TTL_S
    clock: Callable[[], float] = time.monotonic
    _items: dict[tuple[Any, ...], tuple[float, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    hits: int = 0
    misses: int = 0

    def get_or_build(self, key: tuple[Any, ...], build: Callable[[], Any]) -> Any:
        now = self.clock()
        with self._lock:
            hit = self._items.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                self.hits += 1
                return hit[1]
        value = build()
        with self._lock:
            self.misses += 1
            if len(self._items) >= _MAX_ENTRIES:
                self._items = {k: v for k, v in self._items.items() if now - v[0] < self.ttl}
                if len(self._items) >= _MAX_ENTRIES:
                    self._items.clear()
            self._items[key] = (now, value)
        return value


def response_cache(request: Request) -> ResponseCache:
    cache: ResponseCache | None = getattr(request.app.state, "performance_cache", None)
    if cache is None:
        cache = ResponseCache()
        request.app.state.performance_cache = cache
    return cache


Cache = Annotated[ResponseCache, Depends(response_cache)]


def _stamp(path: Path) -> tuple[int, int, int, int]:
    """The store's (mtime_ns, size) plus its WAL's, once it has frames.

    Any committed write moves one of them. An empty ``-wal`` (created by the first
    read-only open) is ignored, so the first poll after start-up still hits.
    """
    try:
        st = path.stat()
        main = (st.st_mtime_ns, st.st_size)
    except OSError:
        main = (0, 0)
    try:
        wal = path.with_name(path.name + "-wal").stat()
        frames = (wal.st_mtime_ns, wal.st_size) if wal.st_size else (0, 0)
    except OSError:
        frames = (0, 0)
    return (main[0], main[1], frames[0], frames[1])


def _sleeves(cfg: TowerConfig, sleeve: str | None) -> list[DteSleeve]:
    """E22.5: the profile's named sleeves; 422 for a *sleeve* filter that names none."""
    sleeves = named_sleeves(effective(cfg)[0])
    if sleeve is not None and sleeve != OTHER and sleeve not in {s.name for s in sleeves}:
        names = ", ".join([*(s.name for s in sleeves), OTHER])
        msg = f"unknown sleeve {sleeve!r} (profile sleeves: {names})"
        raise TowerError(422, "invalid_request", msg)
    return sleeves


@router.get(
    "/performance",
    response_model=PerformanceOut,
    responses={422: {"model": ErrorResponse}},
)
def performance(  # noqa: PLR0913 - one query parameter per control
    cfg: Tower,
    conn: Conn,
    cache: Cache,
    scope: Scope,
    preset: Annotated[Preset, Query()] = "90d",
    # TODO(cleanup PR): the SPA stopped sending ``compare`` in E8.8c; drop the parameter and
    # the compare_* response fields once no caller sends it. Default ``none`` skips the work.
    compare: Annotated[Compare, Query()] = "none",
    include_tests: Annotated[bool, Query()] = False,
    date_from: Annotated[_dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[_dt.date | None, Query(alias="to")] = None,
    sleeve: Annotated[str | None, Query(pattern=_SLEEVE_RE)] = None,
) -> PerformanceResponse:
    """Every Performance card for the period, with the comparison computed server-side.

    ``sleeve`` (E22.5, D97) filters the trade cards to one entry-DTE sleeve (or ``other``).
    """
    now = cfg.clock()
    sleeves = _sleeves(cfg, sleeve)
    stamp = _stamp(cfg.db_path)
    windows = tuple((s.name, s.dte_min, s.dte_max, s.max_positions) for s in sleeves)
    key = (
        "perf", preset, compare, include_tests, date_from, date_to, sleeve, windows,
        now.date(), str(cfg.db_path), stamp,
    )  # fmt: skip

    def build() -> PerformanceResponse:
        try:
            return load_performance(
                conn,
                now=now,
                preset=preset,
                compare=compare,
                include_tests=include_tests,
                date_from=date_from,
                date_to=date_to,
                sleeve=sleeve,
                sleeves=sleeves,
            )
        except PeriodError as exc:
            raise TowerError(422, "invalid_request", str(exc)) from exc

    result: PerformanceResponse = cache.get_or_build(key, build)
    return wrap(PerformanceOut, result, scope)


@router.get(
    "/performance/breakdown",
    response_model=BreakdownOut,
    responses={422: {"model": ErrorResponse}},
)
def performance_breakdown(  # noqa: PLR0913 - one query parameter per control
    cfg: Tower,
    conn: Conn,
    cache: Cache,
    scope: Scope,
    by: Annotated[BreakdownBy, Query()] = "ticker",
    preset: Annotated[Preset, Query()] = "90d",
    include_tests: Annotated[bool, Query()] = False,
    date_from: Annotated[_dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[_dt.date | None, Query(alias="to")] = None,
    sleeve: Annotated[str | None, Query(pattern=_SLEEVE_RE)] = None,
) -> BreakdownResponse:
    """One breakdown tab: closed trades grouped by *by*, ranked by P&L."""
    now = cfg.clock()
    sleeves = _sleeves(cfg, sleeve)
    windows = tuple((s.name, s.dte_min, s.dte_max) for s in sleeves)
    key = (
        "bd", by, preset, include_tests, date_from, date_to, sleeve, windows, now.date(),
        str(cfg.db_path), _stamp(cfg.db_path),
    )  # fmt: skip

    def build() -> BreakdownResponse:
        try:
            return load_breakdown(
                conn,
                now=now,
                by=by,
                preset=preset,
                include_tests=include_tests,
                date_from=date_from,
                date_to=date_to,
                sleeve=sleeve,
                sleeves=sleeves,
            )
        except PeriodError as exc:
            raise TowerError(422, "invalid_request", str(exc)) from exc

    result: BreakdownResponse = cache.get_or_build(key, build)
    return wrap(BreakdownOut, result, scope)


@router.get(
    "/performance/funnel",
    response_model=FunnelOut,
    responses={422: {"model": ErrorResponse}},
)
def performance_funnel(  # noqa: PLR0913 - one query parameter per control
    cfg: ControlTower,
    conn: ControlConn,
    cache: Cache,
    scope: Scope,
    range: Annotated[FunnelRange, Query()] = "1W",  # noqa: A002 - the URL parameter name
    date_from: Annotated[_dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[_dt.date | None, Query(alias="to")] = None,
) -> FunnelReport:
    """E13.14 (D56): the idea funnel (docs -> candidates -> pool -> shortlist ->
    structures -> proposals -> fills) per feed for a day range; 60 s cache."""
    now = cfg.clock()
    try:
        today = now.astimezone(ET).date()
        first, last = funnel_bounds(today, range, since=date_from, until=date_to)
    except ValueError as exc:
        raise TowerError(422, "invalid_request", str(exc)) from exc
    key = ("funnel", first, last, _stamp(cfg.db_path))

    def build() -> FunnelReport:
        _, routines = effective(cfg)
        return load_funnel_report(conn, since=first, until=last, routines=routines)

    result: FunnelReport = cache.get_or_build(key, build)
    return wrap(FunnelOut, result, scope, shared=True)
