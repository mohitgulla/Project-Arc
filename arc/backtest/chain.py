"""Per-session option chain preparation: mid, spread, DTE, implied vol and delta.

The backtester only sees end-of-day data. For each session we attach to every
contract row:

* ``mid`` / ``spread`` from :class:`arc.backtest.costs.CostModel` (quoted NBBO
  when present, else trade close + estimated spread);
* ``dte`` in calendar days;
* ``iv`` solved from ``mid`` with a vectorised BSM bisection
  (:func:`arc.pricing.bs.price_vectorized`), European assumption;
* ``delta`` (BSM, per share) at that IV.

Contracts whose mid is outside the no-arbitrage band (below intrinsic, above
the forward bound) get ``NaN`` IV/delta and are never selected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from scipy.stats import norm

from arc.pricing.bs import price_vectorized

if TYPE_CHECKING:
    from arc.backtest.costs import CostModel

__all__ = ["IV_HI", "IV_LO", "implied_vol_vec", "prepare_chain"]

IV_LO = 0.01
IV_HI = 5.0
_BISECT_ITERS = 60


def implied_vol_vec(
    price: np.ndarray,
    spot: np.ndarray | float,
    strike: np.ndarray,
    t: np.ndarray,
    r: float,
    q: float,
    flag: np.ndarray,
) -> np.ndarray:
    """Vectorised BSM implied vol by bisection on [IV_LO, IV_HI]; NaN when not bracketed."""
    price = np.asarray(price, dtype=float)
    strike = np.asarray(strike, dtype=float)
    t = np.asarray(t, dtype=float)
    flag = np.asarray(flag)
    spot_a = np.broadcast_to(np.asarray(spot, dtype=float), price.shape)
    lo = np.full(price.shape, IV_LO)
    hi = np.full(price.shape, IV_HI)
    p_lo = price_vectorized(flag, spot_a, strike, t, r, lo, q)
    p_hi = price_vectorized(flag, spot_a, strike, t, r, hi, q)
    ok = np.isfinite(price) & (price > p_lo) & (price < p_hi) & (t > 0)
    for _ in range(_BISECT_ITERS):
        mid = 0.5 * (lo + hi)
        p_mid = price_vectorized(flag, spot_a, strike, t, r, mid, q)
        above = p_mid > price
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)


def _delta_vec(
    spot: float, strike: np.ndarray, t: np.ndarray, r: float, q: float, sigma: np.ndarray, is_call
) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        d1 = (np.log(spot / strike) + (r - q + 0.5 * sigma**2) * t) / (sigma * np.sqrt(t))
    df_q = np.exp(-q * t)
    return np.where(is_call, df_q * norm.cdf(d1), df_q * (norm.cdf(d1) - 1.0))


def prepare_chain(
    rows: pd.DataFrame,
    spot: float,
    *,
    cost: CostModel,
    r: float,
    q: float = 0.0,
) -> pd.DataFrame:
    """Return *rows* (one session, one underlying) with mid/spread/dte/iv/delta columns.

    Rows without a usable price, or expiring on/before the session, are dropped.
    """
    if rows.empty:
        return rows.assign(mid=[], spread=[], dte=[], iv=[], delta=[])
    df = rows.copy()
    dates = pd.to_datetime(df["date"])
    exps = pd.to_datetime(df["expiration"])
    df["dte"] = (exps - dates).dt.days.astype(int)
    df = df[df["dte"] > 0]

    bid = df["bid"].to_numpy(dtype=float) if "bid" in df else np.full(len(df), np.nan)
    ask = df["ask"].to_numpy(dtype=float) if "ask" in df else np.full(len(df), np.nan)
    close = df["close"].to_numpy(dtype=float)
    has_q = np.isfinite(bid) & np.isfinite(ask) & (bid >= 0) & (ask >= bid) & (ask > 0)
    mid = np.where(has_q, (bid + ask) / 2.0, close)
    est = np.maximum(cost.spread_min, cost.spread_pct * np.maximum(np.nan_to_num(mid), 0.0))
    spread = np.where(has_q, ask - bid, est)
    df["mid"] = mid
    df["spread"] = spread
    df["quoted"] = has_q
    df = df[np.isfinite(df["mid"]) & (df["mid"] > 0)]
    if df.empty:
        return df.assign(iv=[], delta=[])

    t = df["dte"].to_numpy(dtype=float) / 365.0
    k = df["strike"].to_numpy(dtype=float)
    is_call = (df["right"].astype(str) == "call").to_numpy()
    flag = np.where(is_call, "c", "p")
    iv = implied_vol_vec(df["mid"].to_numpy(dtype=float), spot, k, t, r, q, flag)
    df["iv"] = iv
    df["delta"] = _delta_vec(spot, k, t, r, q, iv, is_call)
    return df.reset_index(drop=True)
