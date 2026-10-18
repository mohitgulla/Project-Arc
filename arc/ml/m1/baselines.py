"""M1 baselines (E27.2, D126): pure functions over the feature panel.

Three baselines, each also kept as an M1 input feature:

* **HAR-RV per horizon** (``har_rv_h{h}``, computed in the feature store,
  :func:`arc.ml.features.compute.m1_baseline_frame`): a point forecast of ``rv_h``.
* **HAR-IV** (:class:`HarIv`): a pooled OLS of ``ln rv_h`` on ``ln har_rv_h`` and
  ``ln IV30`` (+ intercept), fitted on the training rows; its residual quantiles give
  ``rv_h`` quantiles. Rows without IV30 fall back to ``ln har_rv_h`` alone.
* **Historical quantiles** (``hq_q{p}_h{h}``, feature store): trailing 252 standardised
  h-session returns x HAR vol, the ``log_ret_h`` baseline.
* **Implied-vol lognormal** (:func:`iv_lognormal_quantiles`): zero drift,
  ``ln S_{t+h}/S_t ~ N(−½σ²τ, σ²τ)`` at σ = IV30, τ = h/252.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict
from scipy.special import ndtri

from arc.ml.features.compute import M1_QUANTILES, TRADING_DAYS, qtag

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd


def hq_columns(h: int, levels: Sequence[float] = M1_QUANTILES) -> list[str]:
    return [f"hq_q{qtag(p)}_h{h}" for p in levels]


def iv_lognormal_quantiles(
    iv: np.ndarray, h: int, levels: Sequence[float] = M1_QUANTILES
) -> np.ndarray:
    """(n, K) zero-drift lognormal quantiles of the h-session log return at σ = *iv*."""
    s = np.asarray(iv, dtype=float) * math.sqrt(h / TRADING_DAYS)
    z = ndtri(np.asarray(levels, dtype=float))
    return (-0.5 * s**2)[:, None] + s[:, None] * z[None, :]


def _log(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(x > 0, np.log(x), np.nan)


class HarIv(BaseModel):
    """Pooled HAR-IV for ``rv_h``: ``ln rv = a + b·ln har + c·ln iv`` (+ residual quantiles)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    h: int
    coef: tuple[float, float, float]
    coef_no_iv: tuple[float, float]
    resid_q: tuple[float, ...]
    resid_q_no_iv: tuple[float, ...]
    levels: tuple[float, ...] = M1_QUANTILES

    @classmethod
    def fit(cls, df: pd.DataFrame, h: int, levels: Sequence[float] = M1_QUANTILES) -> HarIv:
        y = _log(df[f"rv_h{h}"].to_numpy())
        lh = _log(df[f"har_rv_h{h}"].to_numpy())
        li = _log(df["iv30"].to_numpy())
        ok2 = np.isfinite(y) & np.isfinite(lh)
        ok3 = ok2 & np.isfinite(li)
        x3 = np.column_stack([np.ones(ok3.sum()), lh[ok3], li[ok3]])
        b3, *_ = np.linalg.lstsq(x3, y[ok3], rcond=None)
        x2 = np.column_stack([np.ones(ok2.sum()), lh[ok2]])
        b2, *_ = np.linalg.lstsq(x2, y[ok2], rcond=None)
        lv = np.asarray(levels, dtype=float)
        return cls(
            h=h,
            coef=(float(b3[0]), float(b3[1]), float(b3[2])),
            coef_no_iv=(float(b2[0]), float(b2[1])),
            resid_q=tuple(float(v) for v in np.quantile(y[ok3] - x3 @ b3, lv)),
            resid_q_no_iv=tuple(float(v) for v in np.quantile(y[ok2] - x2 @ b2, lv)),
            levels=tuple(float(p) for p in levels),
        )

    def quantiles(self, har: np.ndarray, iv: np.ndarray) -> np.ndarray:
        """(n, K) quantiles of ``rv_h`` (NaN where HAR is missing)."""
        lh, li = _log(har), _log(iv)
        a, b, c = self.coef
        a2, b2 = self.coef_no_iv
        with_iv = np.isfinite(li)
        mu = np.where(with_iv, a + b * lh + c * np.nan_to_num(li), a2 + b2 * lh)
        r = np.where(
            with_iv[:, None], np.asarray(self.resid_q)[None, :], np.asarray(self.resid_q_no_iv)
        )
        return np.exp(mu[:, None] + r)
