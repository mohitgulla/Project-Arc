"""Unit tests for alpaca data adapter — mocked, no network."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models.bars import BarSet
from structlog.testing import capture_logs

from arc.config import AlpacaOptionsFeed, ArcSettings
from arc.data.alpaca import (
    AlpacaMarketData,
    _check_quality,
    _parse_occ_symbol,
    _parse_timeframe,
    sip_window_end,
)
from arc.data.base import OptionGreeks
from arc.utils.calendar import ET

# Fixed clock (E1.1b): quote timestamps and the adapter's ``now_et`` both read this.
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _frozen_adapter_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter stamps quote age with ``now_et()``; pin it so staleness is deterministic."""
    monkeypatch.setattr("arc.data.alpaca.now_et", lambda: NOW)


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
            quote_ts=NOW,
            now=NOW,
        )
        issues = [f.issue for f in flags]
        assert "zero_bid" in issues

    def test_missing_greeks_none(self) -> None:
        flags = _check_quality(
            symbol="TEST",
            bid=1.0,
            greeks=None,
            quote_ts=NOW,
            now=NOW,
        )
        issues = [f.issue for f in flags]
        assert "missing_greeks" in issues

    def test_missing_greeks_no_delta(self) -> None:
        flags = _check_quality(
            symbol="TEST",
            bid=1.0,
            greeks=OptionGreeks(delta=None),
            quote_ts=NOW,
            now=NOW,
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
    def test_history_bars_real_barset(self) -> None:
        """Regression: ``symbol in BarSet`` is always False (pydantic __iter__)."""
        raw = {
            "SPY": [
                {
                    "t": "2026-09-25T04:00:00Z",
                    "o": 650.0,
                    "h": 655.0,
                    "l": 648.0,
                    "c": 653.0,
                    "v": 1_000_000,
                    "n": 5000,
                    "vw": 652.0,
                }
            ]
        }
        mock_stock = MagicMock()
        mock_stock.get_stock_bars.return_value = BarSet(raw)

        adapter = AlpacaMarketData(option_client=MagicMock(), stock_client=mock_stock)
        bars = adapter.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 10, 1))

        assert len(bars) == 1
        assert bars[0].close == 653.0
        assert adapter.history_bars("QQQ", dt.date(2026, 9, 1), dt.date(2026, 10, 1)) == []

    @patch.dict("os.environ", {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})
    def test_option_chain_with_quality_flags(self) -> None:
        mock_option = MagicMock()

        # Build a mock snapshot with zero bid and no greeks
        mock_snap = MagicMock()
        mock_snap.latest_quote = MagicMock()
        mock_snap.latest_quote.bid_price = 0.0
        mock_snap.latest_quote.ask_price = 0.05
        mock_snap.latest_quote.timestamp = NOW
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
        mock_snap.latest_quote.timestamp = NOW
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


# ---------------------------------------------------------------------------
# Data feed selection (free tier: iex stocks / indicative options)
# ---------------------------------------------------------------------------

_KEYS = {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}


def _stock_client() -> MagicMock:
    mock_stock = MagicMock()
    q = MagicMock(bid_price=1.0, ask_price=1.1, timestamp=dt.datetime(2026, 10, 1, tzinfo=ET))
    mock_stock.get_stock_latest_quote.return_value = {"SPY": q}
    mock_stock.get_stock_bars.return_value = {}
    return mock_stock


