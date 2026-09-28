"""Incremental downloader: provider → parquet cache, only fetching uncached sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from arc.data.history.base import HistoricalDataProvider
    from arc.data.history.store import ParquetHistoryStore

log = structlog.get_logger()


class DownloadResult(BaseModel):
    provider: str
    underlying: str
    requested_sessions: int
    fetched_sessions: int
    skipped_cached: int
    rows: int
    error: str | None = None


def missing_ranges(
    sessions: Sequence[dt.date], cached: set[dt.date], max_sessions: int
) -> list[list[dt.date]]:
    """Split uncached *sessions* into runs of consecutive sessions (≤ *max_sessions* each).

    A cached session breaks a run so already-stored days are never re-fetched.
    """
    if max_sessions < 1:
        msg = "max_sessions must be >= 1"
        raise ValueError(msg)
    runs: list[list[dt.date]] = []
    cur: list[dt.date] = []
    for d in sessions:
        if d in cached:
            if cur:
                runs.append(cur)
                cur = []
            continue
        cur.append(d)
        if len(cur) >= max_sessions:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def download(
    provider: HistoricalDataProvider,
    store: ParquetHistoryStore,
    underlyings: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    max_dte: int = 60,
    refresh: bool = False,
    max_sessions_per_request: int = 10,
) -> list[DownloadResult]:
    """Fetch and cache EOD option rows for each underlying over trading sessions in [start, end].

    *start* is clamped to ``provider.earliest_date()``.  Already-cached sessions
    are skipped unless *refresh*.  A provider error on one ticker is recorded in
    its :class:`DownloadResult` and the loop continues with the next ticker.
    """
    start = max(start, provider.earliest_date())
    sessions = sessions_between(start, end)
    results: list[DownloadResult] = []
    for raw in underlyings:
        u = raw.upper()
        cached = set() if refresh else store.cached_dates(provider.name, u)
        runs = missing_ranges(sessions, cached, max_sessions_per_request)
        fetched = 0
        rows = 0
        error: str | None = None
        for run in runs:
            try:
                data = provider.fetch_option_eod(u, run[0], run[-1], max_dte=max_dte)
            except Exception as exc:  # noqa: BLE001 — record and continue with next ticker
                error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "history.download_failed", provider=provider.name, underlying=u, error=error
                )
                break
            store.write_range(provider.name, u, run, data)
            fetched += len(run)
            rows += len(data)
            log.info(
                "history.range_cached",
                provider=provider.name,
                underlying=u,
                start=run[0].isoformat(),
                end=run[-1].isoformat(),
                rows=len(data),
            )
        results.append(
            DownloadResult(
                provider=provider.name,
                underlying=u,
                requested_sessions=len(sessions),
                fetched_sessions=fetched,
                skipped_cached=sum(1 for d in sessions if d in cached),
                rows=rows,
                error=error,
            )
        )
    return results
