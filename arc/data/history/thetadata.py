"""ThetaData historical options provider (EOD report + open interest via Theta Terminal v3).

Requires the Theta Terminal v3 running locally (default
``http://127.0.0.1:25503``); credentials live in the terminal's own config,
never in this repo.  Uses ``GET /v3/option/history/eod`` with
``expiration=*`` and ``max_dte`` to pull every contract's daily EOD report
(OHLC + volume/count + closing NBBO + last trade time) for an underlying, and
on Value+ tiers ``GET /v3/option/history/open_interest`` joined per
contract-day (D84, E7.6).

Subscription tiers (ThetaData "Subscriptions" docs, options table, read
2026-10-10) set the first available date and the concurrent-request limit:

====== ========== ==========
tier   first date concurrent
====== ========== ==========
free   2023-06-01 1 (20-30 req/min; also capped at 365 days back)
value  2020-01-01 2
standard 2016-01-01 4
pro    2012-06-01 8
====== ========== ==========

Requests are issued per ``chunk_days`` calendar window.  The window adapts
per underlying: a timeout, an HTTP 570 (LARGE_REQUEST) or a response over
``max_response_bytes`` halves it; a small chain (fewer than
``grow_below_rows_per_day`` rows per calendar day) doubles it up to
``max_chunk_days``.  HTTP 429 / 474 / 5xx, timeouts and connection errors are
retried with exponential backoff; 472 (NO_DATA) is an empty result, not an
error.
"""

from __future__ import annotations

import csv
import datetime as dt
import enum
import io
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import BaseModel

from arc.data.history.base import OptionEodRow, OptionRight, occ_symbol
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

THETA_DEFAULT_URL = "http://127.0.0.1:25503"
THETA_FREE_LOOKBACK_DAYS = 365
_EOD_PATH = "/v3/option/history/eod"
_OI_PATH = "/v3/option/history/open_interest"

#: Retry these HTTP statuses with backoff (429 OS_LIMIT/queue full, 474 DISCONNECTED,
#: 571 SERVER_STARTING and any other 5xx except 570, which halves the chunk instead).
_HTTP_NO_DATA = 472
_HTTP_LARGE_REQUEST = 570
_RETRY_STATUSES = frozenset({429, 474})


class ThetaTier(enum.StrEnum):
    FREE = "free"
    VALUE = "value"
    STANDARD = "standard"
    PRO = "pro"


#: First options history date per tier (ThetaData Subscriptions docs).
TIER_FIRST_DATE: dict[ThetaTier, dt.date] = {
    ThetaTier.FREE: dt.date(2023, 6, 1),
    ThetaTier.VALUE: dt.date(2020, 1, 1),
    ThetaTier.STANDARD: dt.date(2016, 1, 1),
    ThetaTier.PRO: dt.date(2012, 6, 1),
}

#: Concurrent-request limit per tier (free is rate-limited, treated as 1).
TIER_CONCURRENCY: dict[ThetaTier, int] = {
    ThetaTier.FREE: 1,
    ThetaTier.VALUE: 2,
    ThetaTier.STANDARD: 4,
    ThetaTier.PRO: 8,
}

#: Tiers whose subscription includes the open-interest endpoint.
TIERS_WITH_OI = frozenset({ThetaTier.VALUE, ThetaTier.STANDARD, ThetaTier.PRO})


def tier_earliest_date(
    tier: ThetaTier, today: dt.date, lookback_days: int = THETA_FREE_LOOKBACK_DAYS
) -> dt.date:
    """First date *tier* can serve. Free is also capped at *lookback_days* back."""
    first = TIER_FIRST_DATE[tier]
    if tier is ThetaTier.FREE:
        return max(first, today - dt.timedelta(days=lookback_days))
    return first


def default_concurrency(tier: ThetaTier) -> int:
    return TIER_CONCURRENCY[tier]


def clamp_concurrency(tier: ThetaTier, requested: int | None) -> int:
    """*requested* workers (default: the tier's limit), clamped to ``[1, tier limit]``."""
    cap = TIER_CONCURRENCY[tier]
    if requested is None:
        return cap
    return max(1, min(requested, cap))


class ThetaTerminalError(RuntimeError):
    """The Theta Terminal is unreachable or returned an error."""


class _Timeout(ThetaTerminalError):
    """Request timed out (after retries at the minimum chunk size)."""


class _HttpResponse(Protocol):
    status_code: int
    text: str


class _HttpSession(Protocol):
    def get(self, url: str, params: dict[str, Any], timeout: float) -> _HttpResponse: ...


