"""E4.12 (D55): market_spot never returns a one-sided half-price mid."""

from __future__ import annotations

import datetime as dt

import pytest

from arc.data.base import HistoryBar, Spot, UnderlyingQuote, market_spot
from arc.utils.calendar import ET

TODAY = dt.date(2026, 10, 5)


class _P:
    def __init__(self, bid: float, ask: float, close: float | None, bar_day: dt.date) -> None:
        self.bid, self.ask, self.close, self.bar_day = bid, ask, close, bar_day
        self.quote_calls = 0
        self.bar_calls = 0

    def option_chain(self, underlying, exp_start, exp_end):  # noqa: ANN001, ANN201
        return []

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        self.quote_calls += 1
        ts = dt.datetime(2026, 10, 5, 18, tzinfo=ET)
        mid = (self.bid + self.ask) / 2
        return UnderlyingQuote(symbol=symbol, bid=self.bid, ask=self.ask, mid=mid, timestamp=ts)

    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        self.bar_calls += 1
        if self.close is None:
            return []
        # Alpaca daily bars are stamped at 04:00/05:00 UTC = midnight ET of the session.
        ts = dt.datetime.combine(self.bar_day, dt.time(4), tzinfo=dt.UTC)
        c = self.close
        return [HistoryBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=1.0)]


def test_ask_zero_gives_last_close() -> None:
    # TSLA after hours on 2026-10-05: bid 342.61 / ask 0 -> raw mid 171.31.
    p = _P(342.61, 0.0, 379.0, TODAY)
    assert market_spot(p, "TSLA", TODAY) == Spot(379.0, "last_close")


def test_ask_zero_uses_an_older_close_too() -> None:
    p = _P(342.61, 0.0, 377.0, dt.date(2026, 10, 2))
    assert market_spot(p, "TSLA", TODAY) == Spot(377.0, "last_close")


def test_tight_two_sided_gives_mid_without_bars() -> None:
    p = _P(99.95, 100.05, 50.0, TODAY)
    assert market_spot(p, "X", TODAY) == Spot(100.0, "mid")
    assert p.bar_calls == 0


def test_wide_quote_with_todays_bar_gives_close() -> None:
    # AAPL 316/350 after hours: spread 10% of mid.
    p = _P(316.0, 350.0, 333.79, TODAY)
    assert market_spot(p, "AAPL", TODAY) == Spot(333.79, "last_close")


def test_wide_quote_with_only_an_old_bar_keeps_mid() -> None:
    p = _P(316.0, 350.0, 320.0, dt.date(2026, 10, 2))
    assert market_spot(p, "AAPL", TODAY) == Spot(333.0, "mid")


def test_wide_quote_no_bar_keeps_mid() -> None:
    assert market_spot(_P(90.0, 110.0, None, TODAY), "X", TODAY) == Spot(100.0, "mid")


def test_spread_threshold_is_configurable() -> None:
    p = _P(316.0, 350.0, 333.79, TODAY)
    assert market_spot(p, "AAPL", TODAY, max_spread_pct=0.2) == Spot(333.0, "mid")


def test_no_quote_no_close_is_none() -> None:
    assert market_spot(_P(0.0, 0.0, None, TODAY), "X", TODAY) == Spot(None, None)
    assert market_spot(_P(5.0, 0.0, 0.0, TODAY), "X", TODAY) == Spot(None, None)


def test_passed_quote_is_not_refetched() -> None:
    p = _P(99.95, 100.05, None, TODAY)
    q = p.underlying_quote("X")
    assert market_spot(p, "X", TODAY, quote=q).price == pytest.approx(100.0)
    assert p.quote_calls == 1


def test_naive_bar_timestamp() -> None:
    p = _P(342.61, 0.0, 379.0, TODAY)

    def bars(symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN202
        ts = dt.datetime(2026, 10, 5)
        return [HistoryBar(timestamp=ts, open=1, high=1, low=1, close=379.0, volume=1.0)]

    p.history_bars = bars  # type: ignore[method-assign]
    p.bid, p.ask = 300.0, 400.0  # wide two-sided: today's close wins
    assert market_spot(p, "X", TODAY) == Spot(379.0, "last_close")
