"""Earnings calendar connector.

Fetches upcoming and recent earnings dates from Finnhub (free tier
default, configurable).  Yields one ``RawDoc`` per earnings event
with the ticker, date, and any available EPS estimate data.

E4.1d: the connector fails loudly instead of returning ``[]``:

- no ``ARC_FINNHUB_API_KEY`` -> :class:`EarningsNoKeyError` (the routine records the
  run ``skipped`` with reason ``no_api_key`` and posts one notice per day);
- an HTTP / network / JSON error -> the original exception propagates (run ``failed``);
- Finnhub's free tier silently caps ``/calendar/earnings`` at ``row_cap`` rows per
  call (it keeps the *latest* dates), so the window is fetched in ``chunk_days``
  chunks; a capped chunk is re-fetched one day at a time and a capped single day is
  :class:`EarningsFetchError` ``truncated``. HTTP 429 is retried once after
  ``Retry-After`` (default 60 s), then ``rate_limited``. A client-side throttle keeps
  the call rate under ``rate_limit_per_min``.

E4.8 (D46): the HTTP call goes through the shared :mod:`arc.ingest.finnhub` client,
so the calendar and the per-ticker Finnhub jobs share one cross-process budget
(``finnhub_calls_per_minute``, default 55 of the key's 60/min).

Nothing is stored unless the whole window was fetched, so a failed run never leaves
a silent partial calendar. Incremental: the cursor is the day of the last full fetch.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import time
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.ingest.finnhub import (
    DbRateLimiter,
    FinnhubClient,
    FinnhubForbidden,
    FinnhubRateLimited,
    redact,
)
from arc.ingest.finnhub import _get_json as _get_json  # patched by tests (E4.1d)
from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc
from arc.universe.ingest import IngestUniverse
from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from arc.config import ArcSettings
    from arc.ingest.finnhub import RateLimiter

log = structlog.get_logger()

CONNECTOR = "earnings"
CALENDAR_PATH = "/calendar/earnings"

__all__ = [
    "CONNECTOR",
    "EarningsFetchConfig",
    "EarningsFetchError",
    "EarningsNoKeyError",
    "FetchStats",
    "fetch_calendar",
    "fetch_earnings",
]


class EarningsNoKeyError(RuntimeError):
    """``ARC_FINNHUB_API_KEY`` is not set: the run is ``skipped`` (``no_api_key``)."""

    reason = "no_api_key"


class EarningsFetchError(RuntimeError):
    """The calendar could not be fetched completely.

    ``reason``: truncated | rate_limited | forbidden (403: the endpoint left the plan).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


