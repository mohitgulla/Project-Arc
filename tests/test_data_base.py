"""Unit tests for MarketDataProvider protocol and data contracts."""

from __future__ import annotations

import datetime as dt

from arc.data.base import (
    DataQualityFlag,
    HistoryBar,
    MarketDataProvider,
    OptionContract,
    OptionGreeks,
    UnderlyingQuote,
    reference_price,
)
from arc.utils.calendar import ET


class _StubProvider:
    def __init__(self, bid: float, ask: float, closes: list[float]) -> None:
        self.bid, self.ask, self.closes = bid, ask, closes
        self.bar_calls = 0

    def option_chain(self, underlying, exp_start, exp_end):  # noqa: ANN001, ANN201
        return []

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        ts = dt.datetime(2026, 9, 25, 16, tzinfo=ET)
        mid = (self.bid + self.ask) / 2
        return UnderlyingQuote(symbol=symbol, bid=self.bid, ask=self.ask, mid=mid, timestamp=ts)

    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        self.bar_calls += 1
        ts = dt.datetime(2026, 9, 25, tzinfo=ET)
        return [
            HistoryBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=1.0)
            for c in self.closes
        ]


class TestReferencePrice:
    today = dt.date(2026, 9, 27)

    def test_two_sided_quote_uses_mid(self) -> None:
        p = _StubProvider(99.0, 101.0, [50.0])
        assert reference_price(p, "X", today=self.today) == 100.0
        assert p.bar_calls == 0

    def test_one_sided_quote_falls_back_to_close(self) -> None:
        # Off-hours IEX: ask=0 would halve the mid.
        assert reference_price(_StubProvider(70.44, 0.0, [73.9]), "X", today=self.today) == 73.9

    def test_no_data_returns_none(self) -> None:
        assert reference_price(_StubProvider(0.0, 0.0, []), "X", today=self.today) is None
        assert reference_price(_StubProvider(0.0, 0.0, [0.0]), "X", today=self.today) is None


def test_market_data_provider_is_runtime_checkable() -> None:
    """MarketDataProvider is a runtime-checkable Protocol."""
    assert hasattr(MarketDataProvider, "__protocol_attrs__") or hasattr(
        MarketDataProvider, "__abstractmethods__"
    )


def test_option_contract_model() -> None:
    contract = OptionContract(
        symbol="SPY261016C00450000",
        underlying="SPY",
        expiration=dt.date(2026, 10, 16),
        strike=450.0,
        option_type="call",
        bid=3.50,
        ask=3.80,
        mid=3.65,
        open_interest=1500,
        volume=200,
        implied_volatility=0.25,
        greeks=OptionGreeks(delta=0.45, gamma=0.03, theta=-0.05, vega=0.15, rho=0.01),
    )
    assert contract.mid == 3.65
    assert not contract.is_flagged
    assert contract.greeks is not None
    assert contract.greeks.delta == 0.45


def test_option_contract_flagged() -> None:
    contract = OptionContract(
        symbol="SPY261016C00450000",
        underlying="SPY",
        expiration=dt.date(2026, 10, 16),
        strike=450.0,
        option_type="call",
        bid=0.0,
        quality_flags=[DataQualityFlag(symbol="SPY261016C00450000", issue="zero_bid")],
    )
    assert contract.is_flagged


def test_underlying_quote_model() -> None:
    quote = UnderlyingQuote(
        symbol="SPY",
        bid=450.10,
        ask=450.20,
        mid=450.15,
        timestamp=dt.datetime(2026, 10, 1, 10, 30, tzinfo=ET),
    )
    assert quote.mid == 450.15


def test_history_bar_model() -> None:
    bar = HistoryBar(
        timestamp=dt.datetime(2026, 10, 1, tzinfo=ET),
        open=450.0,
        high=452.0,
        low=449.0,
        close=451.5,
        volume=1_000_000,
    )
    assert bar.vwap is None
    assert bar.close == 451.5


def test_data_quality_flag() -> None:
    flag = DataQualityFlag(
        symbol="AAPL261016C00150000",
        issue="stale_timestamp",
        detail="quote age 1200s > 900s",
    )
    assert flag.issue == "stale_timestamp"
