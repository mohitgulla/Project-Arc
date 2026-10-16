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
from typing import TYPE_CHECKING, Literal

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
    "holm_adjust",
    "max_drawdown",
    "mde_always_valid",
    "mde_fixed",
    "mixing_tau",
    "msprt_halfwidth",
    "msprt_p_value",
    "msprt_p_values",
    "non_inferior",
    "sample_sd",
    "seed_for",
    "sessions_needed",
    "sortino",
    "sortino_diff_ci",
    "sortino_diff_p",
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


def msprt_p_value(n: int, mean: float, sigma: float, tau: float, theta0: float = 0.0) -> float:
    """Always-valid p-value of ``theta = theta0`` after *n* sessions: ``min(1, 1/Lambda_n)``.

    The dual of :func:`msprt_halfwidth` with the same sigma and tau: ``p < alpha``
    exactly when the always-valid CI ``mean +- halfwidth`` excludes *theta0*, so the
    p shown next to a CI always agrees with it.
    """
    s2, t2 = sigma**2, tau**2
    v = s2 + n * t2
    log_lam = 0.5 * math.log(s2 / v) + (n**2 * t2 * (mean - theta0) ** 2) / (2.0 * s2 * v)
    return 1.0 if log_lam <= 0.0 else math.exp(-log_lam)


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
    diffs = _boot_diffs(t, c, resamples=resamples, seed=seed, floor=floor)
    if diffs is None:
        return None
    tail = (1.0 - level) / 2.0
    est = (sortino(t, floor=floor) or 0.0) - (sortino(c, floor=floor) or 0.0)
    return Interval(
        estimate=est,
        lo=float(np.quantile(diffs, tail)),
        hi=float(np.quantile(diffs, 1.0 - tail)),
        level=level,
    )


def _boot_diffs(
    t: NDArray[np.float64], c: NDArray[np.float64], *, resamples: int, seed: int, floor: float
) -> NDArray[np.float64] | None:
    if t.size != c.size:
        msg = f"paired series differ in length: {t.size} vs {c.size}"
        raise ValueError(msg)
    if t.size < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, t.size, size=(resamples, t.size))
    out: NDArray[np.float64] = _sortino_rows(t[idx], floor) - _sortino_rows(c[idx], floor)
    return out


def sortino_diff_p(
    treat: ArrayLike,
    ctrl: ArrayLike,
    *,
    margin: float | None,
    resamples: int,
    seed: int,
    floor: float = DOWNSIDE_FLOOR,
) -> float | None:
    """Bootstrap p-value of the Sortino difference, from the same resamples as the CI.

    With a *margin* (A/B): one-sided non-inferiority, ``H0: diff <= -margin``, so
    ``p < alpha`` matches the level ``1 - 2 alpha`` CI's lower bound clearing
    ``-margin``. Without one (A/A): two-sided against 0. ``None`` below 2 sessions.
    """
    diffs = _boot_diffs(
        np.asarray(treat, dtype=float),
        np.asarray(ctrl, dtype=float),
        resamples=resamples,
        seed=seed,
        floor=floor,
    )
    if diffs is None:
        return None
    if margin is not None:
        return float(np.mean(diffs <= -margin))
    return float(min(1.0, 2.0 * min(np.mean(diffs <= 0.0), np.mean(diffs >= 0.0))))


def non_inferior(ci: Interval | None, margin: float) -> bool:
    """Treatment is non-inferior when the CI's lower bound is above ``-margin``."""
    return ci is not None and ci.lo > -margin


# ---------------------------------------------------------------------------
# D69 (E15.5): multiple treatments
# ---------------------------------------------------------------------------


