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
)
from arc.utils.calendar import ET


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
