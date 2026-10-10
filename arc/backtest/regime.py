"""Regime labels for conditioning backtest results (known at the entry close, no look-ahead).

Trend regime uses the v1 rule of ``arc.features.regime`` (E4.3): the trailing
20-session return ``close_t / close_{t-20} − 1`` → ``bull`` if ≥ +5%, ``bear`` if
≤ −5%, else ``sideways``. Vectorised here (with an ``unknown`` warm-up label) using
the same thresholds as :func:`arc.features.regime.label_regimes`.

Vol regime: trailing 20-session annualised realised vol of log returns,
bucketed ``low`` / ``mid`` / ``high`` at fixed thresholds (12% / 20%), so the
label on day *t* never depends on data after *t* (no full-sample terciles).
It is :func:`arc.features.regime.label_vol` (one implementation, D77 / E17.1).

**Regime v2 (D77, E17.3):** :func:`regime_labels` with ``model="v2"`` uses the live
v2 labeller of :mod:`arc.features.regime` instead: the vol-scaled trend z
(:func:`~arc.features.regime.label_regimes_v2`) and the per-ticker realised-vol
percentile state (:func:`~arc.features.regime.label_vol_v2`, fixed buckets during its
warm-up). Both read closes ≤ *t* only. ``model="v1"`` returns exactly
:func:`label_trend` / :func:`label_vol`, so the v1 backtest path is unchanged.
"""

from __future__ import annotations

from typing import Literal

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
    V2_TREND_Z,
    V2_VOL_RANK_WINDOW,
    V2_VOL_SCALE_WINDOW,
    VOL_HIGH,
    VOL_LOW,
    label_regimes_v2,
    label_vol,
    label_vol_v2,
)

__all__ = [
    "BEAR",
    "BULL",
    "LOOKBACK",
    "VOL_HIGH",
    "VOL_LOW",
    "BacktestRegimeModel",
    "label_trend",
    "label_trend_v2",
    "label_vol",
    "label_vol_state_v2",
    "regime_labels",
]

BacktestRegimeModel = Literal["v1", "v2"]


def label_trend(closes: pd.Series, lookback: int = LOOKBACK) -> pd.Series:
    """bear / sideways / bull per date; ``unknown`` until *lookback* sessions exist."""
    r = closes / closes.shift(lookback) - 1.0
    out = np.where(r >= BULL, "bull", np.where(r <= BEAR, "bear", "sideways"))
    out = np.where(r.isna(), "unknown", out)
    return pd.Series(out, index=closes.index, name="trend")


def label_trend_v2(
    closes: pd.Series,
    *,
    trend_z: float = V2_TREND_Z,
    vol_window: int = V2_VOL_SCALE_WINDOW,
) -> pd.Series:
    """v2 trend label per date of *closes* (``unknown`` before a full vol-scale window)."""
    labels = label_regimes_v2(closes, trend_z=trend_z, vol_window=vol_window)
    known: dict[object, str] = {d: str(v) for d, v in labels.items()}
    return pd.Series(
        [known.get(d, "unknown") for d in closes.index], index=closes.index, name="trend"
    )


def label_vol_state_v2(closes: pd.Series, *, rank_window: int = V2_VOL_RANK_WINDOW) -> pd.Series:
    """v2 vol state per date of *closes*: own-history rv20 percentile (fixed-bucket warm-up)."""
    v = label_vol_v2(closes, rank_window=rank_window)
    known: dict[object, str] = {d: str(s) for d, s in zip(v.dates, v.state, strict=True)}
    return pd.Series(
        [known.get(d, "unknown") for d in closes.index], index=closes.index, name="vol"
    )


def regime_labels(
    closes: pd.Series, model: BacktestRegimeModel = "v1"
) -> tuple[pd.Series, pd.Series]:
    """(trend, vol) labels per date of *closes* under *model* (v1 = the E7.2/E7.5 labels)."""
    c = closes.sort_index()
    if model == "v1":
        return label_trend(c), label_vol(c)
    return label_trend_v2(c), label_vol_state_v2(c)
