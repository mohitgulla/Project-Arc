"""Market calendar utilities built on exchange_calendars (XNYS).

Provides session queries, early-close detection, DTE math (calendar
and trading days), and a timezone-aware ``now_et()`` clock.

All datetime operations use ``America/New_York`` (ET).
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")

_XNYS: xcals.ExchangeCalendar | None = None


def _cal() -> xcals.ExchangeCalendar:
    """Lazily initialise the XNYS calendar (avoids import-time cost)."""
    global _XNYS  # noqa: PLW0603
    if _XNYS is None:
        _XNYS = xcals.get_calendar("XNYS")
    return _XNYS


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def now_et() -> _dt.datetime:
    """Current wall-clock time in America/New_York."""
    return _dt.datetime.now(tz=ET)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _to_ts(d: _dt.date | _dt.datetime) -> pd.Timestamp:
    """Convert a date/datetime to a tz-naive pd.Timestamp for calendar lookups."""
    if isinstance(d, _dt.datetime):
        return pd.Timestamp(d.date())
    return pd.Timestamp(d)


def is_session(d: _dt.date | None = None) -> bool:
    """Return True if *d* (default today ET) is a regular trading session."""
    if d is None:
        d = now_et().date()
    return bool(_cal().is_session(_to_ts(d)))


def is_open(dt: _dt.datetime | None = None) -> bool:
    """Return True if the market is open at *dt* (default now ET).

    Accounts for early closes — returns False after the early close time.
    """
    if dt is None:
        dt = now_et()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)

    return bool(_cal().is_open_on_minute(pd.Timestamp(dt)))


def next_session(d: _dt.date | None = None) -> _dt.date:
    """Return the next trading session *after* *d* (default today ET).

    *d* may be a weekend or holiday; the first session strictly after it is
    returned.
    """
    if d is None:
        d = now_et().date()
    ts = _to_ts(d)
    cal = _cal()
    if cal.is_session(ts):
        return cal.next_session(ts).date()
    return cal.date_to_session(ts, direction="next").date()


def previous_session(d: _dt.date | None = None) -> _dt.date:
    """Return the most recent trading session *before* *d* (default today ET).

    *d* may be a weekend or holiday; the last session strictly before it is
    returned.
    """
    if d is None:
        d = now_et().date()
    ts = _to_ts(d)
    cal = _cal()
    if cal.is_session(ts):
        return cal.previous_session(ts).date()
    return cal.date_to_session(ts, direction="previous").date()


def add_sessions(d: _dt.date, n: int) -> _dt.date:
    """Return the session *n* trading sessions after session-or-date *d* (n >= 0).

    ``add_sessions(d, 0)`` is *d* when it is a session, else the next session.
    """
    if n < 0:
        msg = "n must be >= 0"
        raise ValueError(msg)
    cur = d if is_session(d) else next_session(d)
    for _ in range(n):
        cur = next_session(cur)
    return cur


def sessions_between(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """Return all trading sessions in [*start*, *end*] (both inclusive), ascending."""
    if end < start:
        return []
    sessions = _cal().sessions_in_range(_to_ts(start), _to_ts(end))
    return [ts.date() for ts in sessions]


# ---------------------------------------------------------------------------
# Early closes
# ---------------------------------------------------------------------------


def is_early_close(d: _dt.date | None = None) -> bool:
    """Return True if *d* is an early-close session (e.g. day before holiday).

    Returns False if *d* is not a session at all.
    """
    if d is None:
        d = now_et().date()
    ts = _to_ts(d)
    cal = _cal()
    if not cal.is_session(ts):
        return False
    return bool(ts in cal.early_closes)


def session_open(d: _dt.date | None = None) -> _dt.datetime:
    """Return the open time (ET) for session *d*.

    Raises ValueError if *d* is not a trading session.
    """
    if d is None:
        d = now_et().date()
    ts = _to_ts(d)
    cal = _cal()
    if not cal.is_session(ts):
        msg = f"{d} is not a trading session"
        raise ValueError(msg)
    open_ts: pd.Timestamp = cal.session_open(ts)
    return open_ts.to_pydatetime().astimezone(ET)


def session_close(d: _dt.date | None = None) -> _dt.datetime:
    """Return the close time (ET) for session *d*.

    Raises ValueError if *d* is not a trading session.
    """
    if d is None:
        d = now_et().date()
    ts = _to_ts(d)
    cal = _cal()
    if not cal.is_session(ts):
        msg = f"{d} is not a trading session"
        raise ValueError(msg)
    close_ts: pd.Timestamp = cal.session_close(ts)
    return close_ts.to_pydatetime().astimezone(ET)


# ---------------------------------------------------------------------------
# DTE (Days To Expiration)
# ---------------------------------------------------------------------------


def dte_calendar(
    start: _dt.date | None = None,
    end: _dt.date | None = None,
) -> int:
    """Calendar days between *start* and *end* (inclusive of end day).

    Default *start* is today ET.
    """
    if start is None:
        start = now_et().date()
    if end is None:
        msg = "end date is required"
        raise ValueError(msg)
    return (end - start).days


def dte_trading(
    start: _dt.date | None = None,
    end: _dt.date | None = None,
) -> int:
    """Number of trading sessions between *start* (exclusive) and *end* (inclusive).

    Default *start* is today ET.
    """
    if start is None:
        start = now_et().date()
    if end is None:
        msg = "end date is required"
        raise ValueError(msg)
    cal = _cal()
    sessions = cal.sessions_in_range(
        pd.Timestamp(start) + pd.Timedelta(days=1),
        pd.Timestamp(end),
    )
    return len(sessions)
