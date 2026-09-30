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
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from arc.utils import calendar as cal

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# now_et
# ---------------------------------------------------------------------------


class _FrozenDatetime(dt.datetime):
    """``datetime`` whose ``now()`` returns a fixed instant (no wall-clock read)."""

    frozen = dt.datetime(2026, 1, 15, 17, 30, tzinfo=dt.UTC)

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> _FrozenDatetime:  # type: ignore[override]
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> type[_FrozenDatetime]:
    """Swap the ``datetime`` module seen by ``arc.utils.calendar`` for a frozen one."""
    public = {k: getattr(dt, k) for k in dir(dt) if not k.startswith("_")}
    monkeypatch.setattr(cal, "_dt", SimpleNamespace(**{**public, "datetime": _FrozenDatetime}))
    return _FrozenDatetime


class TestNowEt:
    # now_et() is the clock under test; the frozen_clock fixture pins datetime.now(),
    # so these calls read the fixed instant, not the wall clock.
    def test_returns_et_timezone(self, frozen_clock: type[_FrozenDatetime]) -> None:
        result = cal.now_et()  # wall-clock: unit test of now_et itself (datetime frozen)
        assert result.tzinfo is not None
        # Normalize: the tz name should be America/New_York (or ET/EST/EDT)
        assert str(result.tzinfo) == "America/New_York"

    def test_returns_current_time(self, frozen_clock: type[_FrozenDatetime]) -> None:
        # now_et() returns the instant datetime.now() reports, converted to ET.
        result = cal.now_et()  # wall-clock: unit test of now_et itself (datetime frozen)
        assert result == frozen_clock.frozen
        assert result.utcoffset() == dt.timedelta(hours=-5)  # EST in January


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

    def test_now_et_during_dst(
        self, frozen_clock: type[_FrozenDatetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Freeze datetime.now() to a DST instant: now_et() reports EDT (UTC-4).
        frozen = dt.datetime(2026, 7, 15, 12, 0, tzinfo=ET)
        monkeypatch.setattr(frozen_clock, "frozen", frozen)
        result = cal.now_et()  # wall-clock: unit test of now_et itself (datetime frozen)
        assert result == frozen
        assert result.utcoffset() == dt.timedelta(hours=-4)


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

    def test_next_session_from_weekend_and_holiday(self) -> None:
        assert cal.next_session(dt.date(2026, 1, 3)) == dt.date(2026, 1, 5)  # Saturday
        assert cal.next_session(dt.date(2026, 1, 19)) == dt.date(2026, 1, 20)  # MLK Day

    def test_previous_session_from_weekend(self) -> None:
        assert cal.previous_session(dt.date(2026, 1, 4)) == dt.date(2026, 1, 2)  # Sunday

    def test_add_sessions(self) -> None:
        assert cal.add_sessions(dt.date(2026, 1, 16), 0) == dt.date(2026, 1, 16)
        assert cal.add_sessions(dt.date(2026, 1, 17), 0) == dt.date(2026, 1, 20)
        assert cal.add_sessions(dt.date(2026, 1, 16), 2) == dt.date(2026, 1, 21)
        with pytest.raises(ValueError, match="n must be"):
            cal.add_sessions(dt.date(2026, 1, 16), -1)


class TestSessionOpen:
    def test_regular_open(self) -> None:
        assert cal.session_open(dt.date(2026, 1, 5)) == dt.datetime(
            2026, 1, 5, 9, 30, tzinfo=cal.ET
        )

    def test_non_session_raises(self) -> None:
        with pytest.raises(ValueError, match="not a trading session"):
            cal.session_open(dt.date(2026, 1, 3))


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


class TestNonSessionNavigation:
    """next/previous_session accept weekends and holidays (used by session TTLs)."""

    def test_next_from_saturday_and_holiday(self) -> None:
        assert cal.next_session(dt.date(2026, 9, 26)) == dt.date(2026, 9, 28)
        assert cal.next_session(dt.date(2026, 11, 26)) == dt.date(2026, 11, 27)  # Thanksgiving

    def test_previous_from_sunday_and_holiday(self) -> None:
        assert cal.previous_session(dt.date(2026, 9, 27)) == dt.date(2026, 9, 25)
        assert cal.previous_session(dt.date(2026, 11, 26)) == dt.date(2026, 11, 25)


class TestSessionOpenAndPhase:
    def test_session_open(self) -> None:
        assert cal.session_open(dt.date(2026, 9, 28)) == dt.datetime(2026, 9, 28, 9, 30, tzinfo=ET)
        with pytest.raises(ValueError, match="not a trading session"):
            cal.session_open(dt.date(2026, 9, 27))

    def test_phases(self) -> None:
        assert cal.session_phase(dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET)) == "pre"
        assert cal.session_phase(dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)) == "open"
        assert cal.session_phase(dt.datetime(2026, 9, 28, 16, 0, tzinfo=ET)) == "post"
        assert cal.session_phase(dt.datetime(2026, 9, 27, 12, 0, tzinfo=ET)) == "closed"
        # early close: Black Friday closes 13:00
        assert cal.session_phase(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)) == "post"
        # naive input is read as ET
        assert cal.session_phase(dt.datetime(2026, 9, 28, 12, 0)) == "open"

    def test_phase_default_now(self) -> None:
        with mock.patch.object(
            cal, "now_et", return_value=dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)
        ):
            assert cal.session_phase() == "open"
            assert cal.session_open() == dt.datetime(2026, 9, 28, 9, 30, tzinfo=ET)
