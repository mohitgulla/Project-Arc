"""Isotonic calibration with JSON breakpoints (E27.3, D126). Pure numpy.

:func:`fit_isotonic` is the pool-adjacent-violators fit of a non-decreasing step
function ``score → P(y = 1)`` (weighted). The calibrator stores only its breakpoints
(``x`` = block right edges in score space, ``y`` = block means), so it serialises to
JSON and :meth:`Isotonic.predict` is a monotone piecewise-linear interpolation between
block centres, clipped to ``[eps, 1 − eps]``.

:class:`FamilyCalibrator` holds one family calibrator plus optional per-kind ones,
each shrunk toward the family: ``p = λ · p_kind + (1 − λ) · p_family`` with
``λ = n_eff / (n_eff + SHRINK_K)``. A per-kind calibrator is fitted only when that
kind's ECE under the family calibrator is above ``2 ×`` the family's ECE and above the
sampling-noise floor of a calibrated kind of that size (:func:`noise_floor`).
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

EPS = 1e-4
SHRINK_K = 2000.0
#: Per-kind calibrator trigger: kind ECE > this × the family ECE.
KIND_ECE_RATIO = 2.0


class Isotonic(BaseModel):
    """A fitted non-decreasing calibration map (breakpoints only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: list[float] = Field(..., description="Block centres in score space, increasing")
    y: list[float] = Field(..., description="Calibrated probability per block, non-decreasing")
    n_eff: float = Field(0.0, description="Σ weights the map was fitted on")

    def predict(self, s: np.ndarray) -> np.ndarray:
        s = np.asarray(s, dtype=float)
        if not self.x:
            return np.full(s.shape, 0.5)
        out = np.interp(s, self.x, self.y, left=self.y[0], right=self.y[-1])
        out = np.where(np.isfinite(s), out, float(np.mean(self.y)))
        return np.clip(out, EPS, 1.0 - EPS)


def fit_isotonic(s: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> Isotonic:
    """Weighted pool-adjacent-violators on (score, outcome)."""
    s = np.asarray(s, dtype=float)
    y = np.asarray(y, dtype=float)
    w = np.ones(len(s)) if w is None else np.asarray(w, dtype=float)
    ok = np.isfinite(s) & np.isfinite(y) & (w > 0)
    s, y, w = s[ok], y[ok], w[ok]
    if len(s) == 0:
        return Isotonic(x=[], y=[], n_eff=0.0)
    order = np.lexsort((y, s))
    s, y, w = s[order], y[order], w[order]
    # collapse equal scores first (one block per distinct score)
    us, start = np.unique(s, return_index=True)
    ws = np.add.reduceat(w, start)
    ys = np.add.reduceat(w * y, start) / ws
    xs = np.add.reduceat(w * s, start) / ws
    blocks_y: list[float] = []
    blocks_w: list[float] = []
    blocks_x: list[float] = []
    for yi, wi, xi in zip(ys, ws, xs, strict=True):
        blocks_y.append(float(yi))
        blocks_w.append(float(wi))
        blocks_x.append(float(xi) * float(wi))
        while len(blocks_y) > 1 and blocks_y[-2] > blocks_y[-1]:
            y2, w2, x2 = blocks_y.pop(), blocks_w.pop(), blocks_x.pop()
            y1, w1, x1 = blocks_y.pop(), blocks_w.pop(), blocks_x.pop()
            wt = w1 + w2
            blocks_y.append((y1 * w1 + y2 * w2) / wt)
            blocks_w.append(wt)
            blocks_x.append(x1 + x2)
    del us
    cx = [bx / bw for bx, bw in zip(blocks_x, blocks_w, strict=True)]
    return Isotonic(
        x=[round(v, 10) for v in cx],
        y=[round(min(max(v, EPS), 1.0 - EPS), 10) for v in blocks_y],
        n_eff=round(float(w.sum()), 6),
    )


class FamilyCalibrator(BaseModel):
    """Family isotonic map + shrunk per-kind maps (JSON-serialisable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Isotonic
    kinds: dict[str, Isotonic] = Field(default_factory=dict)
    shrink_k: float = SHRINK_K

    def predict(self, s: np.ndarray, kinds: np.ndarray | None = None) -> np.ndarray:
        base = self.family.predict(s)
        if kinds is None or not self.kinds:
            return base
        out = base.copy()
        k = np.asarray(kinds).astype(str)
        for name, cal in self.kinds.items():
            m = k == name
            if m.any():
                lam = cal.n_eff / (cal.n_eff + self.shrink_k)
                out[m] = lam * cal.predict(np.asarray(s)[m]) + (1.0 - lam) * base[m]
        return np.clip(out, EPS, 1.0 - EPS)


def noise_floor(w: np.ndarray, bins: int = 10) -> float:
    """``sqrt(0.25 · bins / n_eff)`` with Kish ``n_eff`` (∞ rows → 0)."""
    w = np.asarray(w, dtype=float)
    s2 = float(np.sum(w * w))
    n_eff = float(np.sum(w)) ** 2 / s2 if s2 > 0 else 0.0
    return float(np.sqrt(0.25 * bins / n_eff)) if n_eff > 0 else float("inf")


def fit_family(
    s: np.ndarray,
    y: np.ndarray,
    w: np.ndarray | None,
    kinds: np.ndarray,
    *,
    kind_ece_ratio: float = KIND_ECE_RATIO,
    shrink_k: float = SHRINK_K,
) -> tuple[FamilyCalibrator, dict[str, dict[str, float]]]:
    """Fit the family map, check every kind's ECE, add per-kind maps where it is
    > *kind_ece_ratio* × the family ECE.

    Returns the calibrator and a ``kind -> {ece_family_map, ece_family, per_kind}``
    report (``per_kind`` 1.0 when a kind map was fitted).
    """
    from arc.ml.validate import ece

    w = np.ones(len(s)) if w is None else np.asarray(w, dtype=float)
    fam = fit_isotonic(s, y, w)
    p = fam.predict(s)
    e_fam = ece(y, p, w)
    k = np.asarray(kinds).astype(str)
    maps: dict[str, Isotonic] = {}
    rep: dict[str, dict[str, float]] = {}
    for name in sorted(set(k)):
        m = k == name
        e_k = ece(y[m], p[m], w[m])
        # sampling-noise floor: a calibrated kind with n_eff rows still shows ECE of order
        # the per-bin binomial s.d. sqrt(0.25 · bins / n_eff); the family map is fitted
        # in-sample so its own ECE is near 0 and "2×" alone would fire on noise.
        floor = noise_floor(w[m])
        fit = bool(
            np.isfinite(e_k) and np.isfinite(e_fam) and e_k > max(kind_ece_ratio * e_fam, floor)
        )
        if fit:
            maps[name] = fit_isotonic(np.asarray(s)[m], np.asarray(y)[m], w[m])
        rep[name] = {
            "ece_under_family_map": e_k,
            "ece_family": e_fam,
            "noise_floor": floor,
            "per_kind": float(fit),
        }
    return FamilyCalibrator(family=fam, kinds=maps, shrink_k=shrink_k), rep