def holm_adjust(p: ArrayLike) -> NDArray[np.float64]:
    """Holm step-down adjusted p-values along the last axis (Holm 1979).

    For sorted ``p_(1) <= ... <= p_(m)``: ``adj_(i) = max_{j <= i} min(1, (m - j + 1)
    p_(j))``, returned in the input order. Rejecting every ``adj < alpha`` controls
    the familywise error at ``alpha`` under any dependence between the tests (the
    K Control-vs-Ti pairs share control, so they are positively correlated). With
    always-valid p-values the bound holds at every daily look: a true null is first
    rejected only when its p falls below ``alpha / m0`` (``m0`` = true nulls), and
    each always-valid p does that with probability <= ``alpha / m0`` over the whole
    run. ``m = 1`` returns *p* unchanged (the one-treatment test).
    """
    a = np.asarray(p, dtype=float)
    if a.shape[-1] == 0:
        return a.copy()
    m = a.shape[-1]
    order = np.argsort(a, axis=-1, kind="stable")
    ranked = np.take_along_axis(a, order, axis=-1)
    factors = np.arange(m, 0, -1, dtype=float)
    stepped = np.maximum.accumulate(np.minimum(1.0, ranked * factors), axis=-1)
    out = np.empty_like(stepped)
    np.put_along_axis(out, order, stepped, axis=-1)
    return out


def msprt_p_values(
    n: ArrayLike, mean: ArrayLike, sigma: ArrayLike, tau: ArrayLike
) -> NDArray[np.float64]:
    """Vectorised :func:`msprt_p_value` (``theta0 = 0``); equal to it element-wise."""
    n_ = np.asarray(n, dtype=float)
    s2 = np.asarray(sigma, dtype=float) ** 2
    t2 = np.asarray(tau, dtype=float) ** 2
    v = s2 + n_ * t2
    m = np.asarray(mean, dtype=float)
    log_lam = 0.5 * np.log(s2 / v) + (n_**2 * t2 * m**2) / (2.0 * s2 * v)
    out: NDArray[np.float64] = np.where(log_lam <= 0.0, 1.0, np.exp(-np.maximum(log_lam, 0.0)))
    return out


def sessions_needed(
    sigma: float,
    effect: float,
    *,
    tau: float,
    alpha: float,
    power: float,
    k: int = 1,
    cap: int = 1000,
) -> int | None:
    """Smallest session count whose always-valid MDE at level ``alpha / k`` is <= *effect*.

    ``alpha / k`` is Holm's first (strictest) step across *k* treatments, so the count
    is what one arm needs in the worst case; every extra arm lengthens the run.
    ``None`` when *cap* sessions are not enough.
    """
    level = alpha / k
    for n in range(1, cap + 1):
        if mde_always_valid(sigma, n, tau=tau, alpha=level, power=power) <= effect:
            return n
    return None


def max_drawdown(equity: ArrayLike) -> float:
    """Largest peak-to-trough fall of an equity curve, as a fraction of the peak (>= 0)."""
    a = np.asarray(equity, dtype=float)
    if a.size == 0:
        return 0.0
    peak = np.maximum.accumulate(a)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - a) / peak, 0.0)
    return float(dd.max())


# ---------------------------------------------------------------------------
# D119 (E25.7): pre-registered SPY regression adjustment + HAC long-run sigma
# ---------------------------------------------------------------------------
#
# Adjustment. ``d_t = a + b x_t + e_t`` by OLS on the running data (x_t = SPY's daily
# return); the estimand is the intercept ``a = mean(d) - b mean(x)``: the treatment's
# daily edge on a flat-market day. Its plug-in variance carries the extra
# ``1 + n mean(x)^2 / Sxx`` factor from estimating ``b``.
#
# Long-run sigma. 10-60 DTE structures carry P&L across sessions, so ``e_t`` is
# autocorrelated and the iid mSPRT CI is too narrow. The residual sigma is scaled by
# ``sqrt(f)``, the Newey-West / Bartlett long-run variance factor
# ``f = 1 + 2 sum_{k<=L} (1 - k/(L+1)) rho_k`` (floored at 1: the correction may only
# widen the CI). Two small-sample corrections keep the always-valid CI calibrated
# with daily peeking over 20-60 sessions (``tests/test_experiment_hac.py`` simulates
# AR(1) residuals with rho in {0, 0.3, 0.5}):
#
# - ``rho_k`` is bias-corrected: the sample autocorrelation of a short regression
#   residual is biased towards 0 (about ``-(1 + 3 rho)/n`` at lag 1, ``-1/n`` beyond);
#   :func:`corrected_autocorrelations` adds that back;
# - ``f`` is at least the AR(1)-implied factor ``(1 + rho_1) / (1 - rho_1)``
#   (Andrews-Monahan style; a lag-L Bartlett sum under-states a persistent series).
#
# Below ``2L + 2`` sessions the residual autocorrelations are noise, so ``f`` is the
# A/A's recorded factor when there is one, else ``hac_max_factor`` (the cap) outright.

