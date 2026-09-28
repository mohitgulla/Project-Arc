"""Alpaca MarketDataProvider implementation.

Uses ``alpaca-py`` data clients for option chains (via snapshots endpoint),
underlying quotes, and history bars.

Reads ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` from environment.
Data-quality checks: stale timestamps, missing greeks, zero bid are flagged.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import structlog
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from arc.config import AlpacaDataFeed, AlpacaOptionsFeed, get_settings
from arc.data.base import (
    DataQualityFlag,
    HistoryBar,
    OptionContract,
    OptionGreeks,
    UnderlyingQuote,
)
from arc.utils.calendar import ET, now_et

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Staleness threshold
# ---------------------------------------------------------------------------

_STALE_SECONDS = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Timeframe parsing
# ---------------------------------------------------------------------------

_TF_MAP: dict[str, TimeFrame] = {
    "1min": TimeFrame(1, TimeFrameUnit.Minute),
    "5min": TimeFrame(5, TimeFrameUnit.Minute),
    "15min": TimeFrame(15, TimeFrameUnit.Minute),
    "1hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1day": TimeFrame(1, TimeFrameUnit.Day),
    "1week": TimeFrame(1, TimeFrameUnit.Week),
    "1month": TimeFrame(1, TimeFrameUnit.Month),
}


def _parse_timeframe(tf: str) -> TimeFrame:
    """Parse a human-readable timeframe string into an alpaca TimeFrame."""
    key = tf.lower().replace(" ", "")
    result = _TF_MAP.get(key)
    if result is None:
        msg = f"Unknown timeframe {tf!r}. Valid: {sorted(_TF_MAP.keys())}"
        raise ValueError(msg)
    return result


# ---------------------------------------------------------------------------
# Data-quality checks
# ---------------------------------------------------------------------------


def _check_quality(
    symbol: str,
    bid: float | None,
    greeks: OptionGreeks | None,
    quote_ts: dt.datetime | None,
    now: dt.datetime,
) -> list[DataQualityFlag]:
    """Run data-quality checks on a single option contract snapshot."""
    flags: list[DataQualityFlag] = []

    # Zero bid
    if bid is not None and bid == 0.0:
        flags.append(DataQualityFlag(symbol=symbol, issue="zero_bid", detail="bid=0"))

    # Missing greeks
    if greeks is None or greeks.delta is None:
        flags.append(
            DataQualityFlag(
                symbol=symbol,
                issue="missing_greeks",
                detail="greeks absent or delta=None",
            )
        )

    # Stale timestamp
    if quote_ts is not None:
        # Ensure both are tz-aware for comparison
        if quote_ts.tzinfo is None:
            quote_ts = quote_ts.replace(tzinfo=ET)
        age = (now - quote_ts).total_seconds()
        if age > _STALE_SECONDS:
            flags.append(
                DataQualityFlag(
                    symbol=symbol,
                    issue="stale_timestamp",
                    detail=f"quote age {age:.0f}s > {_STALE_SECONDS}s",
                )
            )

    return flags


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_keys() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        msg = (
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the "
            "environment (see ~/.hermes/.env)."
        )
        raise RuntimeError(msg)
    return api_key, secret_key


def _parse_occ_symbol(symbol: str) -> dict[str, Any]:
    """Parse an OCC option symbol into components.

    OCC format: AAPL261016C00150000
      - underlying: AAPL (variable length, up to 6 chars)
      - date: 261016 (YYMMDD)
      - type: C or P
      - strike: 00150000 (strike * 1000, 8 digits)
    """
    # Find where the date part starts (6 digits before C/P and 8-digit strike)
    # The symbol ends with C/PXXXXXXXX (1 + 8 = 9 chars for type+strike)
    # Before that is 6 chars of date YYMMDD
    # Everything before that is the underlying
    if len(symbol) < 16:
        return {"underlying": symbol, "expiration": None, "option_type": "unknown", "strike": 0.0}

    strike_str = symbol[-8:]
    option_type_char = symbol[-9]
    date_str = symbol[-15:-9]
    underlying = symbol[:-15]

    try:
        exp = dt.date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
    except (ValueError, IndexError):
        exp = None

    return {
        "underlying": underlying,
        "expiration": exp,
        "option_type": "call" if option_type_char == "C" else "put",
        "strike": int(strike_str) / 1000.0,
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AlpacaMarketData:
    """MarketDataProvider backed by Alpaca's data API.

    Uses the option chain snapshots endpoint for chains with greeks,
    stock data endpoints for underlying quotes and bars.
    """

    def __init__(
        self,
        option_client: OptionHistoricalDataClient | None = None,
        stock_client: StockHistoricalDataClient | None = None,
        data_feed: AlpacaDataFeed | str | None = None,
        options_feed: AlpacaOptionsFeed | str | None = None,
    ) -> None:
        api_key, secret_key = _get_keys()
        if data_feed is None or options_feed is None:
            settings = get_settings()
            data_feed = data_feed or settings.alpaca_data_feed
            options_feed = options_feed or settings.alpaca_options_feed
        # Free/paper tier: stock requests without a feed default to SIP → 403.
        self._data_feed = DataFeed(AlpacaDataFeed(data_feed).value)
        self._options_feed = OptionsFeed(AlpacaOptionsFeed(options_feed).value)
        self._option_client = option_client or OptionHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )
        self._stock_client = stock_client or StockHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )

    # -- option_chain --------------------------------------------------------

    def option_chain(
        self,
        underlying: str,
        exp_start: dt.date,
        exp_end: dt.date,
    ) -> list[OptionContract]:
        req = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=exp_start.isoformat(),
            expiration_date_lte=exp_end.isoformat(),
            feed=self._options_feed,
        )
        snapshots = self._option_client.get_option_chain(req)

        now = now_et()
        contracts: list[OptionContract] = []

        for symbol, snap in snapshots.items():
            parsed = _parse_occ_symbol(symbol)

            # Extract quote data
            bid: float | None = None
            ask: float | None = None
            mid: float | None = None
            quote_ts: dt.datetime | None = None
            last_trade_price: float | None = None

            if snap.latest_quote is not None:
                bid = snap.latest_quote.bid_price
                ask = snap.latest_quote.ask_price
                if bid is not None and ask is not None:
                    mid = (bid + ask) / 2.0
                quote_ts = snap.latest_quote.timestamp

            if snap.latest_trade is not None:
                last_trade_price = snap.latest_trade.price

            # Greeks
            greeks: OptionGreeks | None = None
            if snap.greeks is not None:
                greeks = OptionGreeks(
                    delta=snap.greeks.delta,
                    gamma=snap.greeks.gamma,
                    theta=snap.greeks.theta,
                    vega=snap.greeks.vega,
                    rho=snap.greeks.rho,
                )

            # Quality checks
            flags = _check_quality(symbol, bid, greeks, quote_ts, now)

            contracts.append(
                OptionContract(
                    symbol=symbol,
                    underlying=parsed["underlying"] or underlying,
                    expiration=parsed["expiration"] or exp_start,
                    strike=parsed["strike"],
                    option_type=parsed["option_type"],
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    last_trade_price=last_trade_price,
                    implied_volatility=snap.implied_volatility,
                    greeks=greeks,
                    quote_timestamp=quote_ts,
                    quality_flags=flags,
                )
            )

        log.info(
            "option_chain_fetched",
            underlying=underlying,
            contracts=len(contracts),
            flagged=sum(1 for c in contracts if c.is_flagged),
        )
        return contracts

    # -- underlying_quote ----------------------------------------------------

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._data_feed)
        quotes = self._stock_client.get_stock_latest_quote(req)

        if symbol not in quotes:
            msg = f"No quote returned for {symbol}"
            raise ValueError(msg)

        q = quotes[symbol]
        bid = q.bid_price
        ask = q.ask_price
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else 0.0

        return UnderlyingQuote(
            symbol=symbol,
            bid=bid or 0.0,
            ask=ask or 0.0,
            mid=mid,
            timestamp=q.timestamp,
        )

    # -- history_bars --------------------------------------------------------

    def history_bars(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
    ) -> list[HistoryBar]:
        tf = _parse_timeframe(timeframe)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            start=dt.datetime.combine(start, dt.time.min, tzinfo=ET),
            end=dt.datetime.combine(end, dt.time.max, tzinfo=ET),
            timeframe=tf,
            feed=self._data_feed,
        )
        bars_map = self._stock_client.get_stock_bars(req)
        # BarSet is a pydantic model: ``symbol in BarSet`` iterates model
        # fields, not symbols, so always go through ``.data``.
        bars_by_symbol = getattr(bars_map, "data", bars_map)

        result: list[HistoryBar] = []
        for bar in bars_by_symbol.get(symbol, []):
            result.append(
                HistoryBar(
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    trade_count=bar.trade_count,
                    vwap=bar.vwap,
                )
            )

        return result
