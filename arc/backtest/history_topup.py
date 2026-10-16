"""Nightly ThetaData top-up of the C500 option-history cache (E7.13, D84).

After the paid Value month the cache keeps growing on the free tier: EOD NBBO only
(no open interest), 1 year back, 20 req/min. Each trading night ``history.topup``
fetches, per name of the configured checkpoint, the sessions after its last cached one
(the E7.6 resumable :func:`arc.data.history.download.download`: one request covers a
short run of sessions), refreshes the underlying's raw daily closes (Alpaca SIP, batched)
and appends the new sessions' ``theta_backfill`` IV point for names whose series has
already been seeded (live writes of a never-seeded name wait for the owner-approved
backfill, :mod:`arc.backtest.theta_iv`).

Order is **behind-first**: the names whose cache ends earliest go first, then the pull
list's priority. No new name starts after ``max_runtime_s``; the rest (``deferred``)
are first in line the next night. A Terminal that isn't listening is a skip
(``terminal_down``), never a failure.
"""

from __future__ import annotations

import datetime as _dt
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

import pandas as pd
import structlog

from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from arc.data.history.base import HistoricalDataProvider
    from arc.data.history.download import DownloadResult
    from arc.data.history.store import ParquetHistoryStore

log = structlog.get_logger(__name__)

PROVIDER = "thetadata"
CLOSES_BATCH = 100


class BarsBatch(Protocol):
    """Raw daily bars for many symbols (``close`` + ``volume`` by session date)."""

    def daily_bars(
        self, symbols: Sequence[str], start: _dt.date, end: _dt.date
    ) -> dict[str, pd.DataFrame]: ...


class TerminalDownError(RuntimeError):
    """The Theta Terminal isn't listening (``history.topup`` records ``terminal_down``)."""


def terminal_up(url: str, *, timeout_s: float = 3.0) -> bool:
    """True when something accepts a TCP connection at the Terminal's host:port."""
    u = urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or 25503
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def plan(
    store: ParquetHistoryStore,
    names: Sequence[str],
    *,
    end: _dt.date,
    earliest: _dt.date,
) -> list[tuple[str, _dt.date]]:
    """``(ticker, first missing session)`` for names behind *end*, behind-first.

    A name's gap starts after its last cached session (never before *earliest*, the
    tier's first servable day); a name with nothing cached starts at *earliest*. Names
    already at *end* are left out. Ties keep *names* (priority) order.
    """
    out: list[tuple[int, int, str, _dt.date]] = []
    for pos, t in enumerate(names):
        cached = store.cached_dates(PROVIDER, t)
        last = max((d for d in cached if d <= end), default=None)
        if last is not None and last >= end:
            continue
        first = earliest if last is None else max(earliest, last + _dt.timedelta(days=1))
        todo = [d for d in sessions_between(first, end) if d not in cached]
        if not todo:
            continue
        behind = (end - (last or earliest)).days
        out.append((-behind, pos, t, todo[0]))
    out.sort()
    return [(t, first) for _, _, t, first in out]


def refresh_closes(
    root: Path,
    symbols: Sequence[str],
    start: _dt.date,
    end: _dt.date,
    source: BarsBatch,
    *,
    batch: int = CLOSES_BATCH,
) -> int:
    """Merge raw daily close + volume for [start, end] into ``underlying_daily/``.

    Only symbols missing a close on some session in the window are asked for. Returns
    the number of symbols written.
    """
    from arc.backtest.underlying import UnderlyingStore

    store = UnderlyingStore(root)
    sessions = set(sessions_between(start, end))
    need = []
    for s in dict.fromkeys(x.upper() for x in symbols):
        have = set(store.read(s).index)
        if not sessions <= have:
            need.append(s)
    n = 0
    for i in range(0, len(need), batch):
        chunk = need[i : i + batch]
        got = source.daily_bars(chunk, start, end)
        for s in chunk:
            fresh = got.get(s)
            if fresh is None or fresh.empty:
                continue
            old = store.read_bars(s)
            merged = pd.concat([old, fresh]) if not old.empty else fresh
            merged = merged[~pd.Index(merged.index).duplicated(keep="last")].sort_index()
            store.write_bars(s, merged)
            n += 1
    return n


