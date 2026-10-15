"""D117: a spec v3 experiment started mid-session counts that day as a partial session.

Every v3 arm (control included) opens flat at the same t0 equity at the same instant,
so t0 to the close is a paired observation. v1/v2 keep the full-session rule: their
control is production's broker equity, whose previous EOD row includes the pre-t0
morning.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest

from arc.experiments.config import ExperimentsConfig
from arc.experiments.evaluate import ExperimentReport, _first_session, build_report
from arc.experiments.store import ExperimentStore
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, previous_session
from tests import experiment_fixtures as fx

MON = dt.date(2026, 10, 5)  # a regular session
TUE = dt.date(2026, 10, 6)
FRI_HALF = dt.date(2026, 11, 27)  # day after Thanksgiving, 13:00 ET close
MON_AFTER = dt.date(2026, 11, 30)
OVERLAY = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}


def _at(day: dt.date, h: int, m: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


@pytest.mark.parametrize(
    ("t0", "full", "partial"),
    [
        (_at(MON, 9, 0), MON, MON),  # before the open: the whole day, either rule
        (_at(MON, 9, 30), MON, MON),  # at the open
        (_at(MON, 9, 39), TUE, MON),  # 9 min in: skipped (full) / partial day (D117)
        (_at(MON, 15, 59), TUE, MON),  # a minute before the close
        (_at(MON, 16, 0), TUE, TUE),  # at the close: nothing left of the day
        (_at(MON, 18, 0), TUE, TUE),  # after the close
        (_at(dt.date(2026, 10, 3), 12, 0), MON, MON),  # Saturday
        (_at(FRI_HALF, 12, 0), MON_AFTER, FRI_HALF),  # half day, before its 13:00 close
        (_at(FRI_HALF, 13, 30), MON_AFTER, MON_AFTER),  # half day, after its close
        (dt.datetime(2026, 10, 5, 13, 39, tzinfo=dt.UTC), TUE, MON),  # 09:39 ET in UTC
    ],
)
def test_first_session(t0: dt.datetime, full: dt.date, partial: dt.date) -> None:
    assert _first_session(t0) == full
    assert _first_session(t0, partial=True) == partial


def _conn():  # type: ignore[no-untyped-def]
    c = connect(":memory:")
    migrate(c)
    return c


def _report(c: Any, eid: str, days: list[dt.date]) -> ExperimentReport:
    st_ = ExperimentStore(c, now=lambda: fx.T0).require(eid)
    now = fx.eod(days[-1]) + dt.timedelta(minutes=15)
    with mock.patch("arc.experiments.evaluate._evaluator_sha", return_value="e" * 40):
        return build_report(c, st_, ExperimentsConfig(), now=now)


T0_MID = _at(MON, 9, 39)


def test_v3_mid_session_start_counts_the_partial_day() -> None:
    """XP-1's case: t0 09:39 ET, every arm's first EOD row is that evening."""
    c = _conn()
    fx.start(c, fx.spec("XP-30", arms={"treatments": {"t1": OVERLAY}}, spec_version=3), t0=T0_MID)
    days = fx.sessions(3, start=MON)
    fx.pnl_row(c, previous_session(MON), fx.T0_EQUITY + 777.0, None)  # production only
    ce = te = fx.T0_EQUITY
    for day, (cp, tp) in zip(days, [(-28.0, 12.0), (10.0, 30.0), (5.0, 5.0)], strict=True):
        ce, te = ce + cp, te + tp
        fx.pnl_row(c, day, fx.T0_EQUITY, None)
        fx.pnl_row(c, day, ce, "XP-30:control")
        fx.pnl_row(c, day, te, "XP-30:t1")
    c.commit()
    rep = _report(c, "XP-30", days)
    assert rep.sessions == 3 and rep.missing_sessions == []
    pair = next(p for p in rep.pairwise or [] if p.kind == "control_vs_treatment")
    first = pair.series[0]
    assert first.day == MON
    # the partial day's P&L is measured from the t0 equity, not production's prior close
    assert (first.control_pnl, first.treatment_pnl) == (pytest.approx(-28.0), pytest.approx(12.0))
    assert first.d == pytest.approx(40.0 / fx.T0_EQUITY)


def test_v3_start_at_or_after_the_close_waits_for_the_next_session() -> None:
    c = _conn()
    sp = fx.spec("XP-30", arms={"treatments": {"t1": OVERLAY}}, spec_version=3)
    fx.start(c, sp, t0=_at(MON, 16, 5))
    days = fx.sessions(2, start=TUE)
    for i, day in enumerate(days, 1):
        fx.pnl_row(c, day, fx.T0_EQUITY + i, "XP-30:control")
        fx.pnl_row(c, day, fx.T0_EQUITY + 2 * i, "XP-30:t1")
    c.commit()
    rep = _report(c, "XP-30", days)
    assert rep.sessions == 2
    pair = next(p for p in rep.pairwise or [] if p.kind == "control_vs_treatment")
    assert [s.day for s in pair.series] == days


def test_v2_mid_session_start_still_skips_the_day() -> None:
    """v1/v2 control is production's broker equity: its pre-t0 morning can't be split off."""
    c = _conn()
    fx.start(c, fx.spec("XP-2"), t0=T0_MID)
    days = fx.sessions(3, start=MON)
    fx.pnl_row(c, previous_session(MON), fx.T0_EQUITY, None)
    e = fx.T0_EQUITY
    for day in days:
        e += 10.0
        fx.pnl_row(c, day, e, None)
        fx.pnl_row(c, day, e + 5.0, "XP-2:treatment")
    c.commit()
    rep = _report(c, "XP-2", days)
    assert rep.sessions == 2
    assert [s.day for s in rep.series] == days[1:]
