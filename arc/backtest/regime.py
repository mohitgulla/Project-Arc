"""Regime labels for conditioning backtest results (known at the entry close, no look-ahead).

Trend regime uses the v1 rule of ``arc.features.regime`` (E4.3): the trailing
20-session return ``close_t / close_{t-20} − 1`` → ``bull`` if ≥ +5%, ``bear`` if
≤ −5%, else ``sideways``. Vectorised here (with an ``unknown`` warm-up label) using
the same thresholds as :func:`arc.features.regime.label_regimes`.

Vol regime: trailing 20-session annualised realised vol of log returns,
bucketed ``low`` / ``mid`` / ``high`` at fixed thresholds (12% / 20%), so the
label on day *t* never depends on data after *t* (no full-sample terciles).
It is :func:`arc.features.regime.label_vol` (one implementation, D77 / E17.1).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from arc.features.regime import (
    BEAR_THRESHOLD as BEAR,
)
from arc.features.regime import (
    BULL_THRESHOLD as BULL,
)
from arc.features.regime import (
    LOOKBACK_DAYS as LOOKBACK,
)
from arc.features.regime import (
    VOL_HIGH,
    VOL_LOW,
    label_vol,
)

__all__ = ["BEAR", "BULL", "LOOKBACK", "VOL_HIGH", "VOL_LOW", "label_trend", "label_vol"]


def label_trend(closes: pd.Series, lookback: int = LOOKBACK) -> pd.Series:
    """bear / sideways / bull per date; ``unknown`` until *lookback* sessions exist."""
    r = closes / closes.shift(lookback) - 1.0
    out = np.where(r >= BULL, "bull", np.where(r <= BEAR, "bear", "sideways"))
    out = np.where(r.isna(), "unknown", out)
    return pd.Series(out, index=closes.index, name="trend")
