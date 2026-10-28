"""M1 predict (E27.2): pure features → calibrated quantiles and the published features.

Nothing here reads a file, a database, the network or the clock: the caller loads an
:class:`M1Model` (:func:`arc.ml.m1.train.load_model` reads the artefact directory) and
passes a feature frame (the E27.1 panel columns). :func:`predict_quantiles` and
:func:`publish` never mutate their input.

Published features (registry group ``model``, :mod:`arc.ml.features.registry`):

* ``m1_q{05..95}_h{h}``: calibrated quantiles of the h-session log return;
* ``m1_rv_h{h}``: calibrated median of the h-session realised vol;
* ``m1_p_up_h{h}``: P(log return > 0) from the quantile function :func:`quantile_grid`;
* ``m1_move_vs_implied_h{h}``: E|log return| / (IV30·√(h/252));
* ``m1_direction``: sign of the h21 median when |median| > 0.25 × (q75 − q25), else 0.

The quantile function between the 7 levels is piecewise linear; beyond q05/q95 it
follows a two-piece Student-t whose ν matches the forecast's tail ratio
(q95 − q05)/(q75 − q25) and whose left/right scales match q50 − q05 / q95 − q50, so both
tails stay as calibrated (credit spreads are priced on the adverse one).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import t as student_t

from arc.exits.model import M1PathParams
from arc.ml.features.compute import M1_HORIZONS, M1_QUANTILES, TRADING_DAYS, qtag
from arc.ml.m1.baselines import HarIv, hq_columns, iv_lognormal_quantiles
from arc.ml.m1.conformal import apply_offsets
from arc.ml.m1.dataset import scale

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Kind = Literal["log_ret", "rv"]
Source = Literal["lgbm", "baseline"]
KINDS: tuple[Kind, ...] = ("log_ret", "rv")
#: m1_direction: |median| must exceed this share of the q25–q75 range.
DIRECTION_K = 0.25
NU_MIN, NU_MAX = 3.0, 200.0
_GRID = 200


class TargetModel(BaseModel):
    """How one (kind, h) target is forecast, and its conformal offsets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    h: int
    source: Source
    offsets: dict[str, float] = Field(..., description="qtag -> CQR offset (scale units)")
    har_iv: HarIv | None = None


