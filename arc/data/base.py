"""MarketDataProvider protocol — venue-agnostic market data interface.

See PLAN.md §2.2 (arc/data/) and §4 (E1.4).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime in pydantic models
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class OptionGreeks(BaseModel):
    """Greeks for a single option contract from the data provider."""

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


class DataQualityFlag(BaseModel):
    """A data-quality issue flagged on an option contract."""

    symbol: str
    issue: str  # "stale_timestamp" | "missing_greeks" | "zero_bid"
    detail: str = ""


class OptionContract(BaseModel):
    """A single option contract with market data (from chain snapshot)."""

    symbol: str = Field(..., description="OCC option symbol")
    underlying: str
    expiration: dt.date
    strike: float
    option_type: str  # "call" | "put"

    # Quote data
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    bid_size: float | None = Field(None, description="Top-of-book bid size (contracts)")
    ask_size: float | None = Field(None, description="Top-of-book ask size (contracts)")
    last_trade_price: float | None = None

    # Volume / interest
    open_interest: int | None = None
    volume: int | None = None

    # Volatility and Greeks
    implied_volatility: float | None = None
    greeks: OptionGreeks | None = None

    # Timestamp of the snapshot
    quote_timestamp: dt.datetime | None = None

    # Data quality
    quality_flags: list[DataQualityFlag] = Field(default_factory=list)

    @property
    def is_flagged(self) -> bool:
        """Return True if any data-quality issues were flagged."""
        return len(self.quality_flags) > 0


class UnderlyingQuote(BaseModel):
    """Latest quote for an underlying equity."""

    symbol: str
    bid: float
    ask: float
    mid: float
    timestamp: dt.datetime


class HistoryBar(BaseModel):
    """A single OHLCV bar."""

    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: float | None = None
    vwap: float | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProvider(Protocol):
    """Venue-agnostic market data interface.

    Methods
    -------
    option_chain(underlying, exp_start, exp_end)
        Full option chain for *underlying* with expirations in
        [exp_start, exp_end].  Contracts include bid/ask/mid/oi/vol/IV/greeks.
        Stale, missing-greeks, and zero-bid contracts are flagged.
    underlying_quote(symbol)
        Latest bid/ask/mid for an underlying equity.
    history_bars(symbol, start, end, timeframe)
        OHLCV bars for *symbol* over [start, end].
    """

    def option_chain(
        self,
        underlying: str,
        exp_start: dt.date,
        exp_end: dt.date,
    ) -> list[OptionContract]: ...

    def underlying_quote(self, symbol: str) -> UnderlyingQuote: ...

    def history_bars(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
    ) -> list[HistoryBar]: ...


REFERENCE_LOOKBACK_DAYS = 10


def reference_price(
    provider: MarketDataProvider,
    symbol: str,
    *,
    today: dt.date,
) -> float | None:
    """A sanity-check reference price for *symbol*, or ``None`` if unavailable.

    Uses the quote mid only when both sides are positive. Off-hours IEX quotes
    often have a zero side (``ask=0``), which makes the adapter's mid half the
    real price; in that case the latest daily close is used instead.
    """
    q = provider.underlying_quote(symbol)
    if q.bid > 0 and q.ask > 0:
        return (q.bid + q.ask) / 2.0
    bars = provider.history_bars(symbol, today - dt.timedelta(days=REFERENCE_LOOKBACK_DAYS), today)
    return bars[-1].close if bars and bars[-1].close > 0 else None
