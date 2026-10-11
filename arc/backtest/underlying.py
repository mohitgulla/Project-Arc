"""Daily underlying closes for the backtester (settlement + delta inputs).

Closes are raw (unadjusted) consolidated daily bars — option strikes are raw
prices, so split/dividend-adjusted closes would misprice moneyness.  They are
cached as one parquet per ticker under ``<data_dir>/underlying_daily/``.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — runtime in index comparisons
from pathlib import Path
from typing import Protocol

import pandas as pd
import structlog

from arc.utils.calendar import ET

log = structlog.get_logger()

__all__ = ["BarsSource", "UnderlyingStore", "load_closes"]


class BarsSource(Protocol):
    """Anything that returns daily raw closes for a symbol."""

    def daily_closes(self, symbol: str, start: dt.date, end: dt.date) -> pd.Series:
        """Close per session date (``datetime.date`` index), ascending."""
        ...


class AlpacaBarsSource:  # pragma: no cover - network
    """Alpaca SIP daily bars (historical SIP is free once >15 min old)."""

    def __init__(self) -> None:
        from arc.data.alpaca import AlpacaMarketData

        self._md = AlpacaMarketData(data_feed="sip")

    def daily_closes(self, symbol: str, start: dt.date, end: dt.date) -> pd.Series:
        # Free tier rejects SIP data from the last 15 min; never ask past yesterday.
        from arc.utils.calendar import now_et

        end = min(end, now_et().date() - dt.timedelta(days=1))
        bars = self._md.history_bars(symbol, start, end, "1Day")
        idx = [b.timestamp.astimezone(ET).date() for b in bars]
        return pd.Series([b.close for b in bars], index=idx, name=symbol, dtype=float)


class UnderlyingStore:
    """Parquet cache: ``<root>/underlying_daily/<SYM>.parquet`` with columns date, close.

    E7.7 (D84) adds an optional raw ``volume`` column (shares) for the point-in-time
    universe's dollar-volume screen. :meth:`write` keeps an existing volume column for
    the dates it rewrites, so the closes-only callers never drop it.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / "underlying_daily"

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.parquet"

    def read(self, symbol: str) -> pd.Series:
        p = self.path_for(symbol)
        if not p.is_file():
            return pd.Series(dtype=float, name=symbol.upper())
        df = pd.read_parquet(p)
        vals = df["close"].to_numpy(dtype=float)
        s = pd.Series(vals, index=list(df["date"]), name=symbol.upper())
        return s.sort_index()

    def read_bars(self, symbol: str) -> pd.DataFrame:
        """Columns ``close`` and ``volume`` (NaN where never fetched) by session date."""
        p = self.path_for(symbol)
        if not p.is_file():
            return pd.DataFrame({"close": [], "volume": []}, dtype=float)
        df = pd.read_parquet(p)
        vol = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else float("nan")
        out = pd.DataFrame(
            {"close": df["close"].to_numpy(dtype=float), "volume": vol}, index=list(df["date"])
        )
        return out.sort_index()

    def write(self, symbol: str, closes: pd.Series) -> Path:
        old = self.read_bars(symbol)["volume"]
        vol = [old.get(d, float("nan")) for d in closes.index]
        return self._write(symbol, list(closes.index), closes.to_numpy(dtype=float), vol)

    def write_bars(self, symbol: str, bars: pd.DataFrame) -> Path:
        """Write ``close`` + ``volume`` (raw, unadjusted) indexed by session date."""
        bars = bars.sort_index()
        return self._write(
            symbol,
            list(bars.index),
            bars["close"].to_numpy(dtype=float),
            bars["volume"].to_numpy(dtype=float),
        )

    def _write(self, symbol: str, dates: list[dt.date], close: object, volume: object) -> Path:
        p = self.path_for(symbol)
        p.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame({"date": dates, "close": close, "volume": volume})
        tmp = p.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(p)
        return p


def load_closes(
    store: UnderlyingStore,
    symbol: str,
    start: dt.date,
    end: dt.date,
    source: BarsSource | None = None,
) -> pd.Series:
    """Cached closes covering [start, end]; fetch from *source* when the cache falls short."""
    cached = store.read(symbol)
    have = len(cached) > 0 and min(cached.index) <= start and max(cached.index) >= end
    if not have and source is not None:
        fetched = source.daily_closes(symbol, start, end)
        merged = pd.concat([cached, fetched])
        merged = merged[~pd.Index(merged.index).duplicated(keep="last")].sort_index()
        store.write(symbol, merged)
        log.info("underlying.fetched", symbol=symbol, rows=len(fetched))
        cached = merged
    return cached[(pd.Index(cached.index) >= start) & (pd.Index(cached.index) <= end)]