class RequestLog(BaseModel):
    """One HTTP request to the Terminal (a ledger line, E7.6)."""

    ticker: str
    kind: str  # "eod" | "oi"
    start: dt.date
    end: dt.date
    chunk_days: int
    status: int | None  # HTTP status of the final attempt (None = no response)
    rows: int = 0
    bytes: int = 0
    seconds: float = 0.0
    retries: int = 0
    error: str | None = None


_RIGHTS = {
    "C": OptionRight.CALL,
    "CALL": OptionRight.CALL,
    "P": OptionRight.PUT,
    "PUT": OptionRight.PUT,
}


def _f(v: str | None) -> float | None:
    if v is None or v.strip() == "":
        return None
    return float(v)


def _date(v: str) -> dt.date:
    """Parse ``YYYY-MM-DD``, ``YYYYMMDD`` or an ISO datetime to a date."""
    v = v.strip()
    if len(v) == 8 and v.isdigit():
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:]))
    return dt.date.fromisoformat(v[:10])


def _ts(v: str | None) -> dt.datetime | None:
    """Parse an ISO timestamp (naive = ET); blank/unparseable -> None."""
    if v is None or not v.strip():
        return None
    try:
        return dt.datetime.fromisoformat(v.strip())
    except ValueError:
        return None


def parse_eod_csv(text: str, underlying: str, provider: str = "thetadata") -> list[OptionEodRow]:
    """Parse a Theta v3 ``option/history/eod`` CSV body into rows.

    The session date is taken from ``created`` (report generation time, 17:15 ET).
    Rows with an unknown right or non-positive strike are skipped.
    """
    rows: list[OptionEodRow] = []
    reader = csv.DictReader(io.StringIO(text))
    for rec in reader:
        right = _RIGHTS.get((rec.get("right") or "").strip().upper())
        strike = _f(rec.get("strike"))
        if right is None or strike is None or strike <= 0:
            log.warning("thetadata.skip_row", reason="bad right/strike", rec=rec)
            continue
        expiration = _date(rec["expiration"])
        root = (rec.get("symbol") or underlying).strip().upper()
        rows.append(
            OptionEodRow(
                provider=provider,
                underlying=underlying,
                date=_date(rec["created"]),
                symbol=occ_symbol(root, expiration, right, strike),
                expiration=expiration,
                strike=strike,
                right=right,
                open=_f(rec.get("open")),
                high=_f(rec.get("high")),
                low=_f(rec.get("low")),
                close=_f(rec.get("close")),
                volume=_f(rec.get("volume")),
                trade_count=_f(rec.get("count")),
                bid=_f(rec.get("bid")),
                ask=_f(rec.get("ask")),
                bid_size=_f(rec.get("bid_size")),
                ask_size=_f(rec.get("ask_size")),
                last_trade=_ts(rec.get("last_trade")),
                created=_ts(rec.get("created")),
            )
        )
    return rows


OiKey = tuple[dt.date, str]  # (session date, OCC symbol)


def parse_oi_csv(text: str, underlying: str) -> dict[OiKey, float]:
    """Parse a Theta v3 ``option/history/open_interest`` CSV body.

    Keyed by (date of the report ``timestamp``, OCC symbol).  OPRA reports OI
    around 06:30 ET for the *previous* session's close, so the value keyed to
    session D is what a trader can see during D (no look-ahead).
    """
    out: dict[OiKey, float] = {}
    for rec in csv.DictReader(io.StringIO(text)):
        right = _RIGHTS.get((rec.get("right") or "").strip().upper())
        strike = _f(rec.get("strike"))
        oi = _f(rec.get("open_interest"))
        stamp = rec.get("timestamp") or rec.get("date") or ""
        if right is None or strike is None or strike <= 0 or oi is None or not stamp.strip():
            continue
        root = (rec.get("symbol") or underlying).strip().upper()
        sym = occ_symbol(root, _date(rec["expiration"]), right, strike)
        out[(_date(stamp), sym)] = oi
    return out


def join_open_interest(rows: list[OptionEodRow], oi: dict[OiKey, float]) -> list[OptionEodRow]:
    """Attach ``open_interest`` per (date, symbol); unmatched rows keep None."""
    return [
        r.model_copy(update={"open_interest": oi[(r.date, r.symbol)]})
        if (r.date, r.symbol) in oi
        else r
        for r in rows
    ]


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


class _ThreadLocalSession:
    """One ``requests.Session`` per worker thread (sessions are not thread-safe)."""

    def __init__(self) -> None:
        self._local = threading.local()

    def get(self, url: str, params: dict[str, Any], timeout: float) -> _HttpResponse:
        s = getattr(self._local, "s", None)
        if s is None:  # pragma: no cover - real network session
            import requests

            s = self._local.s = requests.Session()
        return s.get(url, params=params, timeout=timeout)  # type: ignore[no-any-return]


