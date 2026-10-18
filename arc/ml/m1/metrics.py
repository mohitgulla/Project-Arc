"""M1 evaluation metrics (E27.2): pure numpy/pandas.

* :func:`pinball`: the quantile (pinball) loss per row and level.
* :func:`crps_from_quantiles`: CRPS approximated from the 7 quantiles as
  ``2/K · Σ_k pinball_k`` (the quantile-score decomposition of CRPS on a fixed grid);
  used to rank models, not as an absolute CRPS.
* :func:`coverage`: empirical ``P(y ≤ q_p)`` per level.
* :func:`dm_test`: Diebold-Mariano on a daily loss differential series (the per-day
  cross-sectional mean), Newey-West HAC variance with ``h − 1`` lags because h-session
  labels overlap; two-sided normal p-value.
* :func:`qlike`: ``σ²/σ̂² − ln(σ²/σ̂²) − 1`` (Patton 2011), robust for vol forecasts.
* :func:`daily_rank_ic`: mean per-day Spearman correlation of forecast and outcome.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from scipy.special import ndtr

if TYPE_CHECKING:
    from collections.abc import Sequence


def pinball(y: np.ndarray, q: np.ndarray, levels: Sequence[float]) -> np.ndarray:
    """(n, K) pinball loss; *q* is (n, K)."""
    y = np.asarray(y, dtype=float)[:, None]
    p = np.asarray(levels, dtype=float)[None, :]
    d = y - np.asarray(q, dtype=float)
    return np.maximum(p * d, (p - 1.0) * d)


def crps_from_quantiles(y: np.ndarray, q: np.ndarray, levels: Sequence[float]) -> np.ndarray:
    return 2.0 * pinball(y, q, levels).mean(axis=1)


def coverage(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Per level, the share of rows with ``y ≤ q``."""
    return (np.asarray(y, dtype=float)[:, None] <= np.asarray(q, dtype=float)).mean(axis=0)


def newey_west_var(d: np.ndarray, lags: int) -> float:
    d = np.asarray(d, dtype=float)
    d = d - d.mean()
    n = len(d)
    v = float(d @ d) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1.0 - k / (lags + 1.0)
        v += 2.0 * w * float(d[k:] @ d[:-k]) / n
    return v


def dm_test(diff: np.ndarray, horizon: int) -> tuple[float, float]:
    """(DM statistic, two-sided p) of mean(diff) = 0; diff = loss_model − loss_baseline."""
    d = np.asarray(diff, dtype=float)
    d = d[np.isfinite(d)]
    n = len(d)
    if n < 10:  # noqa: PLR2004
        return float("nan"), float("nan")
    v = newey_west_var(d, max(horizon - 1, 0))
    if v <= 0:
        return float("nan"), float("nan")
    stat = float(d.mean()) / math.sqrt(v / n)
    return stat, float(2.0 * ndtr(-abs(stat)))


def qlike(rv: np.ndarray, fc: np.ndarray) -> np.ndarray:
    a = np.asarray(rv, dtype=float) ** 2
    b = np.asarray(fc, dtype=float) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        x = a / b
        return np.where((a > 0) & (b > 0), x - np.log(x) - 1.0, np.nan)


def daily_rank_ic(dates: Sequence[object], fc: np.ndarray, y: np.ndarray, min_n: int = 20) -> float:
    df = pd.DataFrame({"d": list(dates), "f": fc, "y": y}).dropna()
    ics = [
        g["f"].rank().corr(g["y"].rank()) for _, g in df.groupby("d", sort=True) if len(g) >= min_n
    ]
    ics = [x for x in ics if np.isfinite(x)]
    return float(np.mean(ics)) if ics else float("nan")
