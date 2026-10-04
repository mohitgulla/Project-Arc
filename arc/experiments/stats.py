"""Experiment statistics (PLAN D44, card E10.3): pure functions, no DB, no clock.

Primary metric: the paired daily difference ``d_t = (treat_pnl_t - ctrl_pnl_t) /
t0_equity`` (a fraction of t0 equity). Its mean is tested with an **always-valid
confidence sequence** from the normal-mixture mSPRT (Robbins 1970; Johari et al.
2017), so the CI may be looked at every day without inflating the error rate.

For iid ``d_t ~ N(theta, sigma^2)`` and a ``N(0, tau^2)`` mixing prior on
``theta - theta0``, the mixture likelihood ratio after ``n`` sessions is::

    Lambda_n(theta0) = sqrt(sigma^2 / (sigma^2 + n tau^2))
                       * exp(n^2 tau^2 (mean_n - theta0)^2 / (2 sigma^2 (sigma^2 + n tau^2)))

and by Ville's inequality ``P(exists n: Lambda_n(theta) >= 1/alpha) <= alpha``. The
set ``{theta0: Lambda_n(theta0) < 1/alpha}`` is the two-sided level-``alpha``
confidence sequence ``mean_n +- halfwidth_n`` (:func:`msprt_halfwidth`).

**sigma.** The A/A estimate (E10.4) when one is on record; otherwise the running
sample standard deviation inflated to its one-sided ``(1 - sigma_upper_q)`` upper
chi-square confidence bound (:func:`corrected_sigma`). The plug-in estimate alone
under-covers at small ``n`` (a small ``s_n`` narrows the CI exactly when the
mean is noisy); the upper bound restores coverage. ``tests/test_experiment_stats.py``
checks the type-I error with daily peeking by simulation for both modes.

**tau** (mixing scale): the spec's ``mde`` when set, else the fixed-horizon MDE at
``min_sessions`` (``2.8 sigma / sqrt(min_sessions)``): the mixture is tuned to the
effect sizes the experiment can detect.

Secondary: annualised Sortino per arm and a paired bootstrap CI of the difference
(deterministic: a seeded generator) for the non-inferiority check.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats as _sps

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import ArrayLike, NDArray

__all__ = [
    "MDE_Z",
    "TRADING_DAYS",
    "Interval",
    "confidence_sequence",
    "corrected_sigma",
    "max_drawdown",
    "mde_always_valid",
    "mde_fixed",
    "mixing_tau",
    "msprt_halfwidth",
    "non_inferior",
    "sample_sd",
    "seed_for",
    "sortino",
    "sortino_diff_ci",
]

# 2.8 = z_{1-0.05/2} + z_{0.8} (1.96 + 0.84): the fixed-horizon MDE factor at the
# D44 defaults, the number the card and the A/A report quote.
MDE_Z = 2.8
TRADING_DAYS = 252
# Downside deviation floor (fraction of equity per day) so an arm with no losing
# day has a finite Sortino: 1 bp/day.
DOWNSIDE_FLOOR = 1e-4


class Interval(BaseModel):
    """A confidence interval ``[lo, hi]`` around ``estimate`` (fractions)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    estimate: float
    lo: float
    hi: float
    level: float = Field(..., gt=0.0, lt=1.0, description="Coverage, e.g. 0.95")

    @property
    def excludes_zero(self) -> bool:
        return self.lo > 0.0 or self.hi < 0.0


def msprt_halfwidth(
    n: ArrayLike, sigma: ArrayLike, tau: ArrayLike, alpha: float
) -> NDArray[np.float64]:
    """Half-width of the always-valid two-sided CI on the mean after *n* sessions.

    Broadcasts over arrays. ``n >= 1``, ``sigma > 0``, ``tau > 0``, ``0 < alpha < 1``.
    """
    n_ = np.asarray(n, dtype=float)
    s2 = np.asarray(sigma, dtype=float) ** 2
    t2 = np.asarray(tau, dtype=float) ** 2
    v = s2 + n_ * t2
    out: NDArray[np.float64] = np.sqrt(
        2.0 * s2 * v / (n_**2 * t2) * (math.log(1.0 / alpha) + 0.5 * np.log(v / s2))
    )
    return out


def sample_sd(x: ArrayLike) -> float:
    """Sample standard deviation (``ddof=1``); 0.0 for fewer than two values."""
    a = np.asarray(x, dtype=float)
    return float(a.std(ddof=1)) if a.size >= 2 else 0.0


def corrected_sigma(x: ArrayLike, q: float) -> float | None:
    """Running sigma inflated to its one-sided ``1 - q`` upper chi-square bound.

    ``s * sqrt((n - 1) / chi2.ppf(q, n - 1))``; ``None`` for fewer than two values
    or a zero spread (no CI can be formed yet).
    """
    a = np.asarray(x, dtype=float)
    n = a.size
    s = sample_sd(a)
    if n < 2 or s <= 0.0:
        return None
    return float(s * math.sqrt((n - 1) / _sps.chi2.ppf(q, n - 1)))


