"""TTL values and timestamp helpers shared by the context store and the dispatcher.

A TTL is either a wall-clock duration (``"30m"``, ``"2h"``, ``"5d"``) or a number of
trading sessions (``"1 session"``, ``"5 sessions"``). Session TTLs expire at the
close of the Nth session, counting the session the entry becomes valid in (or the
next session, if it becomes valid after that day's close or on a non-trading day).
So a StockedUp brief written Sunday 22:00 ET with ``1 session`` informs Monday's
session and expires at Monday's close (D14).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.utils.calendar import ET, is_session, next_session, session_close

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_SESSION_RE = re.compile(r"^\s*(\d+)\s*sessions?\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Fixed-width UTC format: lexicographic order == chronological order in SQLite.
_DB_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def parse_duration(text: str) -> _dt.timedelta:
    """Parse ``"90s" | "15m" | "2h" | "3d"`` into a timedelta (must be > 0)."""
    m = _DURATION_RE.match(str(text))
    if not m:
        msg = f"invalid duration {text!r}; expected e.g. '30s', '15m', '2h', '3d'"
        raise ValueError(msg)
    value = int(m.group(1))
    if value <= 0:
        msg = f"duration must be positive, got {text!r}"
        raise ValueError(msg)
    return _dt.timedelta(seconds=value * _UNIT_SECONDS[m.group(2)])


def require_aware(dt: _dt.datetime, name: str = "datetime") -> _dt.datetime:
    """Reject naive datetimes: every time in Arc carries a timezone."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        msg = f"{name} must be timezone-aware, got naive {dt!r}"
        raise ValueError(msg)
    return dt


def to_db(dt: _dt.datetime) -> str:
    """Serialize an aware datetime as fixed-width UTC text for SQLite."""
    return require_aware(dt).astimezone(_dt.UTC).strftime(_DB_FMT)


def from_db(text: str) -> _dt.datetime:
    """Parse a :func:`to_db` string back into an ET-aware datetime."""
    return _dt.datetime.strptime(text, _DB_FMT).replace(tzinfo=_dt.UTC).astimezone(ET)


class Ttl(BaseModel):
    """A context/catch-up lifetime: a duration or a count of trading sessions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration: _dt.timedelta | None = None
    sessions: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        if isinstance(v, cls):
            return v
        if isinstance(v, _dt.timedelta):
            return {"duration": v}
        if isinstance(v, str):
            m = _SESSION_RE.match(v)
            if m:
                return {"sessions": int(m.group(1))}
            return {"duration": parse_duration(v)}
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> Ttl:
        if (self.duration is None) == (self.sessions is None):
            msg = "TTL needs exactly one of duration or sessions"
            raise ValueError(msg)
        if self.duration is not None and self.duration <= _dt.timedelta(0):
            msg = "TTL duration must be positive"
            raise ValueError(msg)
        return self

    def expires_at(self, valid_from: _dt.datetime) -> _dt.datetime:
        """Absolute expiry for an entry that becomes valid at *valid_from*."""
        start = require_aware(valid_from, "valid_from").astimezone(ET)
        if self.duration is not None:
            return start + self.duration
        assert self.sessions is not None  # guaranteed by _exactly_one
        day = start.date()
        if not (is_session(day) and start < session_close(day)):
            day = next_session(day)
        for _ in range(self.sessions - 1):
            day = next_session(day)
        return session_close(day)

    def __str__(self) -> str:
        if self.sessions is not None:
            return f"{self.sessions} session" + ("s" if self.sessions > 1 else "")
        assert self.duration is not None
        secs = int(self.duration.total_seconds())
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs % size == 0:
                return f"{secs // size}{unit}"
        return f"{secs}s"
