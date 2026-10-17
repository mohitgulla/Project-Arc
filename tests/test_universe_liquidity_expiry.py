"""E12.4a: the liquidity screen measures the in-window expiry with the most near-ATM OI."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from arc.data.base import OptionContract
from arc.universe import LiquidityThresholds, measure_liquidity, screen_liquidity
from arc.universe.screen import pick_expiry_by_atm_oi, pick_liquidity_expiry

TODAY = dt.date(2026, 10, 16)
WEEKLY_NOV6 = dt.date(2026, 11, 6)  # 21 DTE
WEEKLY_NOV13 = dt.date(2026, 11, 13)  # 28 DTE (old nearest-30 pick)
MONTHLY_NOV20 = dt.date(2026, 11, 20)  # 35 DTE
WEEKLY_NOV27 = dt.date(2026, 11, 27)  # 42 DTE
T = LiquidityThresholds()  # atm_strikes 3


def _c(
    exp: dt.date,
    strike: float,
    right: str,
    oi: int | None,
    *,
    bid: float | None = 1.0,
    ask: float | None = 1.1,
) -> OptionContract:
    return OptionContract(
        symbol=f"X{exp:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}",
        underlying="X",
        expiration=exp,
        strike=strike,
        option_type=right,
        bid=bid,
        ask=ask,
        open_interest=oi,
    )


def _expiry(
    exp: dt.date, oi_each: int | None, *, bid: float = 1.0, ask: float = 1.1
) -> list[OptionContract]:
    """Strikes 90..110 by 5 around spot 100, calls + puts, *oi_each* per contract."""
    return [
        _c(exp, k, r, oi_each, bid=bid, ask=ask)
        for k in (90.0, 95.0, 100.0, 105.0, 110.0)
        for r in ("call", "put")
    ]


class _FakeMarket:
    def __init__(self, chain: list[OptionContract], price: float = 100.0) -> None:
        self.chain = chain
        self.price = price

    def underlying_quote(self, symbol: str) -> Any:
        return SimpleNamespace(bid=self.price - 0.01, ask=self.price + 0.01)

    def history_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[Any]:
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        return [
            SimpleNamespace(
                timestamp=dt.datetime.combine(d, dt.time(16)), volume=5e6, close=self.price
            )
            for d in days
        ]

    def option_chain(self, symbol: str, lo: dt.date, hi: dt.date) -> list[OptionContract]:
        return self.chain


def _measure(chain: list[OptionContract]) -> Any:
    return measure_liquidity(
        _FakeMarket(chain),
        "X",
        today=TODAY,
        dte_window=(21, 60),
        thresholds=T,  # type: ignore[arg-type]
    )


class TestPickHelper:
    def test_monthly_beats_nearer_weekly(self) -> None:
        # the ADBE shape on 2026-10-16: Nov 13 weekly thin, Nov 20 monthly holds the OI
        chain = (
            _expiry(WEEKLY_NOV6, 90)
            + _expiry(WEEKLY_NOV13, 25)
            + _expiry(MONTHLY_NOV20, 1_000)
            + _expiry(WEEKLY_NOV27, 14)
        )
        assert pick_liquidity_expiry(chain, 100.0, 3, today=TODAY) == MONTHLY_NOV20

    def test_only_near_atm_strikes_count(self) -> None:
        # Nov 13 has huge far-OTM OI but nothing near spot -> the monthly still wins
        chain = (
            _expiry(WEEKLY_NOV13, 0)
            + [_c(WEEKLY_NOV13, 150.0, "call", 1_000_000)]
            + _expiry(MONTHLY_NOV20, 10)
        )
        assert pick_liquidity_expiry(chain, 100.0, 3, today=TODAY) == MONTHLY_NOV20

    def test_tie_goes_nearest_30_then_earlier(self) -> None:
        # equal OI: Nov 13 (|28-30|=2) beats Nov 20 (|35-30|=5)
        chain = _expiry(WEEKLY_NOV13, 50) + _expiry(MONTHLY_NOV20, 50)
        assert pick_liquidity_expiry(chain, 100.0, 3, today=TODAY) == WEEKLY_NOV13
        # equal OI and equal distance from 30 DTE -> the earlier date
        d28, d32 = TODAY + dt.timedelta(days=28), TODAY + dt.timedelta(days=32)
        rows = [(d32, 100.0, 7), (d28, 100.0, 7)]
        assert pick_expiry_by_atm_oi(rows, 100.0, 3, today=TODAY) == d28

    def test_all_oi_unknown_falls_back_to_nearest_30(self) -> None:
        chain = (
            _expiry(WEEKLY_NOV6, None) + _expiry(WEEKLY_NOV13, None) + _expiry(MONTHLY_NOV20, None)
        )
        assert pick_liquidity_expiry(chain, 100.0, 3, today=TODAY) == WEEKLY_NOV13

    def test_unknown_oi_rows_add_nothing(self) -> None:
        # a partly-known expiry with 1 contract of OI beats a fully-unknown one
        chain = _expiry(WEEKLY_NOV13, None) + [_c(MONTHLY_NOV20, 100.0, "call", 1)]
        assert pick_liquidity_expiry(chain, 100.0, 3, today=TODAY) == MONTHLY_NOV20

    def test_empty_or_no_price(self) -> None:
        assert pick_liquidity_expiry([], 100.0, 3, today=TODAY) is None
        assert pick_liquidity_expiry(_expiry(WEEKLY_NOV13, 5), None, 3, today=TODAY) is None
        assert pick_expiry_by_atm_oi([], 100.0, 3, today=TODAY) is None


class TestMeasure:
    def test_measures_the_monthly_and_passes(self) -> None:
        chain = _expiry(WEEKLY_NOV13, 3) + _expiry(MONTHLY_NOV20, 200)
        m = _measure(chain)
        assert m.error is None and m.expiries_in_window == 2
        assert m.atm_expiry == MONTHLY_NOV20 and m.atm_strike == 100.0
        assert m.atm_open_interest == 3 * 2 * 200  # 3 strikes x call/put
        assert screen_liquidity(m, T).passed

    def test_spread_read_from_chosen_expiry(self) -> None:
        # weekly: tight 2 % but thin; monthly: liquid with a 50 % spread
        chain = _expiry(WEEKLY_NOV13, 3, bid=1.0, ask=1.02) + _expiry(
            MONTHLY_NOV20, 200, bid=1.0, ask=1.67
        )
        m = _measure(chain)
        assert m.atm_expiry == MONTHLY_NOV20
        assert abs(m.atm_spread_pct - 0.67 / 1.335) < 1e-9
        res = screen_liquidity(m, T)
        assert not res.passed and "ATM spread" in res.detail()
        assert "near-ATM OI" not in res.detail()

    def test_all_oi_unknown_fails_as_unknown(self) -> None:
        chain = _expiry(WEEKLY_NOV13, None) + _expiry(MONTHLY_NOV20, None)
        m = _measure(chain)
        assert m.atm_expiry == WEEKLY_NOV13 and m.atm_open_interest is None
        res = screen_liquidity(m, T)
        assert not res.passed and "near-ATM OI unknown" in res.detail()

    def test_out_of_window_expiry_ignored(self) -> None:
        # a huge 10-DTE expiry outside the (21, 60) window is never picked
        near = TODAY + dt.timedelta(days=10)
        chain = _expiry(near, 99_999) + _expiry(WEEKLY_NOV13, 40)
        m = _measure(chain)
        assert m.atm_expiry == WEEKLY_NOV13 and m.expiries_in_window == 1


def test_backtest_screen_uses_the_same_expiry_pick() -> None:
    """``day_liquidity_metrics`` (backtest D9 flag) follows the live E12.4a rule."""
    import pandas as pd

    from arc.backtest.data import day_liquidity_metrics

    rows = [
        {
            "date": TODAY,
            "expiration": c.expiration,
            "strike": c.strike,
            "open_interest": c.open_interest,
            "bid": c.bid,
            "ask": c.ask,
        }
        for c in _expiry(WEEKLY_NOV13, 3) + _expiry(MONTHLY_NOV20, 200, bid=1.0, ask=1.05)
    ]
    days = pd.Index([TODAY - dt.timedelta(days=i) for i in range(25)][::-1])
    m = day_liquidity_metrics(
        "X",
        TODAY,
        pd.DataFrame(rows),
        closes=pd.Series(100.0, index=days),
        volume=pd.Series(5e6, index=days),
        thresholds=T,
        dte_window=(21, 60),
    )
    assert m.atm_expiry == MONTHLY_NOV20 and m.atm_open_interest == 1_200
    assert abs(m.atm_spread_pct - 0.05 / 1.025) < 1e-9
    assert screen_liquidity(m, T).passed