#: D119: the lag-1 bias-corrected autocorrelation is clipped here before the AR(1)
#: floor ``(1 + rho) / (1 - rho)`` (19x at most), so a near-unit-root series stays finite.
AR1_RHO_MAX = 0.9

HacSource = Literal["iid", "running", "aa", "cap"]
SigmaSource = Literal["aa", "running_corrected"]


class Fit(BaseModel):
    """The OLS fit of one paired series (or its plain mean when unadjusted)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int
    estimate: float = Field(..., description="Intercept a (adjusted) or mean(d)")
    adjusted: bool
    beta: float | None = None
    beta_se: float | None = None
    betas: list[float] | None = Field(default=None, description="D120: every b (k > 1)")
    betas_se: list[float] | None = Field(default=None)
    r2: float | None = None
    df: int = Field(..., description="Residual degrees of freedom (n - 1 - k adjusted, n - 1)")
    s_e: float = Field(..., description="Plug-in residual sd sqrt(SSE / df)")
    intercept_factor: float = Field(
        1.0, description="sqrt(1 + n mean(x)^2 / Sxx): the intercept's extra sd (1 = mean)"
    )
    resid: list[float] = Field(default_factory=list)


def ols_fit(d: ArrayLike, x: ArrayLike | None) -> Fit | None:
    """Fit ``d = a + b x`` (or the mean when *x* is None, ``n < 3`` or ``Sxx = 0``).

    D120 (E25.9): *x* may be ``(n, k)`` for k covariates, ``d = a + b_1 x_1 + ... +
    b_k x_k``. k = 1 (a 1-D *x* or one column) is the D119 closed form, unchanged;
    k > 1 solves the normal equations and reads the intercept's variance from
    ``(X'X)^-1`` (:func:`_ols_matrix`), falling back to the mean below ``k + 2``
    sessions or on a rank-deficient design. ``None`` below 2 sessions (no residual
    degree of freedom).
    """
    dd = np.asarray(d, dtype=float)
    n = dd.size
    if n < 2:
        return None
    if x is not None:
        x2 = np.asarray(x, dtype=float)
        if x2.ndim == 2 and x2.shape[1] == 1:
            x = x2[:, 0]
        elif x2.ndim == 2:
            if x2.shape[0] != n:
                msg = f"d and x differ in length: {n} vs {x2.shape[0]}"
                raise ValueError(msg)
            fit = _ols_matrix(dd, x2)
            return fit if fit is not None else ols_fit(dd, None)
    if x is not None and n >= 3:
        xx = np.asarray(x, dtype=float)
        if xx.size != n:
            msg = f"d and x differ in length: {n} vs {xx.size}"
            raise ValueError(msg)
        xm, dm = float(xx.mean()), float(dd.mean())
        sxx = float(np.sum((xx - xm) ** 2))
        if sxx > 0.0:
            b = float(np.sum((xx - xm) * (dd - dm)) / sxx)
            a = dm - b * xm
            e = dd - a - b * xx
            sse = float(np.sum(e**2))
            sst = float(np.sum((dd - dm) ** 2))
            df = n - 2
            s_e = math.sqrt(sse / df)
            return Fit(
                n=n,
                estimate=a,
                adjusted=True,
                beta=b,
                beta_se=s_e / math.sqrt(sxx),
                r2=1.0 - sse / sst if sst > 0.0 else 0.0,
                df=df,
                s_e=s_e,
                intercept_factor=math.sqrt(1.0 + n * xm * xm / sxx),
                resid=e.tolist(),
            )
    m = float(dd.mean())
    e = dd - m
    return Fit(
        n=n,
        estimate=m,
        adjusted=False,
        df=n - 1,
        s_e=math.sqrt(float(np.sum(e**2)) / (n - 1)),
        intercept_factor=1.0,
        resid=e.tolist(),
    )


def _ols_matrix(d: NDArray[np.float64], x: NDArray[np.float64]) -> Fit | None:
    """OLS of *d* on ``[1, x]`` (``x``: ``(n, k)``); None below ``k + 2`` or rank-deficient.

    ``intercept_factor = sqrt(n (X'X)^-1_00)``, so ``sigma * intercept_factor / sqrt(n)``
    is the intercept's standard error (for k = 1 it equals ``sqrt(1 + n mean(x)^2 /
    Sxx)``). ``beta`` / ``beta_se`` are the first covariate's; ``betas`` / ``betas_se``
    all k.
    """
    n, k = x.shape
    df = n - 1 - k
    if df < 1:
        return None
    design = np.column_stack([np.ones(n), x])
    xtx = design.T @ design
    if np.linalg.matrix_rank(design) < k + 1:
        return None
    inv = np.linalg.inv(xtx)
    coef = inv @ (design.T @ d)
    e = d - design @ coef
    sse = float(np.sum(e**2))
    sst = float(np.sum((d - d.mean()) ** 2))
    s_e = math.sqrt(sse / df)
    se = [s_e * math.sqrt(max(float(inv[j, j]), 0.0)) for j in range(1, k + 1)]
    return Fit(
        n=n,
        estimate=float(coef[0]),
        adjusted=True,
        beta=float(coef[1]),
        beta_se=se[0],
        betas=[float(c) for c in coef[1:]],
        betas_se=se,
        r2=1.0 - sse / sst if sst > 0.0 else 0.0,
        df=df,
        s_e=s_e,
        intercept_factor=math.sqrt(max(n * float(inv[0, 0]), 0.0)),
        resid=e.tolist(),
    )


def autocorrelations(e: ArrayLike, lag: int) -> list[float]:
    """Sample autocorrelations ``rho_1..rho_lag`` of a (mean-zero) residual series.

    ``rho_k = sum_t e_t e_{t-k} / sum_t e_t^2``; 0.0 for a lag >= n or a zero series.
    """
    a = np.asarray(e, dtype=float)
    den = float(np.sum(a * a))
    out: list[float] = []
    for k in range(1, lag + 1):
        if k >= a.size or den <= 0.0:
            out.append(0.0)
            continue
        out.append(float(np.sum(a[k:] * a[:-k]) / den))
    return out


def bartlett_factor(rho: Sequence[float], lag: int) -> float:
    """Newey-West long-run variance factor ``max(1, 1 + 2 sum (1 - k/(L+1)) rho_k)``."""
    acc = sum((1.0 - k / (lag + 1)) * r for k, r in enumerate(rho[:lag], start=1))
    return max(1.0, 1.0 + 2.0 * acc)


def corrected_autocorrelations(rho: Sequence[float], n: int) -> list[float]:
    """Small-sample bias-corrected ``rho_k``: ``+ (1 + 3 max(rho_1, 0)) / n`` at lag 1,
    ``+ 1 / n`` beyond (the residual autocorrelation's downward bias), capped below 1."""
    out: list[float] = []
    for k, r in enumerate(rho, start=1):
        bias = (1.0 + 3.0 * max(r, 0.0)) / n if k == 1 else 1.0 / n
        out.append(min(r + bias, 0.99))
    return out


def running_hac_factor(rho: Sequence[float], n: int, lag: int) -> float:
    """The long-run factor from the series itself (D119): Bartlett on the bias-corrected
    autocorrelations, at least the AR(1)-implied ``(1 + rho_1) / (1 - rho_1)``, >= 1."""
    rc = corrected_autocorrelations(rho, n)
    f = bartlett_factor(rc, lag)
    r1 = min(max(rc[0], 0.0), AR1_RHO_MAX) if rc else 0.0
    return max(f, (1.0 + r1) / (1.0 - r1))


class LongRun(BaseModel):
    """The D119 primary of one paired series: estimate, long-run sigma, CI, p."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fit: Fit
    rho: list[float] = Field(default_factory=list, description="Raw residual rho_1..rho_L")
    factor_running: float | None = Field(None, description="running_hac_factor (n >= 2)")
    factor: float = Field(1.0, description="f used (>= 1)")
    hac_source: HacSource
    sigma_base: float = Field(..., description="A/A sigma, or s_e inflated to its chi2 bound")
    sigma_source: SigmaSource
    sigma_lr: float = Field(..., description="sigma_base * sqrt(f) * intercept factor")
    tau: float
    ci: Interval
    p_value: float


def long_run_primary(
    d: ArrayLike,
    x: ArrayLike | None,
    *,
    hac_lag: int | None,
    alpha: float,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
    max_factor: float,
    aa_sigma: float | None = None,
    aa_factor: float | None = None,
) -> LongRun | None:
    """The always-valid CI and p of the D119 estimand (pure; no clock, no network).

    *x* = SPY returns (None = unadjusted: the mean of *d*). *aa_sigma* = the A/A sigma
    on the same footing (adjusted residual sigma for an adjusted fit, else raw), and
    *aa_factor* its recorded long-run factor; without an A/A sigma the plug-in ``s_e``
    is inflated to its ``(1 - sigma_upper_q)`` chi-square upper bound on ``df``
    degrees of freedom. ``f``: 1 when *hac_lag* is None; the A/A's when it has both;
    else :func:`running_hac_factor` from ``2L + 2`` sessions on and before that the
    A/A's factor or *max_factor*. The mSPRT then runs unchanged on
    ``(estimate, sigma_lr)``. ``None`` while no CI can be formed.
    """
    fit = ols_fit(d, x)
    if fit is None:
        return None
    n = fit.n
    source: SigmaSource
    if aa_sigma is not None:
        base, source = aa_sigma, "aa"
    else:
        if fit.s_e <= 0.0:
            return None
        base = fit.s_e * math.sqrt(fit.df / _sps.chi2.ppf(sigma_upper_q, fit.df))
        source = "running_corrected"
    rho: list[float] = []
    f_run: float | None = None
    hac: HacSource
    if hac_lag is None:
        f, hac = 1.0, "iid"
    else:
        rho = autocorrelations(fit.resid, hac_lag)
        f_run = running_hac_factor(rho, n, hac_lag)
        if aa_sigma is not None and aa_factor is not None:
            f, hac = max(1.0, aa_factor), "aa"
        elif n >= 2 * hac_lag + 2:
            f, hac = f_run, "running"
        elif aa_factor is not None:
            f, hac = max(1.0, aa_factor), "aa"
        else:
            f, hac = max(1.0, max_factor), "cap"
    sigma_lr = base * math.sqrt(f) * fit.intercept_factor
    if sigma_lr <= 0.0:
        return None
    tau = mixing_tau(sigma_lr, mde=mde, min_sessions=min_sessions)
    hw = float(msprt_halfwidth(n, sigma_lr, tau, alpha))
    est = fit.estimate
    return LongRun(
        fit=fit,
        rho=rho,
        factor_running=f_run,
        factor=f,
        hac_source=hac,
        sigma_base=base,
        sigma_source=source,
        sigma_lr=sigma_lr,
        tau=tau,
        ci=Interval(estimate=est, lo=est - hw, hi=est + hw, level=1.0 - alpha),
        p_value=msprt_p_value(n, est, sigma_lr, tau),
    )


def pooled_hac(
    resids: Sequence[Sequence[float]], dfs: Sequence[int], lag: int
) -> tuple[float | None, float | None, list[float]]:
    """A/A calibration across pairs: (pooled residual sigma, pooled long-run factor, rho).

    sigma = ``sqrt(sum SSE_i / sum df_i)``; rho_k pools the lag-k cross products over
    the pairs (``sum_i sum_t e e_{t-k} / sum_i sum_t e^2``), and the factor is
    :func:`running_hac_factor` at the mean series length. ``(None, None, [])`` when no
    pair has a residual degree of freedom.
    """
    sse = sum(float(np.sum(np.asarray(e, dtype=float) ** 2)) for e in resids)
    df = sum(dfs)
    if df <= 0 or not resids or sse <= 0.0:
        return None, None, []
    sigma = math.sqrt(sse / df)
    rho: list[float] = []
    for k in range(1, lag + 1):
        num = 0.0
        for e in resids:
            a = np.asarray(e, dtype=float)
            if k < a.size:
                num += float(np.sum(a[k:] * a[:-k]))
        rho.append(num / sse)
    n_mean = max(2, round(sum(len(e) for e in resids) / len(resids)))
    return sigma, running_hac_factor(rho, n_mean, lag), rho