class ThetaDataEodProvider:
    """``HistoricalDataProvider`` backed by ThetaData's EOD (+ open interest) endpoints."""

    name = "thetadata"

    def __init__(
        self,
        base_url: str = THETA_DEFAULT_URL,
        session: _HttpSession | None = None,
        *,
        tier: ThetaTier | str = ThetaTier.FREE,
        with_oi: bool | None = None,
        lookback_days: int = THETA_FREE_LOOKBACK_DAYS,
        chunk_days: int = 7,
        max_chunk_days: int = 28,
        grow_below_rows_per_day: int = 500,
        max_response_bytes: int = 50_000_000,
        min_interval_s: float = 0.0,
        timeout_s: float = 120.0,
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        today: Callable[[], dt.date] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_request: Callable[[RequestLog], None] | None = None,
    ) -> None:
        if chunk_days < 1:
            msg = "chunk_days must be >= 1"
            raise ValueError(msg)
        self.tier = ThetaTier(tier)
        self.with_oi = self.tier in TIERS_WITH_OI if with_oi is None else with_oi
        if self.with_oi and self.tier not in TIERS_WITH_OI:
            msg = f"open interest needs a Value+ subscription (tier={self.tier})"
            raise ValueError(msg)
        base = base_url.rstrip("/")
        self._eod_url = base + _EOD_PATH
        self._oi_url = base + _OI_PATH
        self._session: _HttpSession = session or _ThreadLocalSession()
        self._lookback = lookback_days
        self.chunk_days = chunk_days
        self._max_chunk = max(chunk_days, max_chunk_days)
        self._grow_below = grow_below_rows_per_day
        self._max_bytes = max_response_bytes
        self._min_interval = min_interval_s
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._today = today or (lambda: now_et().date())
        self._sleep = sleep
        self._on_request = on_request
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_call: float | None = None
        self._chunks: dict[str, int] = {}

    # -- tier ----------------------------------------------------------------

    def earliest_date(self) -> dt.date:
        return tier_earliest_date(self.tier, self._today(), self._lookback)

    def chunk_for(self, underlying: str) -> int:
        """Current adaptive chunk (calendar days) for *underlying*."""
        with self._lock:
            return self._chunks.get(underlying.upper(), self.chunk_days)

    def _set_chunk(self, underlying: str, days: int) -> None:
        with self._lock:
            self._chunks[underlying.upper()] = days

    # -- HTTP ----------------------------------------------------------------

    def _throttle(self) -> None:
        if not self._min_interval:
            return
        with self._throttle_lock:  # global spacing between request starts, across workers
            if self._last_call is not None:
                wait = self._min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            self._last_call = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(self._backoff_max, self._backoff_base * (2**attempt)))

    def _get(self, url: str, params: dict[str, Any]) -> tuple[int, str, int]:
        """One logical request with retries -> (status, body, retries).

        Raises :class:`_Timeout` after the last timed-out attempt (the caller
        may halve the chunk), :class:`ThetaTerminalError` for other failures.
        Returns status 570 without raising so the caller can halve the chunk.
        """
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001 — classify then retry or raise
                if _is_timeout(exc):  # the chunk loop halves (or backs off) and retries
                    msg = f"Theta Terminal timeout at {url}: {exc}"
                    raise _Timeout(msg) from exc
                if attempt >= self._max_retries:
                    msg = (
                        f"Theta Terminal unreachable at {url}: {exc} (is the v3 terminal running?)"
                    )
                    raise ThetaTerminalError(msg) from exc
                log.warning("thetadata.retry", url=url, error=str(exc), attempt=attempt)
                self._backoff(attempt)
                attempt += 1
                continue
            status = resp.status_code
            if status == 200 or status in (_HTTP_NO_DATA, _HTTP_LARGE_REQUEST):
                return status, resp.text if status == 200 else "", attempt
            retryable = status in _RETRY_STATUSES or 500 <= status < 600
            if retryable and attempt < self._max_retries:
                log.warning("thetadata.retry", url=url, status=status, attempt=attempt)
                self._backoff(attempt)
                attempt += 1
                continue
            msg = f"Theta Terminal HTTP {status}: {resp.text[:300]}"
            raise ThetaTerminalError(msg)

    def _record(self, entry: RequestLog) -> None:
        log.info("thetadata.request", **entry.model_dump(mode="json"))
        if self._on_request is not None:
            self._on_request(entry)

    # -- chunk loop ------------------------------------------------------------

    def _chunked(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        kind: str,
        max_dte: int,
        consume: Callable[[str, dt.date, dt.date], int],
    ) -> None:
        """Walk [start, end] in adaptive calendar-day chunks; *consume* parses a body -> rows."""
        u = underlying.upper()
        url = self._eod_url if kind == "eod" else self._oi_url
        cur = start
        timeouts = 0
        while cur <= end:
            chunk = self.chunk_for(u)
            chunk_end = min(end, cur + dt.timedelta(days=chunk - 1))
            params = {
                "symbol": u,
                "expiration": "*",
                "start_date": cur.strftime("%Y%m%d"),
                "end_date": chunk_end.strftime("%Y%m%d"),
                "max_dte": max_dte,
                "format": "csv",
            }
            t0 = time.monotonic()
            try:
                status, text, retries = self._get(url, params)
            except _Timeout as exc:
                self._record(
                    RequestLog(
                        ticker=u, kind=kind, start=cur, end=chunk_end, chunk_days=chunk,
                        status=None, seconds=time.monotonic() - t0, error=f"timeout: {exc}",
                    )
                )  # fmt: skip
                if chunk > 1:
                    self._set_chunk(u, max(1, chunk // 2))
                    log.warning("thetadata.chunk_halved", ticker=u, reason="timeout", chunk=chunk)
                    continue
                if timeouts >= self._max_retries:
                    raise
                self._backoff(timeouts)
                timeouts += 1
                continue
            seconds = time.monotonic() - t0
            if status == _HTTP_LARGE_REQUEST:
                self._record(
                    RequestLog(
                        ticker=u, kind=kind, start=cur, end=chunk_end, chunk_days=chunk,
                        status=status, seconds=seconds, retries=retries, error="large request",
                    )
                )  # fmt: skip
                if chunk == 1:
                    msg = f"Theta Terminal HTTP 570 (LARGE_REQUEST) for a 1-day {kind} chunk"
                    raise ThetaTerminalError(msg)
                self._set_chunk(u, max(1, chunk // 2))
                log.warning("thetadata.chunk_halved", ticker=u, reason="large_request", chunk=chunk)
                continue
            rows = consume(text, cur, chunk_end) if text.strip() else 0
            nbytes = len(text.encode())
            self._record(
                RequestLog(
                    ticker=u, kind=kind, start=cur, end=chunk_end, chunk_days=chunk,
                    status=status, rows=rows, bytes=nbytes, seconds=seconds, retries=retries,
                )
            )  # fmt: skip
            if kind == "eod":  # only the quote report drives chunk sizing
                if nbytes > self._max_bytes and chunk > 1:
                    self._set_chunk(u, max(1, chunk // 2))
                elif rows / chunk < self._grow_below and chunk < self._max_chunk:
                    self._set_chunk(u, min(self._max_chunk, chunk * 2))
            cur = chunk_end + dt.timedelta(days=1)

    # -- public ----------------------------------------------------------------

    def fetch_open_interest(
        self, underlying: str, start: dt.date, end: dt.date, *, max_dte: int
    ) -> dict[OiKey, float]:
        """Open interest per (session, OCC symbol) for every contract ≤ *max_dte* (Value+)."""
        if self.tier not in TIERS_WITH_OI:
            msg = f"open interest needs a Value+ subscription (tier={self.tier})"
            raise ThetaTerminalError(msg)
        start = max(start, self.earliest_date())
        out: dict[OiKey, float] = {}

        def consume(text: str, lo: dt.date, hi: dt.date) -> int:
            got = {k: v for k, v in parse_oi_csv(text, underlying).items() if lo <= k[0] <= hi}
            out.update(got)
            return len(got)

        self._chunked(underlying, start, end, kind="oi", max_dte=max_dte, consume=consume)
        return out

    def fetch_option_eod(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
    ) -> list[OptionEodRow]:
        start = max(start, self.earliest_date())
        rows: list[OptionEodRow] = []

        def consume(text: str, lo: dt.date, hi: dt.date) -> int:
            got = [
                r
                for r in parse_eod_csv(text, underlying, self.name)
                if start <= r.date <= end and lo <= r.date <= hi
            ]
            rows.extend(got)
            return len(got)

        self._chunked(underlying, start, end, kind="eod", max_dte=max_dte, consume=consume)
        if self.with_oi and start <= end:
            rows = join_open_interest(
                rows, self.fetch_open_interest(underlying, start, end, max_dte=max_dte)
            )
        log.info(
            "thetadata.fetched",
            underlying=underlying,
            start=start.isoformat(),
            end=end.isoformat(),
            rows=len(rows),
            with_oi=self.with_oi,
        )
        return rows
