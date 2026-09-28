"""Regime labels for conditioning backtest results (known at the entry close, no look-ahead).

Trend regime uses the same rule as E4.3 (``arc.features.regime``, PR #9): the
trailing 20-session return ``close_t / close_{t-20} − 1`` → ``bull`` if ≥ +5%,
``bear`` if ≤ −5%, else ``sideways``. It is re-implemented here (≈10 lines)
because E4.3 is not merged yet; swap to ``arc.features.regime.label_regimes``
once it is.

Vol regime: trailing 20-session annualised realised vol of log returns,
bucketed ``low`` / ``mid`` / ``high`` at fixed thresholds (12% / 20%), so the
label on day *t* never depends on data after *t* (no full-sample terciles).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["LOOKBACK", "label_trend", "label_vol"]

LOOKBACK = 20
BULL = 0.05
BEAR = -0.05
VOL_LOW = 0.12
VOL_HIGH = 0.20


def label_trend(closes: pd.Series, lookback: int = LOOKBACK) -> pd.Series:
    """bear / sideways / bull per date; ``unknown`` until *lookback* sessions exist."""
    r = closes / closes.shift(lookback) - 1.0
    out = np.where(r >= BULL, "bull", np.where(r <= BEAR, "bear", "sideways"))
    out = np.where(r.isna(), "unknown", out)
    return pd.Series(out, index=closes.index, name="trend")


def label_vol(closes: pd.Series, lookback: int = LOOKBACK) -> pd.Series:
    """low / mid / high realised-vol bucket per date; ``unknown`` during warm-up."""
    lr = np.log(closes / closes.shift(1))
    rv = lr.rolling(lookback).std(ddof=1) * np.sqrt(252.0)
    out = np.where(rv < VOL_LOW, "low", np.where(rv >= VOL_HIGH, "high", "mid"))
    out = np.where(rv.isna(), "unknown", out)
    return pd.Series(out, index=closes.index, name="vol")