class TestDataFeed:
    @patch.dict("os.environ", _KEYS)
    def test_defaults_iex_and_indicative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_ALPACA_DATA_FEED", raising=False)
        monkeypatch.delenv("ARC_ALPACA_OPTIONS_FEED", raising=False)
        monkeypatch.setattr(
            "arc.config.ArcSettings.model_config", {**ArcSettings.model_config, "env_file": None}
        )
        mock_stock = _stock_client()
        mock_option = MagicMock()
        mock_option.get_option_chain.return_value = {}
        adapter = AlpacaMarketData(option_client=mock_option, stock_client=mock_stock)

        adapter.underlying_quote("SPY")
        adapter.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 10, 1))
        adapter.option_chain("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 30))

        assert mock_stock.get_stock_latest_quote.call_args.args[0].feed == DataFeed.IEX
        assert mock_stock.get_stock_bars.call_args.args[0].feed == DataFeed.IEX
        assert mock_option.get_option_chain.call_args.args[0].feed == OptionsFeed.INDICATIVE
        # Serialized request carries the feed param.
        bars_req = mock_stock.get_stock_bars.call_args.args[0]
        assert bars_req.to_request_fields()["feed"] == "iex"

    @patch.dict(
        "os.environ",
        {**_KEYS, "ARC_ALPACA_DATA_FEED": "sip", "ARC_ALPACA_OPTIONS_FEED": "opra"},
    )
    def test_env_override(self) -> None:
        mock_stock = _stock_client()
        mock_option = MagicMock()
        mock_option.get_option_chain.return_value = {}
        adapter = AlpacaMarketData(option_client=mock_option, stock_client=mock_stock)

        adapter.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 10, 1))
        adapter.option_chain("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 30))

        assert mock_stock.get_stock_bars.call_args.args[0].feed == DataFeed.SIP
        assert mock_option.get_option_chain.call_args.args[0].feed == OptionsFeed.OPRA

    @patch.dict("os.environ", _KEYS)
    def test_bars_adjustment(self) -> None:
        """E28.4: the betas job asks for adjusted bars; the default sends no adjustment."""
        day = (dt.date(2026, 9, 1), dt.date(2026, 10, 1))
        plain_stock, adj_stock = _stock_client(), _stock_client()
        opt = MagicMock()
        AlpacaMarketData(
            option_client=opt, stock_client=plain_stock, data_feed="iex", options_feed="indicative"
        ).history_bars("SPY", *day)
        assert "adjustment" not in plain_stock.get_stock_bars.call_args.args[0].to_request_fields()
        AlpacaMarketData(
            option_client=opt,
            stock_client=adj_stock,
            data_feed="iex",
            options_feed="indicative",
            bars_adjustment="all",
        ).history_bars("SPY", *day)
        assert adj_stock.get_stock_bars.call_args.args[0].to_request_fields()["adjustment"] == "all"

    @patch.dict("os.environ", _KEYS)
    def test_explicit_args(self) -> None:
        mock_stock = _stock_client()
        adapter = AlpacaMarketData(
            option_client=MagicMock(),
            stock_client=mock_stock,
            data_feed="delayed_sip",
            options_feed=AlpacaOptionsFeed.INDICATIVE,
        )
        adapter.underlying_quote("SPY")
        assert mock_stock.get_stock_latest_quote.call_args.args[0].feed == DataFeed.DELAYED_SIP

    @patch.dict("os.environ", _KEYS)
    def test_invalid_feed_rejected(self) -> None:
        with pytest.raises(ValueError):
            AlpacaMarketData(
                option_client=MagicMock(),
                stock_client=MagicMock(),
                data_feed="bogus",
                options_feed="indicative",
            )


# ---------------------------------------------------------------------------
# E23.0a (D98): per-call history feed, SIP window clamp, SIP refusal fallback
# ---------------------------------------------------------------------------


def _sip_refusal() -> APIError:
    resp = MagicMock(status_code=403)
    http = MagicMock(response=resp)
    return APIError('{"message":"subscription does not permit querying recent SIP data"}', http)


def _utc(t: dt.datetime) -> dt.datetime:
    """The request model stores ``start``/``end`` as naive UTC."""
    return t.astimezone(dt.UTC).replace(tzinfo=None)


class TestHistoryFeed:
    NOW = dt.datetime(2026, 10, 9, 11, 0, tzinfo=ET)

    def _adapter(self, stock: MagicMock) -> AlpacaMarketData:
        return AlpacaMarketData(
            option_client=MagicMock(),
            stock_client=stock,
            data_feed="iex",
            options_feed="indicative",
            api_key="k",
            secret_key="s",
        )

    def test_sip_window_end_is_16_minutes_back(self) -> None:
        assert sip_window_end(self.NOW) == dt.datetime(2026, 10, 9, 10, 44, tzinfo=ET)

    def test_feed_reaches_the_bars_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arc.data.alpaca.now_et", lambda: self.NOW)
        stock = _stock_client()
        adapter = self._adapter(stock)
        day = self.NOW.date()
        adapter.history_bars("SPY", day, day, "5Min")  # default: the configured feed
        req = stock.get_stock_bars.call_args.args[0]
        assert req.feed == DataFeed.IEX and req.end == _utc(
            dt.datetime.combine(day, dt.time.max, ET)
        )
        adapter.history_bars("SPY", day, day, "5Min", feed="sip")
        req = stock.get_stock_bars.call_args.args[0]
        assert req.feed == DataFeed.SIP
        assert req.end == _utc(sip_window_end(self.NOW))  # clamped: free tier, >= 15 min old
        assert req.to_request_fields()["feed"] == "sip"
        # a past window is not clamped
        past = dt.date(2026, 10, 1)
        adapter.history_bars("SPY", past - dt.timedelta(days=30), past, feed="sip")
        req = stock.get_stock_bars.call_args.args[0]
        assert req.end == _utc(dt.datetime.combine(past, dt.time.max, ET))
        adapter.history_bars("SPY", day, day, feed="iex")
        assert stock.get_stock_bars.call_args.args[0].feed == DataFeed.IEX

    def test_sip_window_before_the_start_never_inverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        early = dt.datetime(2026, 10, 9, 0, 5, tzinfo=ET)  # now - 16 min is yesterday
        monkeypatch.setattr("arc.data.alpaca.now_et", lambda: early)
        stock = _stock_client()
        self._adapter(stock).history_bars("SPY", early.date(), early.date(), feed="sip")
        req = stock.get_stock_bars.call_args.args[0]
        assert req.end >= req.start

    def test_sip_refusal_falls_back_to_iex_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arc.data.alpaca.now_et", lambda: self.NOW)
        stock = _stock_client()
        bar = MagicMock(
            timestamp=self.NOW, open=1.0, high=1.0, low=1.0, close=1.0, volume=5.0,
            trade_count=1.0, vwap=1.0,
        )  # fmt: skip
        stock.get_stock_bars.side_effect = [_sip_refusal(), {"SPY": [bar]}]
        day = self.NOW.date()
        with capture_logs() as logs:
            bars = self._adapter(stock).history_bars("SPY", day, day, "5Min", feed="sip")
        assert [b.volume for b in bars] == [5.0]
        feeds = [c.args[0].feed for c in stock.get_stock_bars.call_args_list]
        assert feeds == [DataFeed.SIP, DataFeed.IEX]
        assert any(e["event"] == "feed_fallback" and e["used"] == "iex" for e in logs)

    def test_other_errors_are_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arc.data.alpaca.now_et", lambda: self.NOW)
        stock = _stock_client()
        resp = MagicMock(status_code=500)
        stock.get_stock_bars.side_effect = APIError('{"message":"boom"}', MagicMock(response=resp))
        day = self.NOW.date()
        with pytest.raises(APIError):
            self._adapter(stock).history_bars("SPY", day, day, feed="sip")
        # an IEX refusal is never retried either (nothing to fall back to)
        stock.get_stock_bars.side_effect = _sip_refusal()
        with pytest.raises(APIError):
            self._adapter(stock).history_bars("SPY", day, day, feed="iex")
        assert stock.get_stock_bars.call_count == 2
