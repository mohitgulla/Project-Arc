"""Conformal calibration of quantile forecasts (E27.2, CQR per level).

Each quantile level is calibrated on its own (both tails, the owner's 2026-10-17 note:
credit spreads are priced on the adverse tail, so one interval width is not enough).
With a forecast ``q̂_p`` and a positive scale ``s`` (the forecast's own q90 − q10, which
makes the offset adaptive across tickers), the offset on a calibration set is

    c_p = Quantile_{p'}( (y − q̂_p) / s ),   p' = clip(⌈(n + 1)·p⌉ / n, 0, 1) for p ≥ ½,
                                            p' = ⌊(n + 1)·p⌋ / n            for p < ½,

the finite-sample conformal level, so the calibrated ``q̂_p + c_p·s`` has
``P(y ≤ ·) ≈ p`` on exchangeable data. Pure numpy; the walk-forward calibrates on the
last 252 sessions before each test block (rolling under drift).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence


def sort_quantiles(q: np.ndarray) -> tuple[np.ndarray, float]:
    """(row-sorted copy, share of rows whose quantiles crossed before sorting)."""
    q = np.asarray(q, dtype=float)
    if q.size == 0:
        return q.copy(), 0.0
    crossed = (np.diff(q, axis=1) < 0).any(axis=1)
    return np.sort(q, axis=1), float(crossed.mean())


def interval_scale(q: np.ndarray, levels: Sequence[float]) -> np.ndarray:
    """Per row ``q90 − q10`` (positive; floored at 1e-6 of its median)."""
    lv = list(levels)
    s = q[:, lv.index(0.9)] - q[:, lv.index(0.1)]
    good = s[np.isfinite(s) & (s > 0)]
    floor = 1e-6 * (float(np.median(good)) if good.size else 1.0)
    return np.where(np.isfinite(s) & (s > floor), s, floor)


def conformal_level(p: float, n: int) -> float:
    if n <= 0:
        return p
    k = math.ceil((n + 1) * p) if p >= 0.5 else math.floor((n + 1) * p)  # noqa: PLR2004
    return min(max(k / n, 0.0), 1.0)


def fit_offsets(q: np.ndarray, y: np.ndarray, levels: Sequence[float]) -> dict[float, float]:
    """Per-level offsets (in scale units) from a calibration set."""
    q = np.asarray(q, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(y) & np.isfinite(q).all(axis=1)
    q, y = q[ok], y[ok]
    if not len(y):
        return dict.fromkeys(levels, 0.0)
    s = interval_scale(q, levels)
    out: dict[float, float] = {}
    for j, p in enumerate(levels):
        e = (y - q[:, j]) / s
        how = "higher" if p >= 0.5 else "lower"  # noqa: PLR2004
        out[float(p)] = float(np.quantile(e, conformal_level(p, len(e)), method=how))
    return out


def apply_offsets(
    q: np.ndarray, offsets: dict[float, float], levels: Sequence[float]
) -> np.ndarray:
    """Calibrated, re-sorted quantiles (the scale is the *uncalibrated* forecast's)."""
    q = np.asarray(q, dtype=float)
    s = interval_scale(q, levels)
    adj = q + np.column_stack([offsets[float(p)] * s for p in levels])
    return np.sort(adj, axis=1)
