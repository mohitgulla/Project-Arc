"""Shared helpers for daily time series used by the feature modules.

Every feature function works on a ``pd.Series`` indexed by ``datetime.date``
(one row per trading session, ascending). These helpers normalise caller
input to that shape and enforce the walk-forward cut-off.
"""

from __future__ import annotations

import datetime as dt
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
