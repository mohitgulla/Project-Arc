"""MarketDataProvider protocol and broker-specific adapters.

Usage::

    from arc.data.base import MarketDataProvider, OptionContract
    from arc.data.alpaca import AlpacaMarketData
"""

from arc.data.base import (
    DataQualityFlag,
    HistoryBar,
    MarketDataProvider,
    OptionContract,
    OptionGreeks,
    UnderlyingQuote,
)

__all__ = [
    "DataQualityFlag",
    "HistoryBar",
    "MarketDataProvider",
    "OptionContract",
    "OptionGreeks",
    "UnderlyingQuote",
]
