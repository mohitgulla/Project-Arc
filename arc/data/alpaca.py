"""Alpaca MarketDataProvider implementation.

Uses ``alpaca-py`` data clients for option chains (via snapshots endpoint),
underlying quotes, and history bars.

Reads ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` from environment unless the
caller passes explicit keys (the integration tests pass the dedicated test
account's keys, E6.2c; production code never does).
Data-quality checks: stale timestamps, missing greeks, zero bid are flagged.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import structlog
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest

from arc.config import AlpacaDataFeed, AlpacaOptionsFeed, get_settings
from arc.data.base import (
    DataQualityFlag,
    HistoryBar,
    OptionContract,
    OptionGreeks,
    UnderlyingQuote,
)
from arc.utils.calendar import ET, now_et
from arc.utils.http_retry import ReadRetryPolicy, install_read_retry
from arc.utils.ratelimit import AccountRateBucket, bucket_path

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


def _size(v: object) -> float | None:
    """A quote size (``bs``/``as``) as a float; ``None`` when the feed omits it."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if v >= 0 else None


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


def _get_keys(api_key: str | None = None, secret_key: str | None = None) -> tuple[str, str]:
    """Explicit keys when given (both or neither), else ``ALPACA_API_KEY``/``_SECRET_KEY``."""
    if (api_key is None) != (secret_key is None):
        msg = "pass both api_key and secret_key, or neither"
        raise ValueError(msg)
    if api_key is not None and secret_key is not None:
        if not api_key or not secret_key:
            msg = "explicit Alpaca api_key/secret_key must be non-empty"
            raise ValueError(msg)
        return api_key, secret_key
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


#: E23.0a (D98): the free tier serves SIP only for windows ending >= 15 min ago;
#: one extra minute of margin for clock skew.
SIP_DELAY = dt.timedelta(minutes=16)


def sip_window_end(now: dt.datetime) -> dt.datetime:
    """The latest request end the free tier accepts on the SIP feed (``now - 16 min``)."""
    return now - SIP_DELAY


