"""Per-ticker daily sequences for the temporal challenger (E27.6): raw inputs, no look-ahead.

One row per (ticker, session) with :data:`CHANNELS`, all computed from data dated ≤ that
session (the vector at session *d* reads closes / OHLC / volume / option stats of *d*
and earlier only; tested by mutating every later row):

* ``ret``: log close-to-close return (split-adjusted ``underlying_ohlc``);
* ``range``: ``ln(high / low)``;
* ``gap``: ``ln(open / previous close)``;
* ``rv20``: √252 × s.d. of the last 20 ``ret`` (ddof 1);
* ``iv30`` / ``term_slope`` / ``skew_25d``: E7.16 ``option_stats/daily``;
* ``vol_rel``: ``ln(volume / mean volume of the previous 20 sessions)`` (raw
  ``underlying_daily`` volume; a ratio, so splits only blip it for 20 sessions);
* ``optvol_rel``: the same for the option volume (``ln((v+1)/(mean+1))``);
* ``spy_ret`` / ``spy_iv30``: SPY's ``ret`` and ``iv30`` on the same session (market
  context; NaN when SPY has no row that day);
* ``observed``: 1 when the session has an IV30, else 0.

A window for row (ticker, *d*) is the ticker's own last :attr:`DLConfig.seq_window`
sessions ending at *d* (inclusive, like every panel feature ``asof="close"``). Sessions
before the ticker's first row are zero-padded with ``observed = 0``.

Scaling: :class:`ChannelScaler` (median / IQR per channel, fitted on the training rows'
own session vectors), clipped to ±5, NaN → 0.

Dev only: :func:`load_tables` refuses an end on or after the holdout start (D84).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

CHANNELS: tuple[str, ...] = (
    "ret",
    "range",
    "gap",
    "rv20",
    "iv30",
    "term_slope",
    "skew_25d",
    "vol_rel",
    "optvol_rel",
    "spy_ret",
    "spy_iv30",
    "observed",
)
HOLDOUT_START = dt.date(2024, 1, 1)
MARKET = "SPY"
CLIP = 5.0


class HoldoutReadError(RuntimeError):
    """A dev-only DL loader asked for a session in the holdout."""


def _rel(v: pd.Series, n: int = 20, plus: float = 0.0) -> pd.Series:
    prev = v.shift(1).rolling(n, min_periods=5).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log((v + plus) / (prev + plus))


def ticker_vectors(
    ohlc: pd.DataFrame,
    volume: pd.Series | None,
    opt: pd.DataFrame | None,
) -> pd.DataFrame:
    """The own-ticker channels (no SPY columns) indexed by session date.

    *ohlc*: split-adjusted ``open high low close`` by date; *volume*: raw share volume
    by date; *opt*: ``option_stats/daily`` columns ``iv30 term_slope skew_25d
    opt_volume`` by date. Every value at *d* is a function of rows dated ≤ *d*.
    """
    o = ohlc.sort_index()
    c = o["close"].astype(float)
    out = pd.DataFrame(index=o.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["ret"] = np.log(c / c.shift(1))
        out["range"] = np.log(o["high"].astype(float) / o["low"].astype(float))
        out["gap"] = np.log(o["open"].astype(float) / c.shift(1))
    out["rv20"] = out["ret"].rolling(20, min_periods=10).std(ddof=1) * np.sqrt(252.0)
    if opt is not None and len(opt):
        op = opt.reindex(o.index)
        for col in ("iv30", "term_slope", "skew_25d"):
            out[col] = pd.to_numeric(op.get(col), errors="coerce")
        ov = pd.to_numeric(op.get("opt_volume"), errors="coerce")
        out["optvol_rel"] = _rel(ov, plus=1.0)
    else:
        for col in ("iv30", "term_slope", "skew_25d", "optvol_rel"):
            out[col] = np.nan
    if volume is not None and len(volume):
        out["vol_rel"] = _rel(pd.to_numeric(volume.reindex(o.index), errors="coerce"))
    else:
        out["vol_rel"] = np.nan
    out["observed"] = np.isfinite(out["iv30"].to_numpy(dtype=float)).astype(float)
    return out


def with_market(own: pd.DataFrame, spy: pd.DataFrame) -> pd.DataFrame:
    """*own* + ``spy_ret`` / ``spy_iv30`` joined on the session date, :data:`CHANNELS` order."""
    m = spy.reindex(own.index)
    out = own.copy()
    out["spy_ret"] = m["ret"].to_numpy(dtype=float)
    out["spy_iv30"] = m["iv30"].to_numpy(dtype=float)
    return out[list(CHANNELS)]


@dataclass
class SequenceTable:
    """Every ticker's session vectors stacked: ``values[(start[t]) : start[t] + n[t]]``."""

    values: np.ndarray  # (N, C) float64, unscaled
    dates: np.ndarray  # (N,) datetime64[D]
    tickers: list[str]
    start: dict[str, int]
    count: dict[str, int]

    def positions(self, tickers: Iterable[object], dates: Iterable[object]) -> np.ndarray:
        """Global row of each (ticker, date) pair, -1 when the ticker has no such session."""
        t = np.asarray([str(x) for x in tickers])
        d = pd.to_datetime(pd.Series(list(dates))).to_numpy("datetime64[D]")
        out = np.full(len(t), -1, dtype=np.int64)
        for name in np.unique(t):
            if name not in self.start:
                continue
            s, n = self.start[name], self.count[name]
            td = self.dates[s : s + n]
            m = t == name
            i = np.searchsorted(td, d[m])
            ok = (i < n) & (td[np.minimum(i, n - 1)] == d[m])
            out[np.flatnonzero(m)] = np.where(ok, s + i, -1)
        return out

    def first_row(self) -> np.ndarray:
        """Per global row, the global row of its ticker's first session."""
        fr = np.empty(len(self.values), dtype=np.int64)
        for name in self.tickers:
            s, n = self.start[name], self.count[name]
            fr[s : s + n] = s
        return fr


