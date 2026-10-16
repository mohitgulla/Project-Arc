"""D120 (E25.9): the descriptive ΔVIX covariate check (pure; no DB, no clock, no network).

Question: does adding ΔVIX_t (VIX close-to-close, vol points) to the D119 adjustment
``d_t = a + b1 SPY_ret_t`` explain enough of the paired difference to pay for its
degree of freedom? Per Ti − C pair, on the sessions that have **both** covariates
(the D117 partial first session has no ΔVIX and is left out of both fits, so the
two models compare on the same sample):

- ``sigma_spy`` / ``sigma_spy_vix``: plug-in residual sd ``sqrt(SSE / df)`` of the
  SPY-only and the SPY + ΔVIX fit;
- ``partial_f``: the partial F of ΔVIX given SPY, ``(SSE_r - SSE_f) / (SSE_f / df_f)``,
  divided by the long-run factor ``f`` the primary would use for the full model now
  (:func:`arc.experiments.stats.long_run_primary`: the cap below 2L+2 sessions, the
  running Bartlett factor from there), so autocorrelated residuals cannot make it
  look significant; ``partial_f_iid`` is the unscaled F. ``partial_p`` is
  ``1 - F_cdf(partial_f; q, df_f)``;
- ``b_vix`` ± ``b_vix_se`` (iid SE × sqrt(f));
- ``ci_halfwidth_spy`` / ``ci_halfwidth_spy_vix``: the always-valid (mSPRT) CI
  half-width at the planning ``plan_n`` (20) sessions with each model's running
  sigma (chi² upper bound on its own df at ``plan_n``: ``plan_n - 2`` vs
  ``plan_n - 3``, the df loss), its long-run factor and intercept factor;
  ``ci_shrink = 1 - hw_spy_vix / hw_spy``.

Pooled over the pairs (:func:`pooled_check`): SSE and df summed like
:func:`arc.experiments.stats.pooled_hac`; the joint partial F has one restriction per
pair. The pre-registered rule (:func:`rule_result`, PLAN D120) is fixed here and
never feeds a verdict.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats as _sps

from arc.experiments import stats

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MIN_CI_SHRINK",
    "PARTIAL_P_MAX",
    "PLAN_N",
    "CovariateCheck",
    "RuleResult",
    "covariate_check",
    "pooled_check",
    "rule_result",
]

#: D120 pre-registered rule: the planning horizon of the CI comparison (sessions).
PLAN_N = 20
#: D120 rule (1): the pooled partial-F p for ΔVIX given SPY must be below this.
PARTIAL_P_MAX = 0.05
#: D120 rule (2): the always-valid CI half-width at PLAN_N must shrink at least this much.
MIN_CI_SHRINK = 0.10

_FORBID = ConfigDict(extra="forbid", frozen=True)


class CovariateCheck(BaseModel):
    """D120: SPY-only vs SPY + ΔVIX on one pair (or pooled). Descriptive, never a verdict."""

    model_config = _FORBID

    n: int = Field(..., description="Sessions with both SPY and ΔVIX (pooled: summed)")
    pairs: int = Field(1, description="Pairs pooled (1 = one pair)")
    sigma_spy: float = Field(..., description="Residual sd of d = a + b1 SPY")
    sigma_spy_vix: float = Field(..., description="Residual sd of d = a + b1 SPY + b2 ΔVIX")
    partial_f: float = Field(..., description="Partial F of ΔVIX given SPY / long-run factor")
    partial_f_iid: float = Field(..., description="Partial F, iid (unscaled)")
    partial_df: tuple[int, int] = Field(..., description="(restrictions, residual df)")
    partial_p: float
    hac_factor: float = Field(..., description="Long-run factor f of the full model (>= 1)")
    hac_source: stats.HacSource
    b_vix: float = Field(..., description="ΔVIX coefficient (fraction of t0 equity / vol pt)")
    b_vix_se: float
    plan_n: int = PLAN_N
    ci_halfwidth_spy: float = Field(..., description="Always-valid CI half-width at plan_n")
    ci_halfwidth_spy_vix: float
    ci_shrink: float = Field(..., description="1 - hw_spy_vix / hw_spy (> 0 = narrower)")


class RuleResult(BaseModel):
    """D120: the pre-registered add-or-not rule on a pooled check (rule 3 = the sim test)."""

    model_config = _FORBID

    partial_p_ok: bool
    ci_shrink_ok: bool
    recommend_add: bool = Field(..., description="(1) and (2); (3) is the committed sim")


class _Side(BaseModel):
    model_config = _FORBID

    sse: float
    df: int
    factor: float
    source: stats.HacSource
    intercept_factor: float


def _side(
    d: list[float],
    x: list[list[float]] | list[float],
    *,
    hac_lag: int | None,
    sigma_upper_q: float,
    max_factor: float,
    aa_factor: float | None,
) -> _Side | None:
    lr = stats.long_run_primary(
        d, x, hac_lag=hac_lag, alpha=0.05, sigma_upper_q=sigma_upper_q, mde=None,
        min_sessions=PLAN_N, max_factor=max_factor, aa_factor=aa_factor,
    )  # fmt: skip
    if lr is None or not lr.fit.adjusted:
        return None
    f = lr.fit
    return _Side(
        sse=f.s_e**2 * f.df,
        df=f.df,
        factor=lr.factor,
        source=lr.hac_source,
        intercept_factor=f.intercept_factor,
    )


def _halfwidth(
    sigma: float, k: int, factor: float, ifac: float, *, alpha: float, q: float,
    mde: float | None, min_sessions: int, plan_n: int,
) -> float:  # fmt: skip
    """mSPRT half-width at *plan_n* for a k-covariate fit with residual sd *sigma*."""
    df = plan_n - 1 - k
    base = sigma * math.sqrt(df / _sps.chi2.ppf(q, df))
    s = base * math.sqrt(factor) * ifac
    tau = stats.mixing_tau(s, mde=mde, min_sessions=min_sessions)
    return float(stats.msprt_halfwidth(plan_n, s, tau, alpha))


def covariate_check(
    d: Sequence[float],
    spy: Sequence[float | None],
    vix: Sequence[float | None],
    *,
    hac_lag: int | None,
    alpha: float,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
    max_factor: float,
    aa_factor: float | None = None,
    plan_n: int = PLAN_N,
) -> CovariateCheck | None:
    """The check on one paired series; None below 4 sessions with both covariates."""
    pts = [
        (float(v), float(s), float(c))
        for v, s, c in zip(d, spy, vix, strict=True)
        if s is not None and c is not None
    ]
    if len(pts) < 4:
        return None
    dd = [p[0] for p in pts]
    x1 = [p[1] for p in pts]
    x2 = [[p[1], p[2]] for p in pts]
    kw = dict(hac_lag=hac_lag, sigma_upper_q=sigma_upper_q, max_factor=max_factor,
              aa_factor=aa_factor)  # fmt: skip
    r = _side(dd, x1, **kw)  # type: ignore[arg-type]
    full = stats.ols_fit(dd, np.asarray(x2))
    f_side = _side(dd, x2, **kw)  # type: ignore[arg-type]
    if r is None or f_side is None or full is None or full.betas is None:
        return None
    assert full.betas_se is not None  # noqa: S101 - set with betas
    return _assemble(
        [r], [f_side], [(full.betas[1], full.betas_se[1])], n=len(pts), alpha=alpha,
        q=sigma_upper_q, mde=mde, min_sessions=min_sessions, plan_n=plan_n,
    )  # fmt: skip


def _assemble(
    rs: list[_Side],
    fs: list[_Side],
    bs: list[tuple[float, float]],
    *,
    n: int,
    alpha: float,
    q: float,
    mde: float | None,
    min_sessions: int,
    plan_n: int,
    factor: float | None = None,
    source: stats.HacSource | None = None,
) -> CovariateCheck | None:
    sse_r, df_r = sum(s.sse for s in rs), sum(s.df for s in rs)
    sse_f, df_f = sum(s.sse for s in fs), sum(s.df for s in fs)
    if df_r <= 0 or df_f <= 0 or sse_f <= 0.0:
        return None
    m = len(fs)
    f = factor if factor is not None else fs[0].factor
    src = source if source is not None else fs[0].source
    f_iid = max(sse_r - sse_f, 0.0) / m / (sse_f / df_f)
    f_hac = f_iid / max(f, 1.0)
    p = float(_sps.f.sf(f_hac, m, df_f))
    sig_r, sig_f = math.sqrt(sse_r / df_r), math.sqrt(sse_f / df_f)
    # intercept factors: root-mean-square over the pairs (1 pair: its own)
    ifr = math.sqrt(sum(s.intercept_factor**2 for s in rs) / len(rs))
    iff = math.sqrt(sum(s.intercept_factor**2 for s in fs) / len(fs))
    fr = factor if factor is not None else rs[0].factor
    hw = {
        "spy": _halfwidth(sig_r, 1, fr, ifr, alpha=alpha, q=q, mde=mde,
                          min_sessions=min_sessions, plan_n=plan_n),
        "vix": _halfwidth(sig_f, 2, f, iff, alpha=alpha, q=q, mde=mde,
                          min_sessions=min_sessions, plan_n=plan_n),
    }  # fmt: skip
    # pooled b: inverse-variance weighted mean of the per-pair b_vix
    w = [1.0 / (se * se) for _, se in bs if se > 0.0]
    if w and len(w) == len(bs):
        b = sum(wi * bi for wi, (bi, _) in zip(w, bs, strict=True)) / sum(w)
        b_se = 1.0 / math.sqrt(sum(w))
    else:
        b, b_se = float(np.mean([bi for bi, _ in bs])), 0.0
    return CovariateCheck(
        n=n,
        pairs=m,
        sigma_spy=sig_r,
        sigma_spy_vix=sig_f,
        partial_f=f_hac,
        partial_f_iid=f_iid,
        partial_df=(m, df_f),
        partial_p=p,
        hac_factor=f,
        hac_source=src,
        b_vix=b,
        b_vix_se=b_se * math.sqrt(max(f, 1.0)),
        plan_n=plan_n,
        ci_halfwidth_spy=hw["spy"],
        ci_halfwidth_spy_vix=hw["vix"],
        ci_shrink=1.0 - hw["vix"] / hw["spy"] if hw["spy"] > 0.0 else 0.0,
    )


def pooled_check(
    series: Sequence[tuple[Sequence[float], Sequence[float | None], Sequence[float | None]]],
    *,
    hac_lag: int | None,
    alpha: float,
    sigma_upper_q: float,
    mde: float | None,
    min_sessions: int,
    max_factor: float,
    plan_n: int = PLAN_N,
) -> CovariateCheck | None:
    """The check pooled over the Ti − C pairs ``(d, spy, vix)`` (SSE/df summed).

    The long-run factor pools the full-model residuals (:func:`stats.pooled_hac`) from
    2L+2 sessions (mean length) on; below that it is *max_factor*, the primary's rule.
    """
    rs: list[_Side] = []
    fs: list[_Side] = []
    bs: list[tuple[float, float]] = []
    resids: list[list[float]] = []
    dfs: list[int] = []
    n = 0
    for d, spy, vix in series:
        pts = [
            (float(v), float(s), float(c))
            for v, s, c in zip(d, spy, vix, strict=True)
            if s is not None and c is not None
        ]
        if len(pts) < 4:
            continue
        dd = [p[0] for p in pts]
        x2 = np.asarray([[p[1], p[2]] for p in pts])
        fr = stats.ols_fit(dd, [p[1] for p in pts])
        ff = stats.ols_fit(dd, x2)
        if fr is None or ff is None or not fr.adjusted or not ff.adjusted or ff.betas is None:
            continue
        assert ff.betas_se is not None  # noqa: S101
        rs.append(_Side(sse=fr.s_e**2 * fr.df, df=fr.df, factor=1.0, source="iid",
                        intercept_factor=fr.intercept_factor))  # fmt: skip
        fs.append(_Side(sse=ff.s_e**2 * ff.df, df=ff.df, factor=1.0, source="iid",
                        intercept_factor=ff.intercept_factor))  # fmt: skip
        bs.append((ff.betas[1], ff.betas_se[1]))
        resids.append(ff.resid)
        dfs.append(ff.df)
        n += len(pts)
    if not fs:
        return None
    factor: float
    source: stats.HacSource
    if hac_lag is None:
        factor, source = 1.0, "iid"
    elif n / len(fs) >= 2 * hac_lag + 2:
        _, f_run, _ = stats.pooled_hac(resids, dfs, hac_lag)
        factor, source = (f_run or 1.0), "running"
    else:
        factor, source = max(1.0, max_factor), "cap"
    return _assemble(
        rs, fs, bs, n=n, alpha=alpha, q=sigma_upper_q, mde=mde, min_sessions=min_sessions,
        plan_n=plan_n, factor=factor, source=source,
    )  # fmt: skip


def rule_result(check: CovariateCheck) -> RuleResult:
    """D120's pre-registered rule (1) + (2) on a pooled *check*; (3) is the sim test."""
    p_ok = check.partial_p < PARTIAL_P_MAX
    shrink_ok = check.ci_shrink >= MIN_CI_SHRINK
    return RuleResult(partial_p_ok=p_ok, ci_shrink_ok=shrink_ok, recommend_add=p_ok and shrink_ok)