class M1Manifest(BaseModel):
    """The artefact manifest (``manifest.json``): enough to rebuild the model byte-identically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    feature_schema: str
    features: list[str]
    levels: tuple[float, ...] = M1_QUANTILES
    horizons: tuple[int, ...] = M1_HORIZONS
    targets: list[TargetModel]
    lgb_params: dict[str, Any]
    num_rounds: int
    train_window: dict[str, str]
    calibration_window: dict[str, str]
    code_sha: str
    config_hashes: dict[str, str]
    data_digest: str
    files: dict[str, str] = Field(default_factory=dict, description="file -> sha256")
    #: E27.7 (D133): the ``ML-<n>`` study that trained it, and its overlay beyond
    #: params/rounds (both None for a plain ``arc ml m1 train``).
    study: str | None = None
    overlay: dict[str, Any] | None = None

    def target(self, kind: str, h: int) -> TargetModel:
        for t in self.targets:
            if t.kind == kind and t.h == h:
                return t
        msg = f"no M1 target {kind} h{h}"
        raise KeyError(msg)


@dataclass(frozen=True)
class M1Model:
    """A loaded M1: the manifest plus one booster per (kind, h, level) of ``lgbm`` targets."""

    manifest: M1Manifest
    boosters: Mapping[tuple[str, int, float], Any]


def rv_scale(df: pd.DataFrame, h: int) -> np.ndarray:
    """Annualised vol scale of ``rv_h`` (HAR → rv20 → IV30)."""
    return scale(df, h) / math.sqrt(h / TRADING_DAYS)


def feature_matrix(df: pd.DataFrame, features: Sequence[str]) -> np.ndarray:
    return df.reindex(columns=list(features)).to_numpy(dtype=np.float64)


def raw_lgbm(
    boosters: Mapping[tuple[str, int, float], Any],
    kind: str,
    h: int,
    x: np.ndarray,
    df: pd.DataFrame,
    levels: Sequence[float],
) -> np.ndarray:
    """Uncalibrated (n, K) quantiles of the target in its own units.

    The boosters predict a vol-standardised target: ``log_ret / scale`` and
    ``ln(rv / rv_scale)``; both maps are monotone per row, so quantiles map back exactly.
    """
    z = np.column_stack([boosters[(kind, h, float(p))].predict(x) for p in levels])
    if kind == "log_ret":
        return z * scale(df, h)[:, None]
    return np.exp(z) * rv_scale(df, h)[:, None]


def raw_baseline(
    kind: str, h: int, df: pd.DataFrame, har_iv: HarIv | None, levels: Sequence[float]
) -> np.ndarray:
    """Uncalibrated (n, K) baseline quantiles: historical quantiles for ``log_ret`` (a
    zero-drift lognormal at IV30, else at the HAR → rv20 scale, where they are missing);
    HAR-IV for ``rv``."""
    if kind == "log_ret":
        q = df.reindex(columns=hq_columns(h, levels)).to_numpy(dtype=float, copy=True)
        iv = df["iv30"].to_numpy(dtype=float)
        # σ for the lognormal fallback: IV30, else the HAR → rv20 realised-vol scale
        sig = np.where(iv > 0, iv, rv_scale(df, h))
        ln = iv_lognormal_quantiles(sig, h, levels)
        miss = ~np.isfinite(q).all(axis=1)
        q[miss] = ln[miss]
        return q
    if har_iv is None:
        msg = "rv baseline needs a fitted HarIv"
        raise ValueError(msg)
    har = df[f"har_rv_h{h}"].to_numpy(dtype=float)
    har = np.where(np.isfinite(har) & (har > 0), har, rv_scale(df, h))
    return har_iv.quantiles(har, df["iv30"].to_numpy(dtype=float))


def predict_quantiles(model: M1Model, df: pd.DataFrame) -> dict[tuple[str, int], np.ndarray]:
    """Calibrated, sorted (n, K) quantiles per (kind, h) for the rows of *df*."""
    man = model.manifest
    lv = list(man.levels)
    x = feature_matrix(df, man.features) if any(t.source == "lgbm" for t in man.targets) else None
    out: dict[tuple[str, int], np.ndarray] = {}
    for t in man.targets:
        if t.source == "lgbm":
            assert x is not None
            raw = raw_lgbm(model.boosters, t.kind, t.h, x, df, lv)
        else:
            raw = raw_baseline(t.kind, t.h, df, t.har_iv, lv)
        offs = {float(p): t.offsets[qtag(p)] for p in lv}
        out[(t.kind, t.h)] = apply_offsets(raw, offs, lv)
    return out


# ---------------------------------------------------------------------------
# The quantile function (two-piece Student-t tails)
# ---------------------------------------------------------------------------


@cache
def _t_ratio_table() -> tuple[np.ndarray, np.ndarray]:
    nus = np.geomspace(NU_MIN, NU_MAX, 400)
    r = (student_t.ppf(0.95, nus) * 2) / (student_t.ppf(0.75, nus) * 2)
    return nus, r


def fit_nu(q: np.ndarray, levels: Sequence[float] = M1_QUANTILES) -> np.ndarray:
    """Per row ν whose t tail ratio (q95 − q05)/(q75 − q25) matches the forecast's."""
    lv = list(levels)
    num = q[:, lv.index(0.95)] - q[:, lv.index(0.05)]
    den = q[:, lv.index(0.75)] - q[:, lv.index(0.25)]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = num / den
    nus, r = _t_ratio_table()  # r decreasing in ν
    nu = np.interp(ratio, r[::-1], nus[::-1], left=NU_MAX, right=NU_MIN)
    return np.where(np.isfinite(ratio), nu, NU_MAX)


def quantile_grid(
    q: np.ndarray, levels: Sequence[float] = M1_QUANTILES, n_grid: int = _GRID
) -> tuple[np.ndarray, np.ndarray]:
    """(u grid (G,), Q (n, G)): the row quantile functions on u = (i + ½)/G."""
    lv = np.asarray(levels, dtype=float)
    u = (np.arange(n_grid) + 0.5) / n_grid
    nu = fit_nu(q, levels)
    i05, i50, i95 = list(levels).index(0.05), list(levels).index(0.5), list(levels).index(0.95)
    t95 = student_t.ppf(0.95, nu)
    s_l = (q[:, i50] - q[:, i05]) / t95
    s_r = (q[:, i95] - q[:, i50]) / t95
    out = np.empty((len(q), n_grid))
    mid = (u >= lv[0]) & (u <= lv[-1])
    for i in range(len(q)):
        out[i, mid] = np.interp(u[mid], lv, q[i])
    lo, hi = u < lv[0], u > lv[-1]
    out[:, lo] = q[:, [i50]] + s_l[:, None] * student_t.ppf(u[lo][None, :], nu[:, None])
    out[:, hi] = q[:, [i50]] + s_r[:, None] * student_t.ppf(u[hi][None, :], nu[:, None])
    return u, np.maximum.accumulate(out, axis=1)


