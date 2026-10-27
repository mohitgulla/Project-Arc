"""Implied-vol surfaces on a fixed grid for the surface challenger (E27.6).

Per (ticker, session) one surface: IV at each call-delta-equivalent bucket
(:data:`DELTA_GRID`) × constant-maturity DTE bucket (:data:`DTE_GRID`, inside the live
chain window ``CHAIN_DTE_MIN`` … ``CHAIN_DTE_MAX``), from that session's quoted marked
chain (:func:`arc.backtest.marks.mark_chains`, the E7.16 option-stats marks):

1. **OTM side per bucket.** Call-delta equivalent ``c = Δ_call`` for calls and
   ``c = 1 + Δ_put`` for puts; a bucket ``c ≤ 0.5`` reads the expiry's calls at
   ``Δ_call = c``, a bucket ``c > 0.5`` its puts at ``Δ_put = c − 1`` (the liquid OTM
   wing on each side; 0.5 reads both and averages).
2. **Per expiry**: linear interpolation in delta over the expiry's quoted contracts
   (:func:`arc.backtest.option_stats._interp_delta`, the same helper as ``skew_25d``);
   a bucket the expiry's deltas don't bracket is missing for that expiry.
3. **Across expiries**: per Δ bucket, the live constant-maturity total-variance
   interpolation (:func:`arc.features.vol.constant_maturity_iv`) to each DTE bucket over
   the expiries 7–75 DTE that have the bucket; flat outside the observed DTE range
   (same rule as IV30). ``mask`` = 1 when the DTE bucket is bracketed by two observed
   expiries (or hits one exactly), 0.5 when extrapolated flat, 0 when no expiry has the
   Δ bucket (IV NaN).

Point-in-time by construction: the surface of session *d* reads only session *d*'s
chain (tested by mutating every other session). Aggregates only: the cache under
``data/ml/dl/surfaces/`` holds the grid values, never raw option rows (D84).

Model input (:func:`surface_tensor`): channels ``ln IV`` (standardised by the scalar
centre/scale fitted on training rows), ``IV / ATM-30d − 1`` (shape) and ``mask``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.iv.record import CHAIN_DTE_MAX, CHAIN_DTE_MIN

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

DELTA_GRID: tuple[float, ...] = (0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90)
DTE_GRID: tuple[int, ...] = (10, 20, 30, 45, 60, 75)
ATM_I, D30_J = DELTA_GRID.index(0.50), DTE_GRID.index(30)
SURFACE_VERSION = 1
HOLDOUT_START = dt.date(2024, 1, 1)
CHANNELS = 3


def _cols() -> list[str]:
    return [f"iv_{i}_{j}" for i in range(len(DELTA_GRID)) for j in range(len(DTE_GRID))] + [
        f"m_{i}_{j}" for i in range(len(DELTA_GRID)) for j in range(len(DTE_GRID))
    ]


GRID_COLS = _cols()


def _cm(points: list[tuple[int, float]], target: int) -> tuple[float, float]:
    """(IV at *target* DTE, mask) from ``(dte, iv)`` points (live cm interpolation)."""
    from arc.features.vol import constant_maturity_iv

    if not points:
        return float("nan"), 0.0
    d = [p[0] for p in points]
    v = constant_maturity_iv(points, target_dte=target)
    exact = target in d
    inside = min(d) <= target <= max(d)
    return float(v), 1.0 if (exact or (inside and len(set(d)) > 1)) else 0.5


def day_surface(chain: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """``(iv, mask)`` arrays of shape ``(len(DELTA_GRID), len(DTE_GRID))`` for one
    session's marked chain (columns ``dte right iv delta``)."""
    from arc.backtest.option_stats import _interp_delta

    nd, nt = len(DELTA_GRID), len(DTE_GRID)
    iv = np.full((nd, nt), np.nan)
    mask = np.zeros((nd, nt))
    if chain.empty or "delta" not in chain:
        return iv, mask
    win = chain[(chain["dte"] >= CHAIN_DTE_MIN) & (chain["dte"] <= CHAIN_DTE_MAX)]
    per: list[list[tuple[int, float]]] = [[] for _ in DELTA_GRID]
    for dte, g in win.groupby("dte", sort=True):
        calls = g[g["right"] == "call"]
        puts = g[g["right"] == "put"]
        for i, c in enumerate(DELTA_GRID):
            vals = []
            if c <= 0.5:  # noqa: PLR2004
                v = _interp_delta(calls, c)
                if v is not None:
                    vals.append(v)
            if c >= 0.5:  # noqa: PLR2004
                v = _interp_delta(puts, c - 1.0)
                if v is not None:
                    vals.append(v)
            if vals:
                per[i].append((int(dte), float(np.mean(vals))))  # type: ignore[call-overload]
    for i in range(nd):
        for j, t in enumerate(DTE_GRID):
            iv[i, j], mask[i, j] = _cm(per[i], t)
    return iv, mask