def _is_sip_refusal(exc: APIError) -> bool:
    """A free-tier refusal of recent SIP data (HTTP 403 / "subscription does not permit")."""
    try:
        status = exc.status_code
    except Exception:  # noqa: BLE001 - a malformed error still reads by its text
        status = None
    return status == 403 or "subscription does not permit" in str(exc).lower()


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
        contracts_client: TradingClient | None = None,
        raw_option_client: OptionHistoricalDataClient | None = None,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        read_retry: ReadRetryPolicy | None = None,
        rate_bucket: Any = None,
    ) -> None:
        api_key, secret_key = _get_keys(api_key, secret_key)
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
        # Open interest + daily volume are not in the parsed snapshot model; they are
        # filled from the paper Trading API contracts list (OI) and the raw snapshot
        # daily bar (volume). Built by default only for a real (non-injected) client
        # so unit tests with mocked clients stay offline.
        if option_client is None:
            contracts_client = contracts_client or TradingClient(
                api_key=api_key, secret_key=secret_key, paper=True
            )
            raw_option_client = raw_option_client or OptionHistoricalDataClient(
                api_key=api_key, secret_key=secret_key, raw_data=True
            )
        self._contracts_client = contracts_client
        self._raw_option_client = raw_option_client
        self._install_read_retry(api_key, read_retry, rate_bucket)

    def _install_read_retry(
        self, api_key: str, policy: ReadRetryPolicy | None, bucket: Any
    ) -> None:
        """D114: every data client's GETs retry transient failures (arc.utils.http_retry).

        Defaults from settings (``alpaca_read_retries`` / ``_base_s`` / ``_budget_s``);
        each retry takes a ``read`` token from this key's account bucket
        (``alpaca_rate_per_min``). Injected test doubles without a ``requests.Session``
        are left untouched.
        """
        if policy is None or bucket is None:
            s = get_settings()
            if policy is None:
                policy = ReadRetryPolicy(
                    retries=s.alpaca_read_retries,
                    base_s=s.alpaca_read_retry_base_s,
                    budget_s=s.alpaca_read_retry_budget_s,
                )
            if bucket is None:
                bucket = AccountRateBucket(
                    bucket_path(api_key, s.alpaca_rate_lock_dir), s.alpaca_rate_per_min
                )
        self.read_retry = policy
        for client in (
            self._option_client,
            self._stock_client,
            self._contracts_client,
            self._raw_option_client,
        ):
            if client is not None:
                install_read_retry(client, policy, bucket=bucket)

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
        open_interest, penny = self._contract_facts(underlying, exp_start, exp_end)
        volume = self._daily_volume(req)

        now = now_et()
        contracts: list[OptionContract] = []

        for symbol, snap in snapshots.items():
            parsed = _parse_occ_symbol(symbol)

            # Extract quote data
            bid: float | None = None
            ask: float | None = None
            mid: float | None = None
            bid_size: float | None = None
            ask_size: float | None = None
            quote_ts: dt.datetime | None = None
            last_trade_price: float | None = None

            if snap.latest_quote is not None:
                bid = snap.latest_quote.bid_price
                ask = snap.latest_quote.ask_price
                if bid is not None and ask is not None:
                    mid = (bid + ask) / 2.0
                # ``bs`` / ``as`` in the raw snapshot: top of book only (no depth on Alpaca)
                bid_size = _size(getattr(snap.latest_quote, "bid_size", None))
                ask_size = _size(getattr(snap.latest_quote, "ask_size", None))
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
                    bid_size=bid_size,
                    ask_size=ask_size,
                    last_trade_price=last_trade_price,
                    open_interest=open_interest.get(symbol),
                    volume=volume.get(symbol),
                    penny_program=penny.get(symbol),
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

    def _contract_facts(
        self, underlying: str, exp_start: dt.date, exp_end: dt.date
    ) -> tuple[dict[str, int], dict[str, bool]]:
        """OCC symbol → open interest, and → Penny Program flag (``ppind``, D66).

        One paged walk of the paper Trading API contracts list (the same requests
        as before D66). The pinned alpaca-py ``OptionContract`` model has no
        ``ppind`` field, so each page is read raw through the client's own ``get``
        with the request's fields when the client exposes it; otherwise (a client
        without ``get``) the parsed model is used and ``ppind`` stays unknown.
        """
        if self._contracts_client is None:
            return {}, {}
        oi: dict[str, int] = {}
        penny: dict[str, bool] = {}
        page: str | None = None
        while True:
            request = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_gte=exp_start,
                expiration_date_lte=exp_end,
                limit=10_000,
                page_token=page,
            )
            contracts, page = self._contracts_page(request)
            for c in contracts:
                symbol = c.get("symbol")
                if not symbol:
                    continue
                if c.get("open_interest") is not None:
                    oi[symbol] = int(c["open_interest"])
                if isinstance(c.get("ppind"), bool):
                    penny[symbol] = c["ppind"]
            if not page:
                return oi, penny

    def _contracts_page(
        self, request: GetOptionContractsRequest
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One contracts page as raw dicts plus the next page token."""
        client = self._contracts_client
        assert client is not None
        if isinstance(client, TradingClient):
            params = request.to_request_fields()
            params["underlying_symbols"] = ",".join(request.underlying_symbols or [])
            raw = client.get("/options/contracts", params)
            body = raw if isinstance(raw, dict) else {}
            return list(body.get("option_contracts") or []), body.get("next_page_token")
        resp = client.get_option_contracts(request)
        parsed = [
            {
                "symbol": c.symbol,
                "open_interest": c.open_interest,
                "ppind": getattr(c, "ppind", None),
            }
            for c in getattr(resp, "option_contracts", None) or []
        ]
        return parsed, getattr(resp, "next_page_token", None)

    def _daily_volume(self, req: OptionChainRequest) -> dict[str, int]:
        """OCC symbol → latest daily-bar volume from the raw snapshots payload."""
        if self._raw_option_client is None:
            return {}
        raw = self._raw_option_client.get_option_chain(req)
        out: dict[str, int] = {}
        for symbol, snap in raw.items():  # type: ignore[union-attr]
            bar = snap.get("dailyBar") or snap.get("prevDailyBar")
            if bar and bar.get("v") is not None:
                out[symbol] = int(bar["v"])
        return out

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
        *,
        feed: AlpacaDataFeed | str | None = None,
    ) -> list[HistoryBar]:
        """OHLCV bars for [*start*, *end*] on *feed* (``None``: the configured data feed).

        E23.0a (D98): on ``sip`` the request end is clamped to :func:`sip_window_end`
        (the free tier refuses SIP data from the last 15 minutes). A refusal anyway
        (403 / "subscription does not permit") falls back to IEX once, logged
        ``feed_fallback``; it never raises for that reason.
        """
        tf = _parse_timeframe(timeframe)
        data_feed = self._data_feed if feed is None else DataFeed(AlpacaDataFeed(feed).value)
        end_at = dt.datetime.combine(end, dt.time.max, tzinfo=ET)
        if data_feed is DataFeed.SIP:
            end_at = min(end_at, sip_window_end(now_et()))
        start_at = dt.datetime.combine(start, dt.time.min, tzinfo=ET)
        try:
            bars_map = self._get_bars(symbol, start_at, end_at, tf, data_feed)
        except APIError as exc:
            if data_feed is not DataFeed.SIP or not _is_sip_refusal(exc):
                raise
            log.warning(
                "feed_fallback",
                symbol=symbol,
                timeframe=timeframe,
                requested="sip",
                used="iex",
                error=str(exc)[:200],
            )
            end_at = dt.datetime.combine(end, dt.time.max, tzinfo=ET)
            bars_map = self._get_bars(symbol, start_at, end_at, tf, DataFeed.IEX)
        return _to_history_bars(bars_map, symbol)

    def _get_bars(
        self,
        symbol: str,
        start_at: dt.datetime,
        end_at: dt.datetime,
        tf: TimeFrame,
        feed: DataFeed,
    ) -> Any:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            start=start_at,
            end=max(end_at, start_at),
            timeframe=tf,
            feed=feed,
        )
        return self._stock_client.get_stock_bars(req)


def _to_history_bars(bars_map: Any, symbol: str) -> list[HistoryBar]:
    """``get_stock_bars`` output -> :class:`HistoryBar` list for *symbol*."""
    # BarSet is a pydantic model: ``symbol in BarSet`` iterates model
    # fields, not symbols, so always go through ``.data``.
    bars_by_symbol = getattr(bars_map, "data", bars_map)
    return [
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
        for bar in bars_by_symbol.get(symbol, [])
    ]