def mde_fixed(sigma: float, n: int, z: float = MDE_Z) -> float:
    """Fixed-horizon minimum detectable effect: ``z * sigma / sqrt(n)`` (card formula)."""
    return z * sigma / math.sqrt(n)


def mixing_tau(sigma: float, *, mde: float | None, min_sessions: int) -> float:
    """The mixture scale: the spec's ``mde`` if set, else the fixed MDE at ``min_sessions``."""
    return mde if mde is not None else mde_fixed(sigma, min_sessions)


def mde_always_valid(sigma: float, n: int, *, tau: float, alpha: float, power: float) -> float:
    """Effect the always-valid test detects by session *n* with probability >= *power*.

    The CI half-width at *n* plus ``z_power * sigma / sqrt(n)``: at that shift the
    lower bound is above 0 at *n* with probability *power* (peeking before *n*
    only adds chances). Larger than :func:`mde_fixed`: the price of daily peeking.
    """
    hw = float(msprt_halfwidth(n, sigma, tau, alpha))
    return hw + float(_sps.norm.ppf(power)) * sigma / math.sqrt(n)


def confidence_sequence(
    d: Sequence[float] | NDArray[np.float64],
    *,
    alpha: float,
    sigma: float | None,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
) -> tuple[Interval | None, float | None, float | None]:
    """The always-valid CI on ``mean(d)`` now, plus the sigma and tau it used.

    *sigma* = the A/A estimate, or ``None`` to use :func:`corrected_sigma` of *d*.
    Returns ``(None, sigma, tau)`` when no CI can be formed (too few sessions, or
    zero spread with no A/A sigma).
    """
    a = np.asarray(d, dtype=float)
    n = a.size
    sig = sigma if sigma is not None else corrected_sigma(a, sigma_upper_q)
    if n == 0 or sig is None or sig <= 0.0:
        return None, sig, None
    tau = mixing_tau(sig, mde=mde, min_sessions=min_sessions)
    hw = float(msprt_halfwidth(n, sig, tau, alpha))
    m = float(a.mean())
    return Interval(estimate=m, lo=m - hw, hi=m + hw, level=1.0 - alpha), sig, tau


def sortino(r: ArrayLike, *, floor: float = DOWNSIDE_FLOOR) -> float | None:
    """Annualised Sortino of daily returns *r* (target 0); ``None`` when *r* is empty.

    ``mean(r) / max(sqrt(mean(min(r, 0)^2)), floor) * sqrt(252)``.
    """
    a = np.asarray(r, dtype=float)
    if a.size == 0:
        return None
    dd = math.sqrt(float(np.mean(np.minimum(a, 0.0) ** 2)))
    return float(a.mean() / max(dd, floor) * math.sqrt(TRADING_DAYS))


def _sortino_rows(a: NDArray[np.float64], floor: float) -> NDArray[np.float64]:
    dd = np.sqrt(np.mean(np.minimum(a, 0.0) ** 2, axis=1))
    out: NDArray[np.float64] = a.mean(axis=1) / np.maximum(dd, floor) * math.sqrt(TRADING_DAYS)
    return out


def seed_for(*parts: object) -> int:
    """A stable 32-bit seed from *parts* (e.g. experiment id and session count)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:4], "big")


def sortino_diff_ci(
    treat: ArrayLike,
    ctrl: ArrayLike,
    *,
    level: float,
    resamples: int,
    seed: int,
    floor: float = DOWNSIDE_FLOOR,
) -> Interval | None:
    """Paired percentile-bootstrap CI of ``sortino(treat) - sortino(ctrl)``.

    Sessions are resampled in pairs (keeps the day-to-day correlation). Seeded,
    so the same inputs always give the same interval. ``None`` below 2 sessions.
    """
    t = np.asarray(treat, dtype=float)
    c = np.asarray(ctrl, dtype=float)
    if t.size != c.size:
        msg = f"paired series differ in length: {t.size} vs {c.size}"
        raise ValueError(msg)
    if t.size < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, t.size, size=(resamples, t.size))
    diffs = _sortino_rows(t[idx], floor) - _sortino_rows(c[idx], floor)
    tail = (1.0 - level) / 2.0
    est = (sortino(t, floor=floor) or 0.0) - (sortino(c, floor=floor) or 0.0)
    return Interval(
        estimate=est,
        lo=float(np.quantile(diffs, tail)),
        hi=float(np.quantile(diffs, 1.0 - tail)),
        level=level,
    )


def non_inferior(ci: Interval | None, margin: float) -> bool:
    """Treatment is non-inferior when the CI's lower bound is above ``-margin``."""
    return ci is not None and ci.lo > -margin


def max_drawdown(equity: ArrayLike) -> float:
    """Largest peak-to-trough fall of an equity curve, as a fraction of the peak (>= 0)."""
    a = np.asarray(equity, dtype=float)
    if a.size == 0:
        return 0.0
    peak = np.maximum.accumulate(a)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - a) / peak, 0.0)
    return float(dd.max())
