"""The one Finnhub client every caller uses (E4.8 / D46).

Finnhub's free key allows **60 calls/min for the whole key**, shared by the
earnings calendar (E4.1d) and the per-ticker context jobs (earnings surprises,
insider transactions, recommendation trends, basic financials). Background-lane
jobs run as separate processes (D39), so the budget is enforced across
processes: :class:`DbRateLimiter` keeps a sliding window of call timestamps in one
``routine_state`` row, read and written inside ``BEGIN IMMEDIATE`` (no migration).

The API key never leaves this module: it is only placed in the request URL, and
:func:`redact` strips ``token=`` from anything that is logged or raised.

Errors:

- :class:`FinnhubNoKey` - ``ARC_FINNHUB_API_KEY`` is not set (the run is ``skipped``);
- :class:`FinnhubForbidden` - HTTP 403, the endpoint is not on the free plan (loud);
- :class:`FinnhubRateLimited` - HTTP 429 twice (one retry after ``Retry-After``, else 60 s);
- :class:`FinnhubError` - any other HTTP / network / payload error from :meth:`FinnhubClient.get`.

Free endpoints only (D3). Measured 403 on the free key (never call these):
economic calendar, dividends, option chain, candles, price target,
upgrade/downgrade, social sentiment.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

log = structlog.get_logger()

BASE_URL = "https://finnhub.io/api/v1"
RATE_STATE_KEY = "finnhub:calls"  # routine_state row: JSON list of epoch seconds
DEFAULT_CALLS_PER_MINUTE = 55  # headroom under the key's 60/min
WINDOW_S = 60.0

# Paid endpoints (403 on the free key, measured 2026-10-03). Never requested.
PAID_PATHS: frozenset[str] = frozenset(
    {
        "/calendar/economic",
        "/stock/dividend",
        "/stock/option-chain",
        "/stock/candle",
        "/stock/price-target",
        "/stock/upgrade-downgrade",
        "/stock/social-sentiment",
    }
)

__all__ = [
    "BASE_URL",
    "DEFAULT_CALLS_PER_MINUTE",
    "PAID_PATHS",
    "DbRateLimiter",
    "FinnhubClient",
    "FinnhubError",
    "FinnhubForbidden",
    "FinnhubNoKey",
    "FinnhubRateLimited",
    "MemoryRateLimiter",
    "redact",
]


# ---------------------------------------------------------------------------
# Errors + redaction
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(token=)[^&\s'\"]*", re.IGNORECASE)


def redact(text: str, key: str | None = None) -> str:
    """*text* with every ``token=<value>`` (and the literal *key*) replaced by ``***``."""
    out = _TOKEN_RE.sub(r"\1***", text)
    if key:
        out = out.replace(key, "***")
    return out


class FinnhubError(RuntimeError):
    """A Finnhub call failed. ``reason`` is a short machine label; the text is redacted."""

    reason = "error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.reason}: {detail}")


class FinnhubNoKey(FinnhubError):  # noqa: N818 - card-specified name
    """``ARC_FINNHUB_API_KEY`` is not set: the run is ``skipped`` (``no_api_key``)."""

    reason = "no_api_key"


class FinnhubForbidden(FinnhubError):  # noqa: N818 - card-specified name
    """HTTP 403: the endpoint is not on the plan (a free endpoint went paid: fail loudly)."""

    reason = "forbidden"


class FinnhubRateLimited(FinnhubError):  # noqa: N818 - card-specified name
    """HTTP 429 after one retry."""

    reason = "rate_limited"


# ---------------------------------------------------------------------------
# Rate limiters
# ---------------------------------------------------------------------------


class RateLimiter(Protocol):
    def acquire(self) -> float:
        """Block until one call fits the budget; record it. Returns the seconds waited."""
        ...


def _admit(
    stamps: list[float], now: float, limit: int, window_s: float
) -> tuple[list[float], float]:
    """Pure sliding-window step: ``(kept stamps [+ now if admitted], wait seconds)``."""
    kept = sorted(t for t in stamps if t > now - window_s)
    if len(kept) < limit:
        return [*kept, now], 0.0
    # The oldest stamp that must age out before one more call fits.
    return kept, max(kept[len(kept) - limit] + window_s - now, 0.001)


@dataclass
class MemoryRateLimiter:
    """In-process sliding window (no DB: ``arc ingest`` without a store, tests)."""

    calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE
    window_s: float = WINDOW_S
    sleep: Callable[[float], None] = time.sleep
    wall: Callable[[], float] = time.time
    stamps: list[float] = field(default_factory=list)

    def acquire(self) -> float:
        waited = 0.0
        while True:
            self.stamps, wait = _admit(
                self.stamps, self.wall(), self.calls_per_minute, self.window_s
            )
            if wait == 0.0:
                return waited
            self.sleep(wait)
            waited += wait


@dataclass
class DbRateLimiter:
    """Cross-process sliding window in ``routine_state[finnhub:calls]``.

    Every process that calls Finnhub (the tick, each background child, a manual
    ``arc routines run``) shares the same DB row, so the key's budget holds across
    them. The read-modify-write runs inside ``BEGIN IMMEDIATE`` (one writer at a
    time; SQLite's busy timeout serialises the others). Wall-clock seconds, because
    a monotonic clock is not comparable across processes.
    """

    conn: sqlite3.Connection
    calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE
    window_s: float = WINDOW_S
    sleep: Callable[[float], None] = time.sleep
    wall: Callable[[], float] = time.time
    key: str = RATE_STATE_KEY

    def _step(self) -> float:
        own_txn = not self.conn.in_transaction
        if own_txn:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT value FROM routine_state WHERE key = ?", (self.key,)
            ).fetchone()
            stamps: list[float] = []
            if row is not None:
                try:
                    stamps = [float(x) for x in json.loads(row[0])]
                except (TypeError, ValueError):
                    stamps = []  # a corrupt row is reset, never a crash
            now = self.wall()
            kept, wait = _admit(stamps, now, self.calls_per_minute, self.window_s)
            if wait == 0.0:
                updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z"
                self.conn.execute(
                    """INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                      updated_at = excluded.updated_at""",
                    (self.key, json.dumps([round(t, 3) for t in kept]), updated),
                )
        except BaseException:
            if own_txn:
                self.conn.rollback()
            raise
        if own_txn:
            self.conn.commit()
        return wait

    def acquire(self) -> float:
        waited = 0.0
        while True:
            wait = self._step()
            if wait == 0.0:
                return waited
            log.info("finnhub.rate_wait", wait_s=round(wait, 2), limit=self.calls_per_minute)
            self.sleep(wait)
            waited += wait


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url)  # noqa: S310 - fixed https host
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def _retry_after(exc: urllib.error.HTTPError, default: float) -> float:
    raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    try:
        return max(0.0, float(raw)) if raw is not None else default
    except ValueError:
        return default


class FinnhubClient:
    """``get(path, params)`` with pacing, the shared budget and one 429 retry.

    ``min_interval_s`` paces this client's own calls (E4.1d's per-job
    ``rate_limit_per_min``); ``limiter`` is the key-wide budget. ``get_json``,
    ``sleep`` and ``clock`` are injectable (tests never touch the network).

    ``raw_errors=True`` (the earnings calendar, E4.1d) re-raises a non-403/429
    HTTP / network / JSON error as its original type (an ``HTTPError`` is rebuilt
    with the token stripped from its URL), so that connector's outcomes stay as
    they were. Otherwise every such error is a :class:`FinnhubError`.
    """

    def __init__(
        self,
        api_key: str,
        *,
        limiter: RateLimiter | None = None,
        min_interval_s: float = 0.0,
        timeout_s: float = 15.0,
        retry_after_default_s: float = 60.0,
        get_json: Callable[[str, float], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        raw_errors: bool = False,
    ) -> None:
        if not api_key:
            msg = "ARC_FINNHUB_API_KEY is not set"
            raise FinnhubNoKey(msg)
        self._key = api_key
        self._limiter = limiter or MemoryRateLimiter(sleep=self._do_sleep)
        self._interval = min_interval_s
        self._timeout = timeout_s
        self._retry_default = retry_after_default_s
        self._get = get_json
        self._sleep = sleep
        self._clock = clock
        self._raw = raw_errors
        self._last: float | None = None
        self.calls = 0
        self.endpoints: dict[str, int] = {}  # path -> calls (run manifest, D27)

    def _do_sleep(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    def __repr__(self) -> str:  # never show the key
        return f"FinnhubClient(calls={self.calls})"

    def _pace(self) -> None:
        if self._last is not None and self._interval > 0:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self._do_sleep(wait)
        self._limiter.acquire()
        self._last = self._clock()

    def _url(self, path: str, params: Mapping[str, Any]) -> str:
        query = urllib.parse.urlencode({**params, "token": self._key})
        return f"{BASE_URL}{path}?{query}"

    def _safe(self, text: object) -> str:
        return redact(str(text), self._key)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``BASE_URL + path`` and return the decoded JSON (dict or list)."""
        if path in PAID_PATHS:
            msg = f"{path} is a paid Finnhub endpoint (D3: free endpoints only)"
            raise FinnhubForbidden(msg)
        url = self._url(path, params or {})
        label = f"{path} {dict(params or {})}"
        retried = False
        while True:
            self._pace()
            self.calls += 1
            self.endpoints[path] = self.endpoints.get(path, 0) + 1
            try:
                fetch = self._get if self._get is not None else _get_json
                return fetch(url, self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and not retried:  # noqa: PLR2004
                    retried = True
                    delay = _retry_after(exc, self._retry_default)
                    log.warning("finnhub.rate_limited", path=path, retry_in_s=delay)
                    self._do_sleep(delay)
                    continue
                self._failed(path, exc)
                if exc.code == 429:  # noqa: PLR2004
                    raise FinnhubRateLimited(f"HTTP 429 twice for {label}") from None
                if exc.code == 403:  # noqa: PLR2004
                    raise FinnhubForbidden(f"HTTP 403 for {path} (not on the plan)") from None
                if self._raw:
                    raise urllib.error.HTTPError(
                        self._safe(exc.url), exc.code, self._safe(exc.msg), exc.headers, None
                    ) from None
                raise FinnhubError(f"HTTP {exc.code} for {label}") from None
            except FinnhubError:
                raise
            except Exception as exc:
                self._failed(path, exc)
                if self._raw:
                    raise
                detail = f"{type(exc).__name__} for {label}: {self._safe(exc)}"
                raise FinnhubError(detail) from None

    def _failed(self, path: str, exc: BaseException) -> None:
        log.warning(
            "finnhub.failed", path=path, error=self._safe(exc), error_class=type(exc).__name__
        )
