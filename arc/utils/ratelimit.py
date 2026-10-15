"""D69 (E15.3): one Alpaca request budget per account, shared by every arm process.

Alpaca allows 200 trading-API requests per minute **per account**. Every experiment arm
on the shared ``ALPACA_EXP_*`` account runs in its own process, so a per-process limit
cannot hold the account under it. :class:`AccountRateBucket` is a token bucket whose
state lives in one file per account key (``data/locks/alpaca-<last4>.bucket``), read and
written under an exclusive ``fcntl.flock``, so every process on the host draws from the
same tokens.

* **Rate:** ``alpaca_rate_per_min`` (default 180, 10% under Alpaca's 200), refilled
  continuously. **Burst:** ``capacity = max(1, rate // 10)`` tokens, so a rolling minute
  never exceeds ``rate + capacity`` (198 at the default) even after an idle spell.
* **Priority:** an order submit takes a token whenever one is there; a read only when
  more than ``read_reserve = capacity // 4`` tokens are left, so reads queue behind
  submits and a burst of reads can never make a submit wait for a full refill.
* **Fairness:** a waiter sleeps only until its token is due (never more than
  ``max_sleep``) and re-checks under the lock; no process holds the lock while
  sleeping, so N processes interleave at the refill rate.
* **429:** :meth:`AccountRateBucket.penalise` empties the bucket (every process backs
  off) and :meth:`AccountRateBucket.state` is what the caller logs with it.

Time and sleep are injected (tests never read the wall clock); the default clock is
``time.time`` because the file is shared across processes.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_LOCK_DIR",
    "AccountRateBucket",
    "BucketState",
    "bucket_path",
    "request_kind",
]

log = structlog.get_logger(__name__)

#: ``data/locks`` in the repo (gitignored with the rest of ``data/``).
DEFAULT_LOCK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "locks"

Kind = Literal["submit", "read"]


def bucket_path(api_key: str, lock_dir: Path | str | None = None) -> Path:
    """``<lock_dir>/alpaca-<last4 of the key id>.bucket`` (never the secret)."""
    last4 = "".join(ch for ch in api_key[-4:] if ch.isalnum()) or "none"
    return Path(lock_dir or DEFAULT_LOCK_DIR) / f"alpaca-{last4}.bucket"


def request_kind(method: str, url: str) -> Kind:
    """An order submit (``POST .../orders``) or anything else (a read, a cancel, ...)."""
    path = url.split("?", 1)[0].rstrip("/")
    return "submit" if method.upper() == "POST" and path.endswith("/orders") else "read"


@dataclass(frozen=True)
class BucketState:
    """The bucket as of one look (logged with a 429)."""

    tokens: float
    capacity: int
    rate_per_min: int
    at: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "tokens": round(self.tokens, 3),
            "capacity": self.capacity,
            "rate_per_min": self.rate_per_min,
            "at": round(self.at, 3),
        }


class AccountRateBucket:
    """File-locked token bucket for one Alpaca account (see the module doc)."""

    def __init__(
        self,
        path: Path | str,
        rate_per_min: int,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep: float = 1.0,
    ) -> None:
        if rate_per_min < 1:
            msg = f"rate_per_min must be >= 1, got {rate_per_min}"
            raise ValueError(msg)
        self.path = Path(path)
        self.rate_per_min = int(rate_per_min)
        self.capacity = max(1, self.rate_per_min // 10)
        self.read_reserve = self.capacity // 4
        self._clock = clock
        self._sleep = sleep
        self._max_sleep = max_sleep

    @property
    def per_second(self) -> float:
        return self.rate_per_min / 60.0

    # -- file state (always under the lock) -----------------------------------------

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)

    def _read(self, fd: int, now: float) -> float:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        try:
            data = json.loads(raw.decode() or "{}")
            tokens, at = float(data["tokens"]), float(data["at"])
        except (ValueError, KeyError, TypeError):
            return float(self.capacity)  # a new or unreadable file starts full
        elapsed = max(0.0, now - at)
        return min(float(self.capacity), tokens + elapsed * self.per_second)

    def _write(self, fd: int, tokens: float, now: float) -> None:
        data = json.dumps({"tokens": tokens, "at": now}).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)

    def _locked(self, op: Callable[[int, float], float | None]) -> float | None:
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                return op(fd, self._clock())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- API ------------------------------------------------------------------------

    def try_take(self, kind: Kind = "read") -> float:
        """Take one token if *kind* may; return 0.0, else the seconds until it may."""
        floor = 1.0 + (self.read_reserve if kind == "read" else 0)

        def op(fd: int, now: float) -> float:
            tokens = self._read(fd, now)
            if tokens >= floor:
                self._write(fd, tokens - 1.0, now)
                return 0.0
            self._write(fd, tokens, now)
            return (floor - tokens) / self.per_second

        out = self._locked(op)
        return 0.0 if out is None else out

    def acquire(self, kind: Kind = "read") -> float:
        """Block until a token is taken; return the seconds waited (by the injected clock)."""
        start = self._clock()
        while True:
            wait = self.try_take(kind)
            if wait <= 0.0:
                waited = self._clock() - start
                if waited > 0:
                    log.debug("alpaca.rate_waited", kind=kind, seconds=round(waited, 3))
                return waited
            self._sleep(min(wait, self._max_sleep))

    def penalise(self) -> BucketState:
        """A 429 came back: empty the bucket (every process backs off); the state before."""
        before: list[float] = []

        def op(fd: int, now: float) -> None:
            before.append(self._read(fd, now))
            self._write(fd, 0.0, now)

        self._locked(op)
        return BucketState(before[0], self.capacity, self.rate_per_min, self._clock())

    def state(self) -> BucketState:
        """The bucket now (read-only)."""
        seen: list[float] = []

        def op(fd: int, now: float) -> None:
            seen.append(self._read(fd, now))

        self._locked(op)
        return BucketState(seen[0], self.capacity, self.rate_per_min, self._clock())