def build_table(frames: dict[str, pd.DataFrame]) -> SequenceTable:
    """Stack per-ticker :data:`CHANNELS` frames (sorted by ticker, then date)."""
    vals, dates, start, count = [], [], {}, {}
    pos = 0
    names = sorted(frames)
    for name in names:
        f = frames[name].sort_index()
        vals.append(f[list(CHANNELS)].to_numpy(dtype=np.float64))
        dates.append(pd.to_datetime(pd.Series(list(f.index))).to_numpy("datetime64[D]"))
        start[name], count[name] = pos, len(f)
        pos += len(f)
    v = np.concatenate(vals) if vals else np.zeros((0, len(CHANNELS)))
    d = np.concatenate(dates) if dates else np.zeros(0, dtype="datetime64[D]")
    return SequenceTable(v, d, names, start, count)


def load_tables(
    data_dir: Path, tickers: Sequence[str], start: dt.date, end: dt.date
) -> SequenceTable:
    """The dev :class:`SequenceTable` of *tickers* (+ SPY context) for [start, end]."""
    from arc.backtest.underlying import UnderlyingStore
    from arc.ml.features.adapters.history import load_ohlc, load_option_daily

    if end >= HOLDOUT_START:
        msg = f"DL sequence end {end} reaches the holdout (≥ {HOLDOUT_START}); D84"
        raise HoldoutReadError(msg)
    us = UnderlyingStore(data_dir)

    def own(t: str) -> pd.DataFrame | None:
        px = load_ohlc(data_dir, t, start, end)
        if px is None or px.empty:
            return None
        try:
            bars = us.read_bars(t)
            vol = bars["volume"]
            vol.index = [pd.Timestamp(x).date() for x in vol.index]
            vol = vol[(vol.index >= start) & (vol.index <= end)]  # type: ignore[operator]
        except (FileNotFoundError, KeyError):
            vol = None
        opt = load_option_daily(data_dir, t, start, end)
        return ticker_vectors(px, vol, opt)

    spy = own(MARKET)
    if spy is None:
        msg = f"no {MARKET} OHLC in {data_dir} (market context)"
        raise FileNotFoundError(msg)
    frames = {}
    for t in sorted(set(tickers)):
        f = spy if t == MARKET else own(t)
        if f is not None:
            frames[t] = with_market(f, spy)
    return build_table(frames)


class ChannelScaler(BaseModel):
    """Median / IQR scaler per channel (fitted on training session vectors)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    center: list[float]
    scale: list[float]

    @classmethod
    def fit(cls, values: np.ndarray) -> ChannelScaler:
        c, s = [], []
        for j in range(values.shape[1]):
            col = values[:, j]
            col = col[np.isfinite(col)]
            if col.size == 0:
                c.append(0.0)
                s.append(1.0)
                continue
            q25, q50, q75 = np.quantile(col, [0.25, 0.5, 0.75])
            iqr = float(q75 - q25) / 1.349
            c.append(float(q50))
            s.append(iqr if iqr > 0 else float(np.std(col)) or 1.0)
        return cls(center=c, scale=s)

    def apply(self, values: np.ndarray) -> np.ndarray:
        z = (values - np.asarray(self.center)) / np.asarray(self.scale)
        z = np.clip(np.nan_to_num(z, nan=0.0, posinf=CLIP, neginf=-CLIP), -CLIP, CLIP)
        return z.astype(np.float32)


@dataclass
class WindowGather:
    """``idx -> (B, window, C)`` float32 windows for frame rows with global rows *pos*."""

    scaled: np.ndarray  # (N, C) float32
    first: np.ndarray  # (N,) first global row of the row's ticker
    pos: np.ndarray  # (n_rows,) -1 = no session
    window: int
    observed_col: int = CHANNELS.index("observed")

    @property
    def shape(self) -> tuple[int, int]:
        return (self.window, self.scaled.shape[1])

    def __call__(self, idx: np.ndarray) -> np.ndarray:
        p = self.pos[idx]
        offs = np.arange(-self.window + 1, 1)
        g = p[:, None] + offs[None, :]
        fr = np.where(p >= 0, self.first[np.maximum(p, 0)], 0)
        ok = (p[:, None] >= 0) & (g >= fr[:, None])
        out = self.scaled[np.clip(g, 0, max(len(self.scaled) - 1, 0))]
        out[~ok] = 0.0
        return out


def gather_for(
    table: SequenceTable,
    tickers: Iterable[object],
    dates: Iterable[object],
    window: int,
    scaler: ChannelScaler,
) -> WindowGather:
    raw = table.values.copy()
    obs = CHANNELS.index("observed")
    scaled = scaler.apply(raw)
    scaled[:, obs] = np.nan_to_num(raw[:, obs]).astype(np.float32)  # keep the 0/1 flag
    return WindowGather(scaled, table.first_row(), table.positions(tickers, dates), window)


def train_vectors(table: SequenceTable, before: dt.date) -> np.ndarray:
    """Session vectors dated before *before* (the scaler's fit rows)."""
    return table.values[table.dates < np.datetime64(before, "D")]
