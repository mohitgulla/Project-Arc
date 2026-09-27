"""Tests for arc.utils.calendar — sessions, early closes, DTE, now_et().

Tests cover:
- Session detection (weekday vs weekend vs holiday)
- Early-close days (day before Independence Day, day after Thanksgiving)
- DST boundaries (spring forward / fall back)
- DTE calculations (calendar and trading days)
- now_et() timezone
"""

from __future__ import annotations

import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from arc.utils import calendar as cal

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# now_et
# ---------------------------------------------------------------------------


class TestNowEt:
    def test_returns_et_timezone(self) -> None:
        result = cal.now_et()
        assert result.tzinfo is not None
        # Normalize: the tz name should be America/New_York (or ET/EST/EDT)
        assert str(result.tzinfo) == "America/New_York"

    def test_returns_current_time(self) -> None:
        before = dt.datetime.now(tz=ET)
        result = cal.now_et()
        after = dt.datetime.now(tz=ET)
        assert before <= result <= after


# ---------------------------------------------------------------------------
# is_session
# ---------------------------------------------------------------------------


class TestIsSession:
    def test_regular_weekday(self) -> None:
        # 2026-01-02 is a Friday, regular session
        assert cal.is_session(dt.date(2026, 1, 2)) is True

    def test_weekend(self) -> None:
        # 2026-01-03 is Saturday
        assert cal.is_session(dt.date(2026, 1, 3)) is False
        # 2026-01-04 is Sunday
        assert cal.is_session(dt.date(2026, 1, 4)) is False

    def test_new_years_day_holiday(self) -> None:
        # 2026-01-01 is New Year's Day (observed) — market closed
        assert cal.is_session(dt.date(2026, 1, 1)) is False

    def test_mlk_day(self) -> None:
        # 2026-01-19 is MLK Day — market closed
        assert cal.is_session(dt.date(2026, 1, 19)) is False


# ---------------------------------------------------------------------------
# is_open (intraday)
# ---------------------------------------------------------------------------


class TestIsOpen:
    def test_market_hours(self) -> None:
        # 2026-01-02 10:00 ET — market open
        t = dt.datetime(2026, 1, 2, 10, 0, tzinfo=ET)
        assert cal.is_open(t) is True

    def test_before_open(self) -> None:
        # 2026-01-02 09:00 ET — before market open (9:30)
        t = dt.datetime(2026, 1, 2, 9, 0, tzinfo=ET)
        assert cal.is_open(t) is False

    def test_after_close(self) -> None:
        # 2026-01-02 16:30 ET — after market close (16:00)
        t = dt.datetime(2026, 1, 2, 16, 30, tzinfo=ET)
        assert cal.is_open(t) is False

    def test_weekend_datetime(self) -> None:
        t = dt.datetime(2026, 1, 3, 12, 0, tzinfo=ET)
        assert cal.is_open(t) is False


# ---------------------------------------------------------------------------
# Early closes
# ---------------------------------------------------------------------------


class TestEarlyClose:
    def test_christmas_eve_2026(self) -> None:
        # Dec 24, 2026 — NYSE early close (Thursday before Christmas)
        assert cal.is_early_close(dt.date(2026, 12, 24)) is True

    def test_black_friday_2026(self) -> None:
        # Nov 27, 2026 — day after Thanksgiving, early close
        assert cal.is_early_close(dt.date(2026, 11, 27)) is True

    def test_regular_day_not_early_close(self) -> None:
        assert cal.is_early_close(dt.date(2026, 1, 2)) is False

    def test_non_session_returns_false(self) -> None:
        # Weekend is not an early close
        assert cal.is_early_close(dt.date(2026, 1, 3)) is False

    def test_early_close_session_close_time(self) -> None:
        # Early-close day should close at 13:00 ET
        close = cal.session_close(dt.date(2026, 11, 27))
        assert close.hour == 13
        assert close.minute == 0

    def test_regular_session_close_time(self) -> None:
        close = cal.session_close(dt.date(2026, 1, 2))
        assert close.hour == 16
        assert close.minute == 0


