"""Shared helpers for daily time series used by the feature modules.

Every feature function works on a ``pd.Series`` indexed by ``datetime.date``
(one row per trading session, ascending). These helpers normalise caller
input to that shape and enforce the walk-forward cut-off.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Iterable


class InsufficientHistoryError(ValueError):
    """Raised when a series is too short to compute the requested feature."""


class BarLike(Protocol):
    """Anything with a timestamp and a close (e.g. ``arc.data.base.HistoryBar``)."""

    @property
    def timestamp(self) -> dt.datetime: ...

    @property
    def close(self) -> float: ...


def _to_et_date(value: object) -> dt.date:
    """Map a date / datetime / Timestamp to its America/New_York calendar date."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(ET)
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise TypeError(f"unsupported index value {value!r}")


def to_daily_series(values: pd.Series, *, name: str = "value") -> pd.Series:
    """Normalise *values* to a float series indexed by ET ``date``, ascending.

    NaNs are dropped. Duplicate dates keep the last observation.
    """
    if values.empty:
        return pd.Series(dtype=float, name=name)
    idx = [_to_et_date(v) for v in values.index]
    out = pd.Series(values.to_numpy(dtype=float), index=idx, name=name).dropna()
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def closes_from_bars(bars: Iterable[BarLike]) -> pd.Series:
    """Build a daily close series from OHLCV bars."""
    data = {_to_et_date(b.timestamp): float(b.close) for b in bars}
    return to_daily_series(pd.Series(data, dtype=float), name="close")


def truncate(values: pd.Series, as_of: dt.date) -> pd.Series:
    """Keep only observations dated on or before *as_of* (the look-ahead guard)."""
    if values.empty:
        return values
    mask = [d <= as_of for d in values.index]
    return values[mask]


class OhlcBarLike(BarLike, Protocol):
    """A bar with open/high/low too (``arc.data.base.HistoryBar``), E16.2."""

    @property
    def open(self) -> float: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...


OHLC_COLUMNS = ("open", "high", "low", "close")


def ohlc_from_bars(bars: Iterable[OhlcBarLike]) -> pd.DataFrame:
    """Daily ``open high low close`` frame indexed by ET ``date``, ascending (E16.2).

    Duplicate dates keep the last bar; a bar with a missing, non-finite or
    non-positive price is dropped (never patched), so close-only bars give an
    empty frame (no technicals) rather than an error.
    """
    rows: dict[dt.date, tuple[float, float, float, float]] = {}
    for b in bars:
        raw = tuple(getattr(b, k, None) for k in OHLC_COLUMNS)
        if any(v is None for v in raw):
            continue
        vals = tuple(float(v) for v in raw)  # type: ignore[arg-type]
        if all(v > 0 and math.isfinite(v) for v in vals):
            rows[_to_et_date(b.timestamp)] = vals
    if not rows:
        return pd.DataFrame(columns=list(OHLC_COLUMNS), dtype=float)
    days = sorted(rows)
    return pd.DataFrame([rows[d] for d in days], index=days, columns=list(OHLC_COLUMNS))


def truncate_frame(frame: pd.DataFrame, as_of: dt.date) -> pd.DataFrame:
    """Rows dated on or before *as_of* (the look-ahead guard for a daily frame)."""
    if frame.empty:
        return frame
    return frame.loc[[d <= as_of for d in frame.index]]