def p_up_and_abs(
    q: np.ndarray, levels: Sequence[float] = M1_QUANTILES
) -> tuple[np.ndarray, np.ndarray]:
    """(P(X > 0), E|X|) per row from the quantile function."""
    u, g = quantile_grid(q, levels)
    below = (g <= 0).mean(axis=1)
    return 1.0 - below, np.abs(g).mean(axis=1)


def direction(q21: np.ndarray, levels: Sequence[float] = M1_QUANTILES) -> np.ndarray:
    """+1 / −1 / 0 per row (NaN where the forecast is missing)."""
    lv = list(levels)
    med = q21[:, lv.index(0.5)]
    iqr = q21[:, lv.index(0.75)] - q21[:, lv.index(0.25)]
    d = np.where(np.abs(med) > DIRECTION_K * iqr, np.sign(med), 0.0)
    return np.where(np.isfinite(med) & np.isfinite(iqr), d, np.nan)


def publish(model: M1Model, df: pd.DataFrame) -> pd.DataFrame:
    """The ``m1_*`` features for every row of *df* (index aligned, ``date``/``ticker`` kept)."""
    qs = predict_quantiles(model, df)
    lv = list(model.manifest.levels)
    out = pd.DataFrame(index=df.index)
    for c in ("date", "ticker"):
        if c in df:
            out[c] = df[c]
    iv = df["iv30"].to_numpy(dtype=float)
    for h in model.manifest.horizons:
        q = qs[("log_ret", h)]
        for j, p in enumerate(lv):
            out[f"m1_q{qtag(p)}_h{h}"] = q[:, j]
        out[f"m1_rv_h{h}"] = qs[("rv", h)][:, lv.index(0.5)]
        pu, ea = p_up_and_abs(q, lv)
        ok = np.isfinite(q).all(axis=1)
        out[f"m1_p_up_h{h}"] = np.where(ok, pu, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            mvi = ea / (iv * math.sqrt(h / TRADING_DAYS))
        out[f"m1_move_vs_implied_h{h}"] = np.where(ok & (iv > 0), mvi, np.nan)
    out["m1_direction"] = direction(qs[("log_ret", 21)], lv)
    return out


def m1_trend_labels(m1_direction: pd.Series) -> pd.Series:
    """``m1_direction`` as trend labels (bull / bear / sideways / unknown) by date."""
    lab = {1.0: "bull", -1.0: "bear", 0.0: "sideways"}
    return m1_direction.map(lambda v: lab.get(float(v), "unknown") if pd.notna(v) else "unknown")


# ---------------------------------------------------------------------------
# Monte Carlo hook
# ---------------------------------------------------------------------------


def horizon_for_dte(dte: int, horizons: Sequence[int] = M1_HORIZONS) -> int:
    """The M1 horizon nearest *dte* calendar days in sessions (dte × 252/365)."""
    sess = dte * TRADING_DAYS / 365.0
    return min(horizons, key=lambda h: (abs(h - sess), h))


def path_params(row: pd.Series, dte: int, levels: Sequence[float] = M1_QUANTILES) -> M1PathParams:
    """:class:`M1PathParams` for one candidate from a row of :func:`publish` output.

    σ = ``m1_rv_h`` at the horizon nearest *dte*; drift = the annualised median log
    return plus ½σ² (so the paths' median log return over h sessions is M1's median);
    ν matches the h-session quantiles' tail ratio.
    """
    h = horizon_for_dte(dte)
    q = np.array([[float(row[f"m1_q{qtag(p)}_h{h}"]) for p in levels]])
    sigma = float(row[f"m1_rv_h{h}"])
    med = float(q[0, list(levels).index(0.5)])
    return M1PathParams(
        sigma=sigma,
        drift=med / (h / TRADING_DAYS) + 0.5 * sigma * sigma,
        nu=float(fit_nu(q, levels)[0]),
        horizon=h,
    )