class EarningsFetchConfig(BaseModel):
    """Per-job knobs (``sources.earnings`` in ``config/routines.yaml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Finnhub free tier: /calendar/earnings returns at most this many rows per call.
    row_cap: Annotated[int, Field(ge=1)] = 1500
    chunk_days: Annotated[int, Field(ge=1, le=31)] = 7
    rate_limit_per_min: Annotated[int, Field(ge=1, le=600)] = 60
    lookback_days: Annotated[int, Field(ge=0, le=60)] = 7
    horizon_days: Annotated[int, Field(ge=1, le=120)] = 30
    retry_after_default_s: Annotated[float, Field(ge=0)] = 60.0
    timeout_s: Annotated[float, Field(gt=0)] = 15.0

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> EarningsFetchConfig:
        """Pick this model's keys out of a job's options (other extras are ignored)."""
        return cls.model_validate({k: v for k, v in options.items() if k in cls.model_fields})


class FetchStats(BaseModel):
    """What one window fetch did (logged as ``earnings.done``)."""

    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    chunks: int = 0
    chunks_split: int = 0
    events: int = 0
    max_rows: int = 0  # largest single response (must stay < row_cap)
    min_date: str | None = None
    max_date: str | None = None


# ---------------------------------------------------------------------------
# HTTP (E4.8: the shared Finnhub client and its cross-process budget)
# ---------------------------------------------------------------------------


class _Client:
    """``/calendar/earnings`` on the shared :class:`FinnhubClient` (E4.8 / D46).

    The shared client paces at ``rate_limit_per_min``, holds the key-wide budget
    (``limiter``) and retries a 429 once. Errors keep their E4.1d shape: a second
    429 is :class:`EarningsFetchError` ``rate_limited``, a 403 is ``forbidden``,
    any other HTTP / network / JSON error propagates as its original type.
    """

    def __init__(
        self,
        api_key: str,
        cfg: EarningsFetchConfig,
        *,
        get_json: Callable[[str, float], Any],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        limiter: RateLimiter | None = None,
    ) -> None:
        self._http = FinnhubClient(
            api_key,
            limiter=limiter,
            min_interval_s=60.0 / cfg.rate_limit_per_min,
            timeout_s=cfg.timeout_s,
            retry_after_default_s=cfg.retry_after_default_s,
            get_json=get_json,
            sleep=sleep,
            clock=clock,
            raw_errors=True,
        )

    @property
    def calls(self) -> int:
        return self._http.calls

    def calendar(self, start: _dt.date, end: _dt.date) -> list[dict[str, Any]]:
        params = {"from": start.isoformat(), "to": end.isoformat()}
        try:
            data = self._http.get(CALENDAR_PATH, params)
        except FinnhubRateLimited as exc:
            self._failed(start, end, exc)
            detail = f"HTTP 429 twice for {start}..{end}"
            raise EarningsFetchError("rate_limited", detail) from None
        except FinnhubForbidden as exc:
            self._failed(start, end, exc)
            raise EarningsFetchError("forbidden", str(exc)) from None
        except Exception as exc:
            self._failed(start, end, exc)
            raise
        return self._rows(start, end, data)

    def _rows(self, start: _dt.date, end: _dt.date, data: Any) -> list[dict[str, Any]]:
        rows = data.get("earningsCalendar") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            exc = ValueError(f"unexpected Finnhub payload: {redact(str(data))[:120]}")
            self._failed(start, end, exc)
            raise exc
        return [r for r in rows if isinstance(r, dict)]

    @staticmethod
    def _failed(start: _dt.date, end: _dt.date, exc: BaseException) -> None:
        log.warning(
            "earnings.finnhub_failed",
            from_date=start.isoformat(),
            to_date=end.isoformat(),
            error=redact(str(exc)),
            error_class=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Window fetch
# ---------------------------------------------------------------------------


def _chunks(start: _dt.date, end: _dt.date, days: int) -> list[tuple[_dt.date, _dt.date]]:
    out: list[tuple[_dt.date, _dt.date]] = []
    cur = start
    while cur <= end:
        last = min(cur + _dt.timedelta(days=days - 1), end)
        out.append((cur, last))
        cur = last + _dt.timedelta(days=1)
    return out


def fetch_calendar(
    api_key: str,
    start: _dt.date,
    end: _dt.date,
    cfg: EarningsFetchConfig,
    *,
    get_json: Callable[[str, float], Any] = _get_json,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    limiter: RateLimiter | None = None,
) -> tuple[list[dict[str, Any]], FetchStats]:
    """Every event in ``[start, end]``, deduped on (symbol, date), or raise.

    A chunk returning ``>= row_cap`` rows may be truncated, so it is re-fetched
    one day at a time; a single day still at the cap raises ``truncated``.
    """
    client = _Client(api_key, cfg, get_json=get_json, sleep=sleep, clock=clock, limiter=limiter)
    stats = FetchStats()
    events: dict[tuple[str, str], dict[str, Any]] = {}

    def capped(rows: list[dict[str, Any]]) -> bool:
        stats.max_rows = max(stats.max_rows, len(rows))
        return len(rows) >= cfg.row_cap

    def keep(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            key = (normalize_symbol(str(r.get("symbol") or "")), str(r.get("date") or ""))
            if key[0] and key[1]:
                events.setdefault(key, r)

    for c_start, c_end in _chunks(start, end, cfg.chunk_days):
        stats.chunks += 1
        rows = client.calendar(c_start, c_end)
        if not capped(rows):
            keep(rows)
            continue
        if c_start == c_end:
            msg = f"{c_start} returned {len(rows)} rows (cap {cfg.row_cap})"
            raise EarningsFetchError("truncated", msg)
        stats.chunks_split += 1
        log.info("earnings.chunk_split", from_date=str(c_start), to_date=str(c_end), rows=len(rows))
        for day, _ in _chunks(c_start, c_end, 1):
            day_rows = client.calendar(day, day)
            if capped(day_rows):
                msg = f"{day} returned {len(day_rows)} rows (cap {cfg.row_cap})"
                raise EarningsFetchError("truncated", msg)
            keep(day_rows)

    stats.calls = client.calls
    stats.events = len(events)
    dates = sorted(d for _, d in events)
    if dates:
        stats.min_date, stats.max_date = dates[0], dates[-1]
    return list(events.values()), stats


def fetch_window(
    cursor_val: str | None, today: _dt.date, cfg: EarningsFetchConfig
) -> tuple[_dt.date, _dt.date]:
    """``[max(cursor, today - lookback), today + horizon]`` (a bad cursor = no cursor)."""
    start = today - _dt.timedelta(days=cfg.lookback_days)
    if cursor_val:
        with contextlib.suppress(ValueError):  # a bad cursor = no cursor
            start = max(start, _dt.date.fromisoformat(cursor_val[:10]))
    return min(start, today), today + _dt.timedelta(days=cfg.horizon_days)


def fetch_earnings(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    cfg: EarningsFetchConfig | None = None,
    today: _dt.date | None = None,
    get_json: Callable[[str, float], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[RawDoc]:
    """Fetch earnings calendar events (D28: any symbol-master ticker, seed list otherwise).

    Returns only newly stored documents. Raises :class:`EarningsNoKeyError` without
    a key, :class:`EarningsFetchError` on a truncated / rate-limited window, and the
    original exception on any other fetch error (nothing is stored then).
    """
    cfg = cfg or EarningsFetchConfig()
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)

    api_key = settings.finnhub_api_key
    if not api_key:
        log.warning("earnings.no_api_key", hint="Set ARC_FINNHUB_API_KEY in ~/.hermes/.env")
        msg = "no_api_key: ARC_FINNHUB_API_KEY is not set (earnings calendar not fetched)"
        raise EarningsNoKeyError(msg)

    today = today or _dt.datetime.now(_dt.UTC).date()
    from_date, to_date = fetch_window(cursor_repo.get(CONNECTOR), today, cfg)
    events, stats = fetch_calendar(
        api_key,
        from_date,
        to_date,
        cfg,
        get_json=get_json or _get_json,
        sleep=sleep or time.sleep,
        clock=clock or time.monotonic,
        # D46: one key-wide budget shared with the per-ticker Finnhub jobs (cross-process).
        limiter=DbRateLimiter(
            conn, calls_per_minute=settings.finnhub_calls_per_minute, sleep=sleep or time.sleep
        ),
    )

    # D28: keep events for every symbol-master ticker (next_earnings and the gate's
    # earnings blackout need them for any name the open universe may trade). Without
    # a master (strict mode / no cache) this is the seed list, as before.
    uni = IngestUniverse.from_settings(settings)
    seed_only_to_scout = uni.config.earnings.scout == "seed"
    results: list[RawDoc] = []
    calendar_only: list[str] = []

    for event in events:
        symbol = normalize_symbol(event.get("symbol", ""))
        if not symbol or not uni.known(symbol):
            continue

        report_date = event.get("date", "")
        try:
            pub_dt = _dt.datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=_dt.UTC)
        except (ValueError, TypeError):
            continue  # no usable date: next_earnings could not read it either

        # Build a descriptive text from the event
        eps_estimate = event.get("epsEstimate")
        eps_actual = event.get("epsActual")
        revenue_estimate = event.get("revenueEstimate")
        hour = event.get("hour", "")

        text_parts = [
            f"Earnings report for {symbol} on {report_date}.",
            f"Reporting: {hour}" if hour else "",
            f"EPS estimate: {eps_estimate}" if eps_estimate is not None else "",
            f"EPS actual: {eps_actual}" if eps_actual is not None else "",
            f"Revenue estimate: {revenue_estimate}" if revenue_estimate is not None else "",
        ]
        text = " ".join(p for p in text_parts if p)

        url = f"https://finnhub.io/calendar/earnings/{symbol}/{report_date}"
        h = content_hash(CONNECTOR, url)

        doc = RawDoc(
            source=CONNECTOR,
            url=url,
            published_at=pub_dt,
            text=text,
            tickers_hint=[symbol],
            content_hash=h,
        )

        doc_id = doc_repo.insert(
            source=doc.source,
            url=doc.url,
            published_at=doc.published_at.isoformat(),
            text=doc.text,
            tickers_hint=doc.tickers_hint,
            hash_val=h,
        )

        if doc_id is not None:
            results.append(doc)
            if seed_only_to_scout and not uni.is_seed(symbol):
                calendar_only.append(doc_id)

    # Non-seed events are calendar data only (config/universe.yaml earnings.scout: seed):
    # stored for next_earnings / the gate, but marked scouted so the ~1k-event calendar
    # does not crowd the Scout's batches.
    if calendar_only:
        doc_repo.mark_scouted(calendar_only, run_id="earnings:calendar-only")

    # The whole window was fetched: the next run starts from today (minus lookback).
    cursor_repo.set(CONNECTOR, today.isoformat())

    log.info(
        "earnings.done",
        calls=stats.calls,
        chunks=stats.chunks,
        chunks_split=stats.chunks_split,
        events=stats.events,
        max_rows=stats.max_rows,
        min_date=stats.min_date,
        max_date=stats.max_date,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        new_docs=len(results),
        calendar_only=len(calendar_only),
    )
    return results
