"""Bounded retry of transient failures on Alpaca market-data **reads** (XP-1 day 1 fix).

On 2026-10-14 ~14:00Z Alpaca's market-data edge answered for a few seconds with
``rpc error: code = Unavailable desc = name resolver error: ... no such host``. One
blip failed production ``research`` and two XP-1 arms' ``risk.open`` for a whole slot.

:class:`ReadRetrySession` is a drop-in ``requests.Session`` for the alpaca-py data
clients (``client._session``; :func:`install_read_retry`). A ``GET`` that fails
transiently is sent again, at most ``policy.retries`` times:

* **transient** = HTTP 500 / 502 / 503 (Alpaca's ``Unavailable`` comes back as a 5xx),
  or a ``requests`` connection error (DNS, connection reset/refused), timeout or
  broken chunked read. 504 and 429 are left to alpaca-py's own retry (it already
  retries those), so they are never retried twice over.
* **never retried:** any other status (every 4xx: auth, 403 subscription refusals, 422,
  ...), any non-``GET`` method, so an order submit (``POST .../orders``) or a cancel
  can never be sent twice through this session.
* **backoff:** ``base_s * factor**k`` with ``± jitter`` (defaults 0.5 s then 1.5 s,
  ±25%), with the total sleep capped at ``budget_s`` (3 s).
* **rate:** each retry first takes one ``read`` token from the account's bucket
  (:class:`arc.utils.ratelimit.AccountRateBucket`, ``alpaca_rate_per_min``) when one is
  attached; the first attempt is unchanged.
* **exhausted:** the last response is returned (alpaca-py raises the same
  ``APIError`` as before) or the last exception re-raised: callers see exactly
  today's failure. Logged ``marketdata.retry`` per retry and
  ``marketdata.retry_exhausted`` once.

Sleep, clock and the jitter RNG are injected (tests never sleep or read the clock).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "TRANSIENT_STATUSES",
    "ReadRetryPolicy",
    "ReadRetrySession",
    "install_read_retry",
    "is_transient_error",
]

log = structlog.get_logger(__name__)

#: 5xx answers sent again. 504 (and 429) are retried by alpaca-py itself
#: (``DEFAULT_RETRY_EXCEPTION_CODES``), so they are left to it.
TRANSIENT_STATUSES: frozenset[int] = frozenset({500, 502, 503})

#: Only reads are retried: a repeated POST/PATCH/DELETE could double an order action.
_RETRY_METHODS: frozenset[str] = frozenset({"GET"})

_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,  # DNS failure, connection reset/refused, ConnectTimeout
    requests.Timeout,  # ReadTimeout
    requests.exceptions.ChunkedEncodingError,  # connection dropped mid-body
)


def is_transient_error(exc: BaseException) -> bool:
    """A network-level failure worth one more read (see the module doc)."""
    return isinstance(exc, _TRANSIENT_EXCEPTIONS)


@dataclass(frozen=True)
class ReadRetryPolicy:
    """How many times and how long a transient read is retried."""

    retries: int = 2
    base_s: float = 0.5
    factor: float = 3.0
    jitter: float = 0.25
    budget_s: float = 3.0

    def __post_init__(self) -> None:
        if self.retries < 0:
            msg = f"retries must be >= 0, got {self.retries}"
            raise ValueError(msg)
        if self.base_s < 0 or self.budget_s < 0 or self.factor < 1 or not 0 <= self.jitter < 1:
            msg = (
                f"bad retry policy (base {self.base_s}, factor {self.factor}, "
                f"jitter {self.jitter}, budget {self.budget_s})"
            )
            raise ValueError(msg)

    def delay(self, attempt: int, spent: float, rng: random.Random) -> float | None:
        """Sleep before retry *attempt* (1-based); ``None`` when the budget is spent."""
        if attempt > self.retries:
            return None
        nominal = self.base_s * self.factor ** (attempt - 1)
        wait = nominal * (1.0 + self.jitter * rng.uniform(-1.0, 1.0))
        left = self.budget_s - spent
        if left <= 0:
            return None
        return max(0.0, min(wait, left))


@dataclass
class _RetryDeps:
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)


def _path(url: str) -> str:
    return url.split("?", 1)[0].split("://", 1)[-1].split("/", 1)[-1]


class ReadRetrySession(requests.Session):
    """``requests.Session`` that retries transient ``GET`` failures (module doc)."""

    def __init__(
        self,
        policy: ReadRetryPolicy,
        *,
        bucket: Any = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__()
        self.policy = policy
        #: the account's shared rate bucket; each retry takes one ``read`` token
        self.bucket = bucket
        deps = _RetryDeps()
        self._sleep = sleep or deps.sleep
        self._clock = clock or deps.clock
        self._rng = rng or deps.rng

    def request(  # type: ignore[override]
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        send = super().request
        if method.upper() not in _RETRY_METHODS or self.policy.retries == 0:
            return send(method, url, *args, **kwargs)
        start = self._clock()
        spent = 0.0
        attempt = 0
        while True:
            error: str
            try:
                resp = send(method, url, *args, **kwargs)
            except Exception as exc:
                if not is_transient_error(exc):
                    raise
                failure: BaseException | requests.Response = exc
                error = type(exc).__name__
                status = None
            else:
                if resp.status_code not in TRANSIENT_STATUSES:
                    return resp
                failure = resp
                error = f"HTTP{resp.status_code}"
                status = resp.status_code
            attempt += 1
            wait = self.policy.delay(attempt, spent, self._rng)
            if wait is None:
                log.warning(
                    "marketdata.retry_exhausted",
                    method=method.upper(),
                    path=_path(url),
                    attempts=attempt,
                    error_class=error,
                    status=status,
                    elapsed_s=round(self._clock() - start, 3),
                )
                if isinstance(failure, requests.Response):
                    return failure
                raise failure
            log.warning(
                "marketdata.retry",
                method=method.upper(),
                path=_path(url),
                attempt=attempt,
                error_class=error,
                status=status,
                sleep_s=round(wait, 3),
            )
            if isinstance(failure, requests.Response):
                failure.close()
            self._sleep(wait)
            spent += wait
            if self.bucket is not None:
                self.bucket.acquire("read")


def install_read_retry(
    client: Any,
    policy: ReadRetryPolicy,
    *,
    bucket: Any = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    rng: random.Random | None = None,
) -> ReadRetrySession | None:
    """Swap *client*'s ``requests.Session`` for a :class:`ReadRetrySession`; return it.

    A client with no real ``requests.Session`` (a test double) is left as it is
    (``None``). Headers and mounted adapters carry over.
    """
    old = getattr(client, "_session", None)
    if not isinstance(old, requests.Session):
        return None
    if isinstance(old, ReadRetrySession):
        return old
    session = ReadRetrySession(policy, bucket=bucket, sleep=sleep, clock=clock, rng=rng)
    session.headers.update(old.headers)
    for prefix, adapter in old.adapters.items():
        session.mount(prefix, adapter)
    client._session = session  # noqa: SLF001 - alpaca-py exposes no session hook
    return session
