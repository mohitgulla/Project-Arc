"""Historical options data for backtesting (E7.1).

Providers implement :class:`HistoricalDataProvider`; data is cached as parquet
under ``data/options_eod/`` by :class:`ParquetHistoryStore`.

Usage::

    from arc.data.history import AlpacaHistoryProvider, ParquetHistoryStore, download
"""

from arc.data.history.alpaca import ALPACA_OPTIONS_HISTORY_START, AlpacaHistoryProvider
from arc.data.history.base import (
    EOD_COLUMNS,
    HistoricalDataProvider,
    OptionEodRow,
    OptionRight,
    occ_symbol,
)
from arc.data.history.download import DownloadResult, download, missing_ranges
from arc.data.history.store import (
    DayCoverage,
    ParquetHistoryStore,
    TickerCoverage,
    format_coverage,
)
from arc.data.history.thetadata import (
    THETA_DEFAULT_URL,
    ThetaDataEodProvider,
    ThetaTerminalError,
    parse_eod_csv,
)

__all__ = [
    "ALPACA_OPTIONS_HISTORY_START",
    "EOD_COLUMNS",
    "THETA_DEFAULT_URL",
    "AlpacaHistoryProvider",
    "DayCoverage",
    "DownloadResult",
    "HistoricalDataProvider",
    "OptionEodRow",
    "OptionRight",
    "ParquetHistoryStore",
    "ThetaDataEodProvider",
    "ThetaTerminalError",
    "TickerCoverage",
    "download",
    "format_coverage",
    "missing_ranges",
    "occ_symbol",
    "parse_eod_csv",
]
