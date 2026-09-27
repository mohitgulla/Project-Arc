"""Unit tests for alpaca data adapter — mocked, no network."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from arc.data.alpaca import (
    AlpacaMarketData,
    _check_quality,
    _parse_occ_symbol,
    _parse_timeframe,
)
from arc.data.base import OptionGreeks
from arc.utils.calendar import ET

# ---------------------------------------------------------------------------
# OCC symbol parser
# ---------------------------------------------------------------------------


class TestParseOccSymbol:
    def test_standard_call(self) -> None:
        result = _parse_occ_symbol("SPY261016C00450000")
        assert result["underlying"] == "SPY"
        assert result["expiration"] == dt.date(2026, 10, 16)
        assert result["option_type"] == "call"
        assert result["strike"] == 450.0

    def test_standard_put(self) -> None:
        result = _parse_occ_symbol("AAPL261016P00150000")
        assert result["underlying"] == "AAPL"
        assert result["option_type"] == "put"
        assert result["strike"] == 150.0

    def test_short_symbol(self) -> None:
        result = _parse_occ_symbol("X261016C00025000")
        assert result["underlying"] == "X"
        assert result["strike"] == 25.0

    def test_too_short(self) -> None:
        result = _parse_occ_symbol("SHORT")
        assert result["underlying"] == "SHORT"
        assert result["expiration"] is None


# ---------------------------------------------------------------------------
# Timeframe parsing
# ---------------------------------------------------------------------------


class TestParseTimeframe:
    def test_valid_timeframes(self) -> None:
        for tf in ("1Min", "5min", "15Min", "1Hour", "1Day", "1Week", "1Month"):
            result = _parse_timeframe(tf)
            assert result is not None

    def test_invalid_timeframe(self) -> None:
        with pytest.raises(ValueError, match="Unknown timeframe"):
            _parse_timeframe("3Day")


# ---------------------------------------------------------------------------
# Data quality checks
# ---------------------------------------------------------------------------


class TestCheckQuality:
    def test_zero_bid(self) -> None:
        flags = _check_quality(
            symbol="TEST",
            bid=0.0,
            greeks=OptionGreeks(delta=0.5),
            quote_ts=dt.datetime.now(tz=ET),
            now=dt.datetime.now(tz=ET),
        )
        issues = [f.issue for f in flags]
        assert "zero_bid" in issues

    def test_missing_greeks_none(self) -> None:
        flags = _check_quality(
            symbol="TEST",
            bid=1.0,
            greeks=None,
            quote_ts=dt.datetime.now(tz=ET),
            now=dt.datetime.now(tz=ET),
        )
        issues = [f.issue for f in flags]
        assert "missing_greeks" in issues

    def test_missing_greeks_no_delta(self) -> None:
        flags = _check_quality(
            symbol="TEST",
            bid=1.0,
            greeks=OptionGreeks(delta=None),
            quote_ts=dt.datetime.now(tz=ET),
            now=dt.datetime.now(tz=ET),
        )
        issues = [f.issue for f in flags]
        assert "missing_greeks" in issues

    def test_stale_timestamp(self) -> None:
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=ET)
        stale_ts = dt.datetime(2026, 10, 1, 11, 30, 0, tzinfo=ET)  # 30 min ago
        flags = _check_quality(
            symbol="TEST",
            bid=1.0,
            greeks=OptionGreeks(delta=0.5),
            quote_ts=stale_ts,
            now=now,
        )
        issues = [f.issue for f in flags]
        assert "stale_timestamp" in issues

    def test_clean_contract(self) -> None:
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=ET)
        flags = _check_quality(
            symbol="TEST",
            bid=1.50,
            greeks=OptionGreeks(delta=0.45, gamma=0.02),
            quote_ts=now - dt.timedelta(seconds=60),
            now=now,
        )
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# AlpacaMarketData with mocked clients
# ---------------------------------------------------------------------------


class TestAlpacaMarketDataMocked:
    """Tests with mocked alpaca-py clients — no network calls."""

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_underlying_quote(self) -> None:
        mock_stock = MagicMock()
        mock_quote = MagicMock()
        mock_quote.bid_price = 450.10
        mock_quote.ask_price = 450.20
        mock_quote.timestamp = dt.datetime(2026, 10, 1, 10, 30, tzinfo=ET)
        mock_stock.get_stock_latest_quote.return_value = {"SPY": mock_quote}

        adapter = AlpacaMarketData(option_client=MagicMock(), stock_client=mock_stock)
        result = adapter.underlying_quote("SPY")

        assert result.symbol == "SPY"
        assert result.bid == 450.10
        assert result.ask == 450.20
        assert abs(result.mid - 450.15) < 0.001

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_underlying_quote_not_found(self) -> None:
        mock_stock = MagicMock()
        mock_stock.get_stock_latest_quote.return_value = {}

        adapter = AlpacaMarketData(option_client=MagicMock(), stock_client=mock_stock)
        with pytest.raises(ValueError, match="No quote returned"):
            adapter.underlying_quote("NOSYMBOL")

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_history_bars(self) -> None:
        mock_stock = MagicMock()
        mock_bar = MagicMock()
        mock_bar.timestamp = dt.datetime(2026, 10, 1, tzinfo=ET)
        mock_bar.open = 450.0
        mock_bar.high = 452.0
        mock_bar.low = 449.0
        mock_bar.close = 451.5
        mock_bar.volume = 1_000_000.0
        mock_bar.trade_count = 5000.0
        mock_bar.vwap = 451.0
        mock_stock.get_stock_bars.return_value = {"SPY": [mock_bar]}

        adapter = AlpacaMarketData(option_client=MagicMock(), stock_client=mock_stock)
        bars = adapter.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 10, 1))

        assert len(bars) == 1
        assert bars[0].close == 451.5
        assert bars[0].vwap == 451.0

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_option_chain_with_quality_flags(self) -> None:
        mock_option = MagicMock()

        # Build a mock snapshot with zero bid and no greeks
        mock_snap = MagicMock()
        mock_snap.latest_quote = MagicMock()
        mock_snap.latest_quote.bid_price = 0.0
        mock_snap.latest_quote.ask_price = 0.05
        mock_snap.latest_quote.timestamp = dt.datetime.now(tz=ET)
        mock_snap.latest_trade = None
        mock_snap.greeks = None
        mock_snap.implied_volatility = None

        mock_option.get_option_chain.return_value = {"SPY261016C00450000": mock_snap}

        adapter = AlpacaMarketData(option_client=mock_option, stock_client=MagicMock())
        contracts = adapter.option_chain("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 30))

        assert len(contracts) == 1
        c = contracts[0]
        assert c.is_flagged
        issues = {f.issue for f in c.quality_flags}
        assert "zero_bid" in issues
        assert "missing_greeks" in issues

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_option_chain_clean(self) -> None:
        mock_option = MagicMock()

        mock_greeks = MagicMock()
        mock_greeks.delta = 0.45
        mock_greeks.gamma = 0.03
        mock_greeks.theta = -0.05
        mock_greeks.vega = 0.15
        mock_greeks.rho = 0.01

        mock_snap = MagicMock()
        mock_snap.latest_quote = MagicMock()
        mock_snap.latest_quote.bid_price = 3.50
        mock_snap.latest_quote.ask_price = 3.80
        mock_snap.latest_quote.timestamp = dt.datetime.now(tz=ET)
        mock_snap.latest_trade = MagicMock()
        mock_snap.latest_trade.price = 3.65
        mock_snap.greeks = mock_greeks
        mock_snap.implied_volatility = 0.25

        mock_option.get_option_chain.return_value = {"SPY261016C00450000": mock_snap}

        adapter = AlpacaMarketData(option_client=mock_option, stock_client=MagicMock())
        contracts = adapter.option_chain("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 30))

        assert len(contracts) == 1
        c = contracts[0]
        assert not c.is_flagged
        assert c.underlying == "SPY"
        assert c.strike == 450.0
        assert c.option_type == "call"
        assert c.greeks is not None
        assert c.greeks.delta == 0.45
        assert c.implied_volatility == 0.25
        assert abs((c.mid or 0) - 3.65) < 0.001
