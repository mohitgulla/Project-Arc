"""E8.7c: closed-trade stats (arc.journal.tradestats) and equity-curve stats
(arc.reconcile.performance: drawdown, daily returns, Sharpe, period return)."""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.journal.scorecard import ClosedPosition
from arc.journal.tradestats import breakdown, hold_hit_rate, trade_stats
from arc.reconcile.performance import (
    TRADING_DAYS,
    DailyEquity,
    daily_returns,
    drawdown,
    period_return,
    sharpe,
)
from arc.utils.calendar import ET

T0 = dt.datetime(2026, 9, 1, 10, tzinfo=ET)


def _c(
    pnl: float,
    *,
    i: int = 0,
    ticker: str = "SPY",
    days: float | None = 3.0,
    shadow: float | None = None,
) -> ClosedPosition:
    opened = T0 + dt.timedelta(days=i)
    return ClosedPosition(
        structure_id=f"os-{i:03d}",
        ticker=ticker,
        kind="vertical_debit",
        contracts=1,
        opened_at=opened if days is not None else None,
        closed_at=opened + dt.timedelta(days=days or 1.0),
        expiration=dt.date(2026, 12, 18),
        early=True,
        exit_reason="profit_target",
        entry_net=2.0,
        realised_pnl=pnl,
        shadow_hold_pnl=shadow,
        open_proposal_hash=f"h{i:03d}",
    )


def _eq(values: list[float], start: dt.date = dt.date(2026, 9, 1)) -> list[DailyEquity]:
    return [
        DailyEquity(start + dt.timedelta(days=k), Decimal(str(v))) for k, v in enumerate(values)
    ]


# ---------------------------------------------------------------------------
# trade_stats
# ---------------------------------------------------------------------------


def test_trade_stats_definitions() -> None:
    closed = [_c(300, i=0, days=2), _c(-100, i=1, days=4), _c(0, i=2, days=6), _c(100, i=3)]
    s = trade_stats(closed)
    assert (s.closed, s.wins, s.losses) == (4, 2, 2)  # a scratch at 0 is a loss (PnlSummary)
    assert s.realised == 300.0
    assert s.win_rate == 0.5
    assert s.avg_win == 200.0 and s.avg_loss == -50.0
    assert s.profit_factor == pytest.approx(4.0)
    assert s.expectancy == pytest.approx(75.0)
    assert s.avg_days_held == pytest.approx((2 + 4 + 6 + 3) / 4)
    assert s.best is not None and s.best.structure_id == "os-000"
    assert s.worst is not None and s.worst.structure_id == "os-001"


def test_trade_stats_empty_and_no_losses() -> None:
    assert trade_stats([]).closed == 0 and trade_stats([]).win_rate is None
    s = trade_stats([_c(50, i=0, days=None)])
    assert s.profit_factor is None  # no losing dollars
    assert s.avg_loss is None and s.avg_days_held is None


@given(st.lists(st.floats(-5000, 5000, allow_nan=False), min_size=1, max_size=40))
def test_expectancy_identity(pnls: list[float]) -> None:
    """expectancy = win_rate x avg_win + (1 - win_rate) x avg_loss = Σ / n."""
    s = trade_stats([_c(p, i=k) for k, p in enumerate(pnls)])
    assert s.expectancy is not None and s.win_rate is not None
    parts = s.win_rate * (s.avg_win or 0.0) + (1 - s.win_rate) * (s.avg_loss or 0.0)
    assert s.expectancy == pytest.approx(sum(pnls) / len(pnls), abs=1e-6)
    assert parts == pytest.approx(s.expectancy, abs=1e-6)
    assert s.wins + s.losses == s.closed
    assert s.best is not None and s.best.realised_pnl == max(pnls)


def test_hold_hit_rate() -> None:
    assert hold_hit_rate([_c(1)]) == (None, 0)
    rate, n = hold_hit_rate([_c(1, shadow=10), _c(1, i=1, shadow=-5), _c(1, i=2)])
    assert (rate, n) == (0.5, 2)


def test_breakdown_ranks_and_shares() -> None:
    rows = breakdown(
        [("SPY", _c(300)), ("QQQ", _c(-100, i=1)), ("SPY", _c(-100, i=2)), (None, _c(50, i=3))]
    )
    assert [r.key for r in rows] == ["SPY", "", "QQQ"]
    spy = rows[0]
    assert (spy.count, spy.wins, spy.pnl, spy.win_rate) == (2, 1, 200.0, 0.5)
    assert sum(r.share for r in rows) == pytest.approx(1.0)
    assert spy.share == pytest.approx(200 / 350)


# ---------------------------------------------------------------------------
# drawdown / returns / Sharpe
# ---------------------------------------------------------------------------


def test_drawdown_peak_trough_recovery() -> None:
    s = _eq([100, 110, 99, 104, 88, 95, 111, 90])
    dd = drawdown(s)
    assert dd.amount == Decimal(-22)  # 110 -> 88
    assert dd.pct == pytest.approx(-0.2)
    assert (dd.peak_day, dd.trough_day) == (s[1].day, s[4].day)
    assert dd.recovered == s[6].day


def test_drawdown_monotonic_and_underwater() -> None:
    up = drawdown(_eq([1, 2, 3]))
    assert up.amount == 0 and up.peak_day is None
    s = _eq([10, 8, 9])
    dd = drawdown(s)
    assert dd.amount == Decimal(-2) and dd.recovered is None


@given(st.lists(st.integers(1, 10_000), min_size=1, max_size=60))
def test_drawdown_is_the_worst_fall_from_a_prior_peak(values: list[int]) -> None:
    dd = drawdown(_eq([float(v) for v in values]))
    brute = min(
        [0] + [values[j] - max(values[: j + 1]) for j in range(len(values))],
    )
    assert dd.amount == Decimal(brute)
    assert dd.amount <= 0


def test_daily_returns_and_sharpe() -> None:
    s = _eq([100, 101, 99.99, 102])
    r = daily_returns(s)
    assert r == pytest.approx([0.01, -0.01, 102 / 99.99 - 1])
    mean = sum(r) / 3
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 2)
    assert sharpe(r) == pytest.approx(mean / sd * math.sqrt(TRADING_DAYS))
    assert sharpe([0.01]) is None
    assert sharpe([0.01, 0.01]) is None  # zero dispersion


def test_period_return_uses_the_prior_close() -> None:
    s = _eq([100, 110, 121, 133.1])
    start, end, ret = period_return(s, s[2].day, s[3].day)
    assert (start, end) == (Decimal("110"), Decimal("133.1"))
    assert ret == pytest.approx(0.21)
    # no prior close: the first close inside (since inception)
    assert period_return(s, s[0].day, s[1].day)[0] == Decimal("100")
    assert period_return(s, dt.date(2030, 1, 1), dt.date(2030, 2, 1)) == (None, None, None)
