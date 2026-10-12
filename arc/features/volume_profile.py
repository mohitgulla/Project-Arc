"""Composite volume profile over N completed sessions (E23.0, PLAN D98).

A deterministic price-by-volume histogram built from intraday OHLCV bars (15-min SIP
bars by default): the point of control (POC), the value area (VAL..VAH, the price band
around the POC holding ``value_area`` of the volume) and the high/low-volume nodes
(HVN/LVN). These are the support/resistance levels the range-edge filter (E23.1), the
pullback watch (E23.2) and the strike-vs-level context (E23.3) read.

Pure: no network, no LLM, no clock. Context only, never a gate input (import-linter).

Construction:

* Bins are ``bin_width`` wide, anchored at ``floor(min low / bin_width) * bin_width``.
* Each bar's volume is spread evenly over every bin its ``[low, high]`` touches (a bar
  with ``high == low`` puts it all in one bin).
* POC = the bin with the most volume; a tie goes to the bin nearest the profile's
  volume-weighted mean price, then the lower one. ``poc`` is that bin's centre.
* Value area: start at the POC bin and repeatedly add the heavier of the next bin above
  / below (a tie adds both) until the included volume reaches ``value_area`` of the
  total. ``val`` / ``vah`` are the outer *edges* of the included bins, so
  ``val <= poc <= vah`` and the value area's share is at least ``value_area``.
* HVN / LVN: local maxima / minima of the 3-bin centred moving average of the
  histogram. An HVN's smoothed share of the total is at least ``hvn_min_share``; an LVN
  is an interior local minimum whose smoothed share is below it. Prices are bin centres.

Empty input, zero total volume or a non-positive bin width give ``None``.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - used at runtime in the pydantic model
import math
from typing import TYPE_CHECKING, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "PRICE_TICK",
    "ProfileBar",
    "SessionOhlc",
    "VolumeProfile",
    "bin_width_for",
    "build_profile",
    "nearest_above",
    "nearest_below",
    "rth_bars",
    "session_ohlc",
]

#: Smallest bin width (one cent: the stock price increment above $1).
PRICE_TICK = 0.01
_RTH_OPEN = dt.time(9, 30)
_RTH_CLOSE = dt.time(16, 0)
_EPS = 1e-9


class ProfileBar(Protocol):
    """An OHLCV bar (e.g. ``arc.data.base.HistoryBar``)."""

    @property
    def timestamp(self) -> dt.datetime: ...
    @property
    def open(self) -> float: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...
    @property
    def volume(self) -> float: ...


class VolumeProfile(BaseModel):
    """One composite volume profile (prices in the bars' currency)."""

    model_config = ConfigDict(extra="forbid")

    bin_width: float = Field(..., gt=0.0)
    bins: int = Field(..., ge=1, description="Histogram bins from the lowest low to the top")
    total_volume: float = Field(..., gt=0.0)
    poc: float = Field(..., description="Point of control: centre of the heaviest bin")
    val: float = Field(..., description="Value area low (lower edge)")
    vah: float = Field(..., description="Value area high (upper edge)")
    va_share: float = Field(..., ge=0.0, le=1.0 + _EPS, description="Volume share inside VA")
    hvns: list[float] = Field(default_factory=list, description="High-volume nodes, ascending")
    lvns: list[float] = Field(default_factory=list, description="Low-volume nodes, ascending")


class SessionOhlc(BaseModel):
    """One session's OHLC aggregated from its intraday bars."""

    model_config = ConfigDict(extra="forbid")

    day: dt.date
    open: float
    high: float
    low: float
    close: float


# ---------------------------------------------------------------------------
# Bar selection
# ---------------------------------------------------------------------------


def rth_bars[B: ProfileBar](bars: Iterable[B], sessions: Iterable[dt.date]) -> list[B]:
    """The bars of *sessions* that start inside regular trading hours (09:30-16:00 ET).

    Alpaca intraday bars carry extended-hours prints; the profile uses the regular
    session only. Ordered by timestamp.
    """
    keep = set(sessions)
    out = [
        b
        for b in bars
        if (ts := b.timestamp.astimezone(ET)).date() in keep and _RTH_OPEN <= ts.time() < _RTH_CLOSE
    ]
    return sorted(out, key=lambda b: b.timestamp)


def session_ohlc(bars: Iterable[ProfileBar]) -> list[SessionOhlc]:
    """Per-session OHLC from intraday bars (sessions ascending)."""
    by_day: dict[dt.date, list[ProfileBar]] = {}
    for b in sorted(bars, key=lambda b: b.timestamp):
        by_day.setdefault(b.timestamp.astimezone(ET).date(), []).append(b)
    return [
        SessionOhlc(
            day=day,
            open=float(rows[0].open),
            high=max(float(r.high) for r in rows),
            low=min(float(r.low) for r in rows),
            close=float(rows[-1].close),
        )
        for day, rows in sorted(by_day.items())
    ]


def bin_width_for(atr14: float | None, bin_atr: float, *, tick: float = PRICE_TICK) -> float:
    """``max(tick, atr14 * bin_atr)``; just *tick* without a positive ATR."""
    if atr14 is None or not math.isfinite(atr14) or atr14 <= 0:
        return tick
    return max(tick, atr14 * bin_atr)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def _histogram(
    bars: Sequence[ProfileBar], bin_width: float
) -> tuple[float, np.ndarray, float] | None:
    """``(anchor, volume per bin, volume-weighted mean price)`` or ``None``."""
    rows = [
        (float(b.low), float(b.high), float(b.volume), float(b.close))
        for b in bars
        if b.volume and b.volume > 0 and math.isfinite(float(b.volume))
    ]
    rows = [(min(lo, hi), max(lo, hi), v, c) for lo, hi, v, c in rows if lo > 0 and hi > 0]
    if not rows:
        return None
    anchor = math.floor(min(r[0] for r in rows) / bin_width + _EPS) * bin_width
    top = max(r[1] for r in rows)
    n = int(math.floor((top - anchor) / bin_width + _EPS)) + 1
    hist = np.zeros(n, dtype=float)
    weighted = 0.0
    for lo, hi, vol, _ in rows:
        i0 = int(math.floor((lo - anchor) / bin_width + _EPS))
        i1 = int(math.floor((hi - anchor) / bin_width + _EPS))
        i0, i1 = max(0, min(i0, n - 1)), max(0, min(i1, n - 1))
        hist[i0 : i1 + 1] += vol / (i1 - i0 + 1)
        weighted += vol * (lo + hi) / 2.0
    total = float(hist.sum())
    if total <= 0:
        return None
    return anchor, hist, weighted / total


def _poc_index(hist: np.ndarray, anchor: float, bin_width: float, vwmp: float) -> int:
    peak = float(hist.max())
    ties = [i for i in range(len(hist)) if hist[i] >= peak - _EPS * max(1.0, peak)]
    return min(ties, key=lambda i: (abs(anchor + (i + 0.5) * bin_width - vwmp), i))


def _value_area(hist: np.ndarray, poc: int, value_area: float) -> tuple[int, int, float]:
    """``(low bin, high bin, included volume)`` of the value area around *poc*."""
    total = float(hist.sum())
    target = value_area * total
    lo = hi = poc
    acc = float(hist[poc])
    n = len(hist)
    while acc < target - _EPS * total and (lo > 0 or hi < n - 1):
        below = float(hist[lo - 1]) if lo > 0 else -1.0
        above = float(hist[hi + 1]) if hi < n - 1 else -1.0
        if above > below:
            hi += 1
            acc += above
        elif below > above:
            lo -= 1
            acc += below
        else:  # a tie (both sides exist): take both
            lo, hi = lo - 1, hi + 1
            acc += above + below
    return lo, hi, acc


def _nodes(hist: np.ndarray, min_share: float) -> tuple[list[int], list[int]]:
    """``(HVN bins, LVN bins)``: local maxima / minima of the 3-bin moving average."""
    n = len(hist)
    if n < 3:  # noqa: PLR2004 - a node needs a neighbour on each side
        return [], []
    total = float(hist.sum())
    padded = np.concatenate(([hist[0]], hist, [hist[-1]]))
    smooth = (padded[:-2] + padded[1:-1] + padded[2:]) / 3.0
    share = smooth / total
    hvn: list[int] = []
    lvn: list[int] = []
    for i in range(n):
        left = smooth[i - 1] if i > 0 else -math.inf
        right = smooth[i + 1] if i < n - 1 else -math.inf
        # plateaus count once (strictly above the left, at least the right)
        is_max = smooth[i] > left + _EPS * total and smooth[i] >= right - _EPS * total
        if is_max and share[i] >= min_share:
            hvn.append(i)
        interior = 0 < i < n - 1
        is_min = (
            interior
            and smooth[i] < smooth[i - 1] - _EPS * total
            and smooth[i] <= smooth[i + 1] + _EPS * total
        )
        if is_min and share[i] < min_share:
            lvn.append(i)
    return hvn, lvn


def build_profile(
    bars: Iterable[ProfileBar],
    *,
    bin_width: float,
    value_area: float = 0.70,
    hvn_min_share: float = 0.03,
) -> VolumeProfile | None:
    """The composite profile of *bars* (``None``: no bars, zero volume, bad width)."""
    if not math.isfinite(bin_width) or bin_width <= 0:
        return None
    built = _histogram(list(bars), bin_width)
    if built is None:
        return None
    anchor, hist, vwmp = built
    total = float(hist.sum())
    poc = _poc_index(hist, anchor, bin_width, vwmp)
    lo, hi, acc = _value_area(hist, poc, value_area)
    hvn, lvn = _nodes(hist, hvn_min_share)

    def centre(i: int) -> float:
        return round(anchor + (i + 0.5) * bin_width, 6)

    return VolumeProfile(
        bin_width=round(bin_width, 6),
        bins=len(hist),
        total_volume=total,
        poc=centre(poc),
        val=round(anchor + lo * bin_width, 6),
        vah=round(anchor + (hi + 1) * bin_width, 6),
        va_share=min(1.0, acc / total),
        hvns=[centre(i) for i in hvn],
        lvns=[centre(i) for i in lvn],
    )


def nearest_above(levels: Iterable[float], price: float) -> float | None:
    """The lowest level strictly above *price* (``None`` if none)."""
    above = [x for x in levels if x > price]
    return min(above) if above else None


def nearest_below(levels: Iterable[float], price: float) -> float | None:
    """The highest level strictly below *price* (``None`` if none)."""
    below = [x for x in levels if x < price]
    return max(below) if below else None