# ---------------------------------------------------------------------------
# DST boundaries
# ---------------------------------------------------------------------------


class TestDST:
    """Market times are always ET; verify DST transitions don't break anything."""

    def test_spring_forward_2026(self) -> None:
        # DST springs forward on 2026-03-08 (Sunday — not a session).
        # Monday 2026-03-09 should be a normal session.
        assert cal.is_session(dt.date(2026, 3, 9)) is True
        # 10:00 ET should be open regardless of DST change
        t = dt.datetime(2026, 3, 9, 10, 0, tzinfo=ET)
        assert cal.is_open(t) is True

    def test_fall_back_2026(self) -> None:
        # DST falls back on 2026-11-01 (Sunday).
        # Monday 2026-11-02 should be a normal session.
        assert cal.is_session(dt.date(2026, 11, 2)) is True
        t = dt.datetime(2026, 11, 2, 10, 0, tzinfo=ET)
        assert cal.is_open(t) is True

    def test_now_et_during_dst(self) -> None:
        # Freeze time to a DST date and verify timezone
        frozen = dt.datetime(2026, 7, 15, 12, 0, tzinfo=ET)
        with mock.patch("arc.utils.calendar.now_et", return_value=frozen):
            result = cal.now_et()
        assert result == frozen


# ---------------------------------------------------------------------------
# next_session / previous_session
# ---------------------------------------------------------------------------


class TestSessionNavigation:
    def test_next_session_from_friday(self) -> None:
        # 2026-01-02 is Friday; next session is Monday 2026-01-05
        nxt = cal.next_session(dt.date(2026, 1, 2))
        assert nxt == dt.date(2026, 1, 5)

    def test_next_session_skips_holiday(self) -> None:
        # 2026-01-16 Friday; next would be Monday 2026-01-19 MLK Day;
        # so next session is 2026-01-20 Tuesday
        nxt = cal.next_session(dt.date(2026, 1, 16))
        assert nxt == dt.date(2026, 1, 20)

    def test_previous_session(self) -> None:
        # 2026-01-05 Monday; previous is 2026-01-02 Friday
        prev = cal.previous_session(dt.date(2026, 1, 5))
        assert prev == dt.date(2026, 1, 2)


# ---------------------------------------------------------------------------
# DTE
# ---------------------------------------------------------------------------


class TestDTE:
    def test_calendar_dte(self) -> None:
        start = dt.date(2026, 1, 2)
        end = dt.date(2026, 1, 16)
        assert cal.dte_calendar(start, end) == 14

    def test_trading_dte(self) -> None:
        # 2026-01-02 (Fri) to 2026-01-16 (Fri)
        # Sessions: Jan 5,6,7,8,9, 12,13,14,15,16 = 10 trading days
        start = dt.date(2026, 1, 2)
        end = dt.date(2026, 1, 16)
        result = cal.dte_trading(start, end)
        assert result == 10

    def test_trading_dte_across_holiday(self) -> None:
        # Jan 16 (Fri) to Jan 23 (Fri), MLK Day Jan 19
        # Sessions: Jan 20,21,22,23 = 4 trading days
        start = dt.date(2026, 1, 16)
        end = dt.date(2026, 1, 23)
        result = cal.dte_trading(start, end)
        assert result == 4

    def test_calendar_dte_requires_end(self) -> None:
        with pytest.raises(ValueError, match="end date"):
            cal.dte_calendar(dt.date(2026, 1, 2))

    def test_trading_dte_requires_end(self) -> None:
        with pytest.raises(ValueError, match="end date"):
            cal.dte_trading(dt.date(2026, 1, 2))

    def test_zero_dte_same_day(self) -> None:
        d = dt.date(2026, 1, 2)
        assert cal.dte_calendar(d, d) == 0
        assert cal.dte_trading(d, d) == 0


# ---------------------------------------------------------------------------
# session_close validation
# ---------------------------------------------------------------------------


class TestSessionClose:
    def test_non_session_raises(self) -> None:
        with pytest.raises(ValueError, match="not a trading session"):
            cal.session_close(dt.date(2026, 1, 3))  # Saturday