def surfaces_from_chains(ticker: str, chains: dict[dt.date, pd.DataFrame]) -> pd.DataFrame:
    """One row per session: ``ticker date`` + :data:`GRID_COLS` (iv then mask)."""
    rows = []
    for d in sorted(chains):
        iv, m = day_surface(chains[d])
        rows.append([ticker, d, *iv.ravel().tolist(), *m.ravel().tolist()])
    return pd.DataFrame(rows, columns=["ticker", "date", *GRID_COLS])


def surface_dir(data_dir: Path) -> Path:
    return data_dir / "ml" / "dl" / "surfaces" / f"v{SURFACE_VERSION}"


def build_ticker(data_dir: Path, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Every cached session of *ticker* in [start, end] (dev only), from disk."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.marks import RateCurve, mark_chains
    from arc.backtest.option_stats import CHUNK_SESSIONS, StatsConfig, _read_raw
    from arc.backtest.underlying import UnderlyingStore
    from arc.data.history.store import ParquetHistoryStore

    if end >= HOLDOUT_START:
        msg = f"surface end {end} reaches the holdout (≥ {HOLDOUT_START}); D84"
        raise ValueError(msg)
    cfg = StatsConfig()
    ds = cfg.data
    store = ParquetHistoryStore(data_dir)
    days = sorted(d for d in store.cached_dates(cfg.provider, ticker) if start <= d <= end)
    closes = UnderlyingStore(data_dir).read_bars(ticker)["close"].dropna()
    rate = RateCurve.load(data_dir, flat=cfg.flat_rate, source=ds.rate_source)
    cost = load_cost_model()
    parts = []
    for k in range(0, len(days), CHUNK_SESSIONS):
        raw = _read_raw(store, cfg.provider, ticker, days[k : k + CHUNK_SESSIONS])
        chains, _ = mark_chains(
            raw,
            closes,
            mode=ds.marks,
            cost=cost,
            rate=rate,
            dte_min=CHAIN_DTE_MIN,
            dte_max=CHAIN_DTE_MAX,
            max_spread_pct=ds.max_quoted_spread_pct,
            fallback=ds.quoted_fallback,
        )
        parts.append(surfaces_from_chains(ticker, chains))
    if not parts:
        return pd.DataFrame(columns=["ticker", "date", *GRID_COLS])
    return pd.concat(parts, ignore_index=True)


def _build_one(args: tuple[str, str, dt.date, dt.date]) -> tuple[str, int]:
    from pathlib import Path

    data, ticker, start, end = args
    out = surface_dir(Path(data)) / f"underlying={ticker}.parquet"
    if out.is_file():
        return ticker, -1
    df = build_ticker(Path(data), ticker, start, end)
    tmp = out.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(out)
    return ticker, len(df)


def build_cache(
    data_dir: Path,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    workers: int = 6,
    say=lambda _m: None,
) -> int:
    """Resumable per-ticker surface cache (a written ticker is skipped)."""
    from concurrent.futures import ProcessPoolExecutor

    surface_dir(data_dir).mkdir(parents=True, exist_ok=True)
    jobs = [(str(data_dir), t, start, end) for t in sorted(set(tickers))]
    n = 0
    with ProcessPoolExecutor(max_workers=max(1, min(workers, 6))) as ex:
        for i, (t, rows) in enumerate(ex.map(_build_one, jobs), 1):
            n += max(rows, 0)
            if i % 25 == 0 or i == len(jobs):
                say(f"  surfaces {i}/{len(jobs)} ({t}: {rows})")
    return n


def load_cache(data_dir: Path, tickers: Iterable[str]) -> pd.DataFrame:
    parts = []
    for t in sorted(set(tickers)):
        p = surface_dir(data_dir) / f"underlying={t}.parquet"
        if p.is_file():
            parts.append(pd.read_parquet(p))
    if not parts:
        return pd.DataFrame(columns=["ticker", "date", *GRID_COLS])
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if len(df) and max(df["date"]) >= HOLDOUT_START:
        df = df[df["date"] < HOLDOUT_START]
    return df.reset_index(drop=True)


@dataclass
class SurfaceGather:
    """``idx -> (B, CHANNELS, len(DELTA_GRID), len(DTE_GRID))`` for frame rows."""

    tensor: np.ndarray  # (N_surfaces + 1, C, H, W); the last slot = all-zero (missing)
    pos: np.ndarray  # (n_rows,) index into tensor

    @property
    def shape(self) -> tuple[int, int, int]:
        return (CHANNELS, len(DELTA_GRID), len(DTE_GRID))

    def __call__(self, idx: np.ndarray) -> np.ndarray:
        return self.tensor[self.pos[idx]]


def surface_tensor(cache: pd.DataFrame, center: float, scale: float) -> np.ndarray:
    """``(N + 1, 3, H, W)`` float32: ln IV standardised, shape vs ATM-30, mask."""
    nd, nt = len(DELTA_GRID), len(DTE_GRID)
    k = nd * nt
    v = cache[GRID_COLS[:k]].to_numpy(dtype=np.float64).reshape(-1, nd, nt)
    m = cache[GRID_COLS[k:]].to_numpy(dtype=np.float64).reshape(-1, nd, nt)
    with np.errstate(divide="ignore", invalid="ignore"):
        lv = (np.log(v) - center) / scale
        atm = v[:, ATM_I, D30_J][:, None, None]
        shp = np.where(atm > 0, v / atm - 1.0, np.nan)
    t = np.stack([lv, np.clip(shp, -3, 3), m], axis=1)
    t = np.nan_to_num(t, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return np.concatenate([t, np.zeros((1, CHANNELS, nd, nt), dtype=np.float32)])


def log_iv_stats(cache: pd.DataFrame, before: dt.date) -> tuple[float, float]:
    """(median, IQR/1.349) of ln IV over the surfaces dated before *before*."""
    k = len(DELTA_GRID) * len(DTE_GRID)
    sub = cache[cache["date"] < before]
    v = sub[GRID_COLS[:k]].to_numpy(dtype=np.float64).ravel()
    v = np.log(v[np.isfinite(v) & (v > 0)])
    if v.size == 0:
        return 0.0, 1.0
    q25, q50, q75 = np.quantile(v, [0.25, 0.5, 0.75])
    s = float(q75 - q25) / 1.349
    return float(q50), s if s > 0 else 1.0


def gather_for(
    cache: pd.DataFrame,
    tickers: Iterable[object],
    dates: Iterable[object],
    center: float,
    scale: float,
) -> SurfaceGather:
    t = surface_tensor(cache, center, scale)
    key = pd.MultiIndex.from_arrays([cache["ticker"].astype(str), cache["date"]])
    look = pd.Series(np.arange(len(cache)), index=key)
    q = pd.MultiIndex.from_arrays([pd.Series([str(x) for x in tickers]), pd.Series(list(dates))])
    pos = look.reindex(q).to_numpy()
    pos = np.where(np.isfinite(pos), pos, len(cache)).astype(np.int64)
    return SurfaceGather(t, pos)
