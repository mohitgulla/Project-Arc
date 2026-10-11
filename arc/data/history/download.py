"""Incremental downloader: provider → parquet cache, only fetching uncached sessions.

E7.6 (D84) adds, for the ThetaData Value-tier pull:

- a bounded worker pool across tickers (priority order = input order);
- span-based runs sized to the provider's adaptive chunk, so each run is one
  provider request and each session file is written as soon as its run lands;
- open-interest completeness: with ``need_oi`` a session counts as cached only
  when its file carries OI; sessions with quotes but no OI get an OI-only fill;
- a JSONL run ledger + periodic progress lines (:class:`RunLedger`);
- a no-network cost estimate (:func:`plan_download`).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import BaseModel, Field

from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from arc.data.history.base import HistoricalDataProvider
    from arc.data.history.store import ParquetHistoryStore
    from arc.data.history.thetadata import RequestLog

log = structlog.get_logger()

#: Plan defaults (D84 sizing note, 2026-10-10): a liquid name's ≤60 DTE chain is a few
#: thousand contracts per session; CSV over the wire is ~10x the parquet size.
PLAN_DEFAULT_ROWS_PER_DAY = 2_000.0
PLAN_DEFAULT_PARQUET_BYTES_PER_ROW = 45.0
PLAN_EOD_CSV_BYTES_PER_ROW = 200.0
PLAN_OI_CSV_BYTES_PER_ROW = 70.0
PLAN_DEFAULT_SECONDS_PER_REQUEST = 3.5


class _ChunkedProvider(Protocol):
    """Optional provider surface (ThetaData) used for span-sized runs and OI fills."""

    def chunk_for(self, underlying: str) -> int: ...

    def fetch_open_interest(
        self, underlying: str, start: dt.date, end: dt.date, *, max_dte: int
    ) -> dict[tuple[dt.date, str], float]: ...


class DownloadResult(BaseModel):
    provider: str
    underlying: str
    requested_sessions: int
    fetched_sessions: int
    skipped_cached: int
    rows: int
    error: str | None = None
    oi_filled_sessions: int = 0
    failed_ranges: list[tuple[dt.date, dt.date]] = Field(default_factory=list)
    seconds: float = 0.0


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


def span_runs(
    sessions: Sequence[dt.date], skip: set[dt.date], span_days: Callable[[], int]
) -> Iterator[list[dt.date]]:
    """Yield runs of consecutive non-*skip* sessions spanning ≤ ``span_days()`` calendar days.

    ``span_days`` is re-read for every run, so a provider's adaptive chunk
    (halved on a timeout, grown on a small chain) sizes the next run.
    """
    i = 0
    n = len(sessions)
    while i < n:
        if sessions[i] in skip:
            i += 1
            continue
        first = sessions[i]
        limit = first + dt.timedelta(days=max(1, span_days()) - 1)
        run: list[dt.date] = []
        while i < n and sessions[i] not in skip and sessions[i] <= limit:
            run.append(sessions[i])
            i += 1
        yield run


# ---------------------------------------------------------------------------
# run ledger + progress
# ---------------------------------------------------------------------------


class RunLedger:
    """Append-only JSONL ledger for one download run, plus live progress lines.

    Thread-safe: workers call :meth:`request` (provider HTTP calls) and
    :meth:`event` (range written / failed).  Every *progress_every* requests a
    one-line summary goes to *out* (done/total requests, rows, GB, req/s, ETA).
    """

    def __init__(
        self,
        path: Path | None,
        *,
        out: Callable[[str], Any] | None = None,
        progress_every: int = 25,
        total_requests: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self._out = out
        self._every = max(1, progress_every)
        self.total_requests = total_requests
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self.requests = 0
        self.rows = 0
        self.bytes = 0
        self.failed = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, rec: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, sort_keys=True) + "\n")

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._append({"event": kind, **fields})

    def request(self, entry: RequestLog) -> None:
        with self._lock:
            self.requests += 1
            self.rows += entry.rows
            self.bytes += entry.bytes
            if entry.error:
                self.failed += 1
            self._append({"event": "request", **entry.model_dump(mode="json")})
            if self._out is not None and self.requests % self._every == 0:
                self._out(self.progress_line())

    def progress_line(self) -> str:
        elapsed = max(1e-9, self._clock() - self._t0)
        rate = self.requests / elapsed
        total = self.total_requests
        done = f"{self.requests}/{total}" if total else f"{self.requests}"
        eta = _fmt_duration(max(0, total - self.requests) / rate) if total and rate > 0 else "?"
        return (
            f"[history] requests {done} · rows {self.rows:,} · "
            f"{self.bytes / 1e9:.2f} GB · {rate:.2f} req/s · ETA {eta}"
        )


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    d, s = divmod(s, 86_400)
    h, s = divmod(s, 3_600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def _download_one(
    provider: HistoricalDataProvider,
    store: ParquetHistoryStore,
    u: str,
    sessions: Sequence[dt.date],
    *,
    max_dte: int,
    refresh: bool,
    max_sessions_per_request: int,
    need_oi: bool,
    ledger: RunLedger | None,
) -> DownloadResult:
    t0 = time.monotonic()
    cached = set() if refresh else store.cached_dates(provider.name, u)
    window = set(sessions)
    oi_done = store.oi_dates(provider.name, u) & window if need_oi and not refresh else set()
    quotes_only = (cached & window) - oi_done if need_oi else set()
    chunked: _ChunkedProvider | None = (
        provider if hasattr(provider, "chunk_for") else None  # type: ignore[assignment]
    )
    if chunked is not None:
        runs: Iterator[list[dt.date]] | list[list[dt.date]] = span_runs(
            sessions, cached, lambda: chunked.chunk_for(u)
        )
    else:
        runs = missing_ranges(sessions, cached, max_sessions_per_request)

    fetched = rows = oi_filled = 0
    error: str | None = None
    failed: list[tuple[dt.date, dt.date]] = []

    def fail(exc: Exception, first: dt.date, stage: str) -> None:
        nonlocal error
        error = f"{type(exc).__name__}: {exc}"
        # Everything from here to the window end is left for a re-run.
        failed.append((first, sessions[-1]))
        log.error("history.download_failed", provider=provider.name, underlying=u, error=error)
        if ledger is not None:
            ledger.event(
                "range_failed", ticker=u, stage=stage, start=first, end=sessions[-1], error=error
            )

    for run in runs:
        try:
            data = provider.fetch_option_eod(u, run[0], run[-1], max_dte=max_dte)
        except Exception as exc:  # noqa: BLE001 — record and continue with next ticker
            fail(exc, run[0], "quotes")
            break
        store.write_range(provider.name, u, run, data, with_oi=need_oi)
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
        if ledger is not None:
            ledger.event("range_cached", ticker=u, start=run[0], end=run[-1], rows=len(data))

    if error is None and quotes_only and chunked is not None:
        # Resume: quotes cached without OI (e.g. an earlier --no-oi run) -> OI-only fill.
        ordered = [d for d in sessions if d in quotes_only]
        for run in span_runs(ordered, set(), lambda: chunked.chunk_for(u)):
            try:
                oi = chunked.fetch_open_interest(u, run[0], run[-1], max_dte=max_dte)
            except Exception as exc:  # noqa: BLE001
                fail(exc, run[0], "open_interest")
                break
            for d in run:
                store.add_open_interest(provider.name, u, d, oi)
            oi_filled += len(run)
            if ledger is not None:
                ledger.event("oi_filled", ticker=u, start=run[0], end=run[-1], values=len(oi))

    return DownloadResult(
        provider=provider.name,
        underlying=u,
        requested_sessions=len(sessions),
        fetched_sessions=fetched,
        skipped_cached=sum(1 for d in sessions if d in cached),
        rows=rows,
        error=error,
        oi_filled_sessions=oi_filled,
        failed_ranges=failed,
        seconds=round(time.monotonic() - t0, 3),
    )


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
    concurrency: int = 1,
    need_oi: bool = False,
    ledger: RunLedger | None = None,
) -> list[DownloadResult]:
    """Fetch and cache EOD option rows for each underlying over trading sessions in [start, end].

    *start* is clamped to ``provider.earliest_date()``.  Already-cached sessions
    are skipped unless *refresh*.  A provider error on one ticker is recorded in
    its :class:`DownloadResult` (with the failed range) and the other tickers go on.

    *concurrency* > 1 runs that many tickers at once (one worker per ticker, so
    each ticker's session files are only written by one thread); tickers start
    in input order, so a priority-ordered list finishes front-first.  Results
    are returned in input order.

    *need_oi* (ThetaData Value+) treats a session as cached only when its file
    carries open interest; quote-only sessions get an OI-only fill.
    """
    if concurrency < 1:
        msg = "concurrency must be >= 1"
        raise ValueError(msg)
    start = max(start, provider.earliest_date())
    sessions = sessions_between(start, end)
    names = [raw.upper() for raw in underlyings]

    def one(u: str) -> DownloadResult:
        if not sessions:
            return DownloadResult(
                provider=provider.name,
                underlying=u,
                requested_sessions=0,
                fetched_sessions=0,
                skipped_cached=0,
                rows=0,
            )
        res = _download_one(
            provider,
            store,
            u,
            sessions,
            max_dte=max_dte,
            refresh=refresh,
            max_sessions_per_request=max_sessions_per_request,
            need_oi=need_oi,
            ledger=ledger,
        )
        if ledger is not None:
            ledger.event("ticker_done", **res.model_dump(mode="json"))
        return res

    if concurrency == 1:
        return [one(u) for u in names]
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="history") as pool:
        return list(pool.map(one, names))


# ---------------------------------------------------------------------------
# --plan estimate
# ---------------------------------------------------------------------------


class TickerPlan(BaseModel):
    ticker: str
    sessions: int
    to_fetch: int
    oi_only: int
    rows_per_day: float
    rows_per_day_source: str  # "cached" | "<other provider>" (its cache) | "default"
    chunk_days: int
    requests: int
    est_rows: int
    est_disk_bytes: int
    est_wire_bytes: int


class DownloadPlan(BaseModel):
    provider: str
    tier: str
    start: dt.date
    end: dt.date
    sessions: int
    with_oi: bool
    concurrency: int
    seconds_per_request: float
    tickers: list[TickerPlan]

    @property
    def requests(self) -> int:
        return sum(t.requests for t in self.tickers)

    @property
    def est_rows(self) -> int:
        return sum(t.est_rows for t in self.tickers)

    @property
    def est_disk_bytes(self) -> int:
        return sum(t.est_disk_bytes for t in self.tickers)

    @property
    def est_wire_bytes(self) -> int:
        return sum(t.est_wire_bytes for t in self.tickers)

    @property
    def eta_seconds(self) -> float:
        return self.requests * self.seconds_per_request / max(1, self.concurrency)


def _count_span_runs(sessions: Sequence[dt.date], span: int) -> int:
    return sum(1 for _ in span_runs(sessions, set(), lambda: span))


def plan_download(
    store: ParquetHistoryStore,
    provider_name: str,
    underlyings: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    tier: str,
    with_oi: bool,
    concurrency: int,
    chunk_days: int = 7,
    max_chunk_days: int = 28,
    grow_below_rows_per_day: int = 500,
    default_rows_per_day: float = PLAN_DEFAULT_ROWS_PER_DAY,
    seconds_per_request: float = PLAN_DEFAULT_SECONDS_PER_REQUEST,
    refresh: bool = False,
) -> DownloadPlan:
    """Estimate requests / rows / bytes / ETA for a download, with no network.

    Rows per session come from the ticker's cached sessions (parquet footers)
    when any exist, else *default_rows_per_day*.  The chunk is the provider's
    starting chunk, or the grown maximum when the chain is small enough that the
    adaptive loop would grow it (mirrors ``ThetaDataEodProvider``).  *start*
    should already be clamped to the tier's earliest date.
    """
    sessions = sessions_between(start, end)
    plans: list[TickerPlan] = []
    for raw in underlyings:
        u = raw.upper()
        cached = set() if refresh else store.cached_dates(provider_name, u)
        oi_done = store.oi_dates(provider_name, u) if with_oi and not refresh else set()
        todo = [d for d in sessions if d not in cached]
        oi_only = [d for d in sessions if d in cached and d not in oi_done] if with_oi else []
        stats = store.file_stats(provider_name, u)
        src = "cached"
        if stats is None:  # any other provider's cache for this name beats a flat guess
            for other in store.providers():
                if other != provider_name and (stats := store.file_stats(other, u)):
                    src = other
                    break
        if stats is not None:
            rpd, bpr = stats
        else:
            rpd, bpr, src = default_rows_per_day, PLAN_DEFAULT_PARQUET_BYTES_PER_ROW, "default"
        # Adaptive growth works on rows per *calendar* day (5 sessions / 7 days).
        chunk = chunk_days
        if rpd * 5 / 7 < grow_below_rows_per_day:
            chunk = max(chunk_days, max_chunk_days)
        quote_reqs = _count_span_runs(todo, chunk)
        oi_reqs = (quote_reqs + _count_span_runs(oi_only, chunk)) if with_oi else 0
        rows = int(round(rpd * len(todo)))
        wire_per_row = PLAN_EOD_CSV_BYTES_PER_ROW + (PLAN_OI_CSV_BYTES_PER_ROW if with_oi else 0)
        plans.append(
            TickerPlan(
                ticker=u,
                sessions=len(sessions),
                to_fetch=len(todo),
                oi_only=len(oi_only),
                rows_per_day=round(rpd, 1),
                rows_per_day_source=src,
                chunk_days=chunk,
                requests=quote_reqs + oi_reqs,
                est_rows=rows,
                est_disk_bytes=int(round(rows * bpr)),
                est_wire_bytes=int(
                    round(rows * wire_per_row + len(oi_only) * rpd * PLAN_OI_CSV_BYTES_PER_ROW)
                ),
            )
        )
    return DownloadPlan(
        provider=provider_name,
        tier=tier,
        start=start,
        end=end,
        sessions=len(sessions),
        with_oi=with_oi,
        concurrency=concurrency,
        seconds_per_request=seconds_per_request,
        tickers=plans,
    )


def _gb(n: float) -> str:
    return f"{n / 1e9:.3f} GB"


def format_plan(plan: DownloadPlan) -> str:
    """Render a :class:`DownloadPlan` as a fixed-width text report."""
    lines = [
        f"plan: provider={plan.provider} tier={plan.tier} "
        f"{plan.start.isoformat()} → {plan.end.isoformat()} ({plan.sessions} sessions) "
        f"oi={'on' if plan.with_oi else 'off'} concurrency={plan.concurrency} "
        f"sec/request={plan.seconds_per_request:g}",
        f"{'ticker':<7} {'fetch':>6} {'oi_only':>7} {'rows/day':>9} {'src':<7} "
        f"{'chunk':>5} {'requests':>8} {'est_rows':>12} {'disk':>10} {'wire':>10}",
    ]
    lines.append("-" * len(lines[1]))
    for t in plan.tickers:
        lines.append(
            f"{t.ticker:<7} {t.to_fetch:>6} {t.oi_only:>7} {t.rows_per_day:>9.1f} "
            f"{t.rows_per_day_source:<7} {t.chunk_days:>5} {t.requests:>8} {t.est_rows:>12,} "
            f"{t.est_disk_bytes / 1e9:>7.3f} GB {t.est_wire_bytes / 1e9:>7.3f} GB"
        )
    lines.append(
        f"total: {len(plan.tickers)} tickers · {plan.requests:,} requests · "
        f"{plan.est_rows:,} rows · disk {_gb(plan.est_disk_bytes)} · "
        f"wire {_gb(plan.est_wire_bytes)} · ETA {_fmt_duration(plan.eta_seconds)}"
    )
    return "\n".join(lines)


def format_results(results: Sequence[DownloadResult]) -> str:
    """Per-ticker summary table plus the failed ranges (re-run the same command)."""
    header = (
        f"{'ticker':<7} {'sessions':>8} {'fetched':>7} {'cached':>6} {'oi_fill':>7} "
        f"{'rows':>10} {'seconds':>8}  status"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.underlying:<7} {r.requested_sessions:>8} {r.fetched_sessions:>7} "
            f"{r.skipped_cached:>6} {r.oi_filled_sessions:>7} {r.rows:>10} {r.seconds:>8.1f}  "
            f"{'FAILED' if r.error else 'ok'}"
        )
    failed = [(r.underlying, a, b, r.error) for r in results for a, b in r.failed_ranges]
    if failed:
        lines.append("")
        lines.append(f"failed ranges ({len(failed)}), re-run the same command to resume:")
        lines.extend(f"  {u} {a.isoformat()} → {b.isoformat()}: {e}" for u, a, b, e in failed)
    return "\n".join(lines)