@dataclass
class TopupResult:
    end: _dt.date
    names: int
    behind: int
    results: list[DownloadResult] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    closes_written: int = 0
    closes_error: str | None = None
    iv: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0

    @property
    def sessions_added(self) -> int:
        return sum(r.fetched_sessions for r in self.results)

    @property
    def errors(self) -> list[DownloadResult]:
        return [r for r in self.results if r.error]

    def summary(self) -> str:
        if not self.behind:
            head = f"{self.names} names current through {self.end}"
        else:
            done = len(self.results) - len(self.errors)
            head = (
                f"{done}/{self.behind} behind names topped up through {self.end}, "
                f"{self.sessions_added} sessions added"
            )
        parts = [head]
        if self.errors:
            parts.append(f"{len(self.errors)} errors (e.g. {self.errors[0].underlying}: "
                         f"{(self.errors[0].error or '')[:80]})")  # fmt: skip
        if self.deferred:
            parts.append(f"{len(self.deferred)} deferred by max_runtime_s")
        if self.closes_error:
            parts.append(f"closes failed ({self.closes_error[:80]})")
        if self.iv:
            parts.append(f"theta_backfill +{self.iv.get('rows', 0)} IV rows")
        return " · ".join(parts)

    def metrics(self) -> dict[str, Any]:
        return {
            "end": self.end.isoformat(),
            "names": self.names,
            "behind": self.behind,
            "topped_up": len(self.results) - len(self.errors),
            "sessions_added": self.sessions_added,
            "requests_rows": sum(r.rows for r in self.results),
            "errors": {r.underlying: r.error for r in self.errors},
            "deferred": self.deferred,
            "closes_written": self.closes_written,
            "closes_error": self.closes_error,
            "iv": self.iv,
            "wall_s": round(self.wall_s, 1),
        }


def seeded_names(conn: sqlite3.Connection, names: Sequence[str]) -> dict[str, _dt.date]:
    """Names with a ``theta_backfill`` series already, and its last stored day."""
    from arc.iv.store import THETA

    want = {t.upper() for t in names}
    rows = conn.execute(
        "SELECT ticker, MAX(day) FROM iv_daily WHERE source = ? GROUP BY ticker", (THETA,)
    ).fetchall()
    return {str(t): _dt.date.fromisoformat(str(d)) for t, d in rows if str(t) in want and d}


def run_topup(
    *,
    data_dir: Path,
    names: Sequence[str],
    end: _dt.date,
    earliest: _dt.date,
    provider_factory: Callable[[], HistoricalDataProvider],
    bars: BarsBatch | None,
    max_runtime_s: float,
    need_oi: bool,
    conn: sqlite3.Connection | None = None,
    now: _dt.datetime | None = None,
    iv_append: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> TopupResult:
    """Fetch every behind name's missing sessions (capped by time), then closes + IV."""
    from arc.data.history.download import download
    from arc.data.history.store import ParquetHistoryStore

    t0 = clock()
    store = ParquetHistoryStore(data_dir)
    todo = plan(store, names, end=end, earliest=earliest)
    res = TopupResult(end=end, names=len(names), behind=len(todo))
    provider = provider_factory() if todo else None
    for i, (t, first) in enumerate(todo):
        if clock() - t0 >= max_runtime_s:
            res.deferred = [x for x, _ in todo[i:]]
            break
        assert provider is not None  # noqa: S101 - built whenever todo is non-empty
        res.results.extend(download(provider, store, [t], first, end, need_oi=need_oi))
    fetched = [r for r in res.results if r.fetched_sessions]
    if fetched and bars is not None:
        lo = min(first for t, first in todo if t in {r.underlying for r in fetched})
        try:
            res.closes_written = refresh_closes(
                data_dir, [r.underlying for r in fetched], lo, end, bars
            )
        except Exception as exc:  # noqa: BLE001 - closes retry next night; IV waits for them
            res.closes_error = f"{type(exc).__name__}: {exc}"
            log.warning("history.topup_closes_failed", error=res.closes_error)
    if iv_append and conn is not None and now is not None:
        from arc.backtest.theta_iv import seed

        seeded = seeded_names(conn, names)
        if seeded:
            since = min(seeded.values()) + _dt.timedelta(days=1)
            rep = seed(conn, data_dir, sorted(seeded), now=now, since=since, until=end)
            res.iv = rep.metrics()
    res.wall_s = clock() - t0
    return res
