"""E25.9 (D120): ΔVIX descriptive covariate + the pre-registered add-or-not check.

Every test injects ``now`` and fakes the SPY bars and the VIX closes; nothing reads the
wall clock or the network. The 2-covariate Type-I simulation is seeded (``slow``).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import TYPE_CHECKING

import numpy as np
import pytest

from arc.experiments import stats
from arc.experiments.config import ExperimentsConfig
from arc.experiments.covariate import (
    CachedCloses,
    partial_first_day,
    stored_vix_changes,
    vix_changes,
)
from arc.experiments.covariate_check import (
    covariate_check,
    pooled_check,
    rule_result,
)
from arc.experiments.evaluate import build_report, store_report
from arc.experiments.models import Analysis
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, previous_session
from tests import experiment_fixtures as fx
from tests.test_experiment_hac import E12, X12, XP1_LIVE_HASH, FakeSpy, _after, _closes

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

CFG = ExperimentsConfig()
V12 = [0.8, -1.5, 0.3, 2.1, -0.4, 0.0, -1.1, 0.6, 1.4, -0.9, 0.2, -0.5]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _vix_closes(days: list[dt.date], chg: list[float], start: float = 18.0) -> dict:
    out = {previous_session(days[0]): start}
    v = start
    for d, c in zip(days, chg, strict=True):
        v += c
        out[d] = v
    return out


# ---------------------------------------------------------------------------
# stats: k-covariate OLS (goldens)
# ---------------------------------------------------------------------------


def test_one_covariate_matrix_input_is_the_d119_closed_form_exactly() -> None:
    """The D119 golden path is untouched: a 1-D x and an (n, 1) x give the same Fit."""
    d = [0.0002 + 0.3 * x + e for x, e in zip(X12, E12, strict=True)]
    a = stats.ols_fit(d, X12)
    b = stats.ols_fit(d, np.asarray(X12).reshape(-1, 1))
    assert a is not None and b is not None
    assert a.model_dump_json() == b.model_dump_json()
    assert a.betas is None and a.df == 10  # the closed form, not the matrix path
    # and it still equals the D119 pinned literals
    assert a.beta == pytest.approx(0.3033463035, rel=1e-9)
    assert a.estimate == pytest.approx(2.0415045396e-4, rel=1e-9)
    assert a.s_e == pytest.approx(3.5088178713e-4, rel=1e-9)


def _solve3(m: list[list[float]], v: list[float]) -> list[float]:
    """Cramer's rule on a 3x3 system (hand computation, independent of numpy)."""

    def det(a: list[list[float]]) -> float:
        return (
            a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
        )

    dm = det(m)
    out = []
    for j in range(3):
        mj = [[v[i] if c == j else m[i][c] for c in range(3)] for i in range(3)]
        out.append(det(mj) / dm)
    return out


def test_golden_two_covariates_on_12_points() -> None:
    """d = 0.0002 + 0.3 SPY + 0.0001 ΔVIX + e: a, both b, s_e, intercept SE by hand."""
    d = [0.0002 + 0.3 * x + 0.0001 * v + e for x, v, e in zip(X12, V12, E12, strict=True)]
    fit = stats.ols_fit(d, np.column_stack([X12, V12]))
    assert fit is not None and fit.adjusted and fit.n == 12 and fit.df == 9
    n = 12
    cols = [[1.0] * n, X12, V12]
    xtx = [[sum(cols[i][t] * cols[j][t] for t in range(n)) for j in range(3)] for i in range(3)]
    xty = [sum(cols[i][t] * d[t] for t in range(n)) for i in range(3)]
    a, b1, b2 = _solve3(xtx, xty)
    e = [d[t] - a - b1 * X12[t] - b2 * V12[t] for t in range(n)]
    s_e = math.sqrt(sum(r * r for r in e) / (n - 3))
    inv00 = _solve3(xtx, [1.0, 0.0, 0.0])[0]  # (X'X)^-1 first column, row 0
    inv22 = _solve3(xtx, [0.0, 0.0, 1.0])[2]
    se_a = s_e * math.sqrt(inv00)
    assert fit.estimate == pytest.approx(a, rel=1e-10)
    assert fit.betas == pytest.approx([b1, b2], rel=1e-10)
    assert fit.beta == pytest.approx(b1, rel=1e-10)
    assert fit.s_e == pytest.approx(s_e, rel=1e-10)
    assert fit.s_e * fit.intercept_factor / math.sqrt(n) == pytest.approx(se_a, rel=1e-10)
    assert fit.betas_se is not None and fit.betas_se[1] == pytest.approx(
        s_e * math.sqrt(inv22), rel=1e-10
    )
    # pinned literals (the numbers above)
    assert a == pytest.approx(1.9614139959e-4, rel=1e-8)
    assert b1 == pytest.approx(3.0375259372e-1, rel=1e-8)
    assert b2 == pytest.approx(1.9001429908e-4, rel=1e-8)
    assert s_e == pytest.approx(3.5487576771e-4, rel=1e-8)
    assert se_a == pytest.approx(1.0344023989e-4, rel=1e-8)


def test_matrix_fit_falls_back_to_the_mean_on_small_or_collinear_designs() -> None:
    three = stats.ols_fit([0.1, 0.2, 0.4], np.column_stack([[1, 2, 3], [3, 1, 2]]))
    assert three is not None and not three.adjusted  # df = 0
    collinear = stats.ols_fit([0.1, 0.2, 0.4, 0.3], np.column_stack([[1, 2, 3, 4], [2, 4, 6, 8]]))
    assert collinear is not None and not collinear.adjusted
    with pytest.raises(ValueError, match="differ in length"):
        stats.ols_fit([0.1, 0.2, 0.3], np.zeros((2, 2)))


def test_long_run_primary_two_covariates_uses_the_matrix_intercept_se() -> None:
    d = [0.0002 + 0.3 * x + 0.0001 * v + e for x, v, e in zip(X12, V12, E12, strict=True)]
    lr = stats.long_run_primary(
        d, np.column_stack([X12, V12]), hac_lag=None, alpha=0.05, sigma_upper_q=0.05,
        mde=None, min_sessions=20, max_factor=4.0,
    )  # fmt: skip
    assert lr is not None and lr.fit.df == 9
    base = lr.fit.s_e * math.sqrt(9 / stats._sps.chi2.ppf(0.05, 9))  # noqa: SLF001
    assert lr.sigma_lr == pytest.approx(base * lr.fit.intercept_factor)


# ---------------------------------------------------------------------------
# the check: partial F, CI shrink, pooled, rule
# ---------------------------------------------------------------------------

KW = dict(alpha=0.05, sigma_upper_q=0.05, mde=None, min_sessions=20, max_factor=4.0)


def test_partial_f_matches_the_sse_formula_and_is_hac_scaled() -> None:
    rng = np.random.default_rng(5)
    n = 30
    x, v = rng.normal(0, 0.01, n), rng.normal(0, 1.5, n)
    d = list(0.3 * x + 0.002 * v + rng.normal(0, 0.003, n))
    c = covariate_check(d, list(x), list(v), hac_lag=5, **KW)  # type: ignore[arg-type]
    assert c is not None and c.n == n and c.partial_df == (1, n - 3)
    r1 = stats.ols_fit(d, x)
    r2 = stats.ols_fit(d, np.column_stack([x, v]))
    assert r1 is not None and r2 is not None and r2.betas is not None
    sse1, sse2 = r1.s_e**2 * r1.df, r2.s_e**2 * r2.df
    f_iid = (sse1 - sse2) / (sse2 / (n - 3))
    assert c.partial_f_iid == pytest.approx(f_iid, rel=1e-10)
    assert c.partial_f == pytest.approx(f_iid / c.hac_factor, rel=1e-10)
    assert c.partial_p == pytest.approx(stats._sps.f.sf(c.partial_f, 1, n - 3))  # noqa: SLF001
    assert c.b_vix == pytest.approx(r2.betas[1]) and c.b_vix == pytest.approx(0.002, abs=4e-4)
    assert c.hac_source == "running" and c.hac_factor >= 1.0
    # a strong ΔVIX effect: tiny p, a much narrower CI, the rule recommends it
    assert c.partial_p < 1e-4 and c.ci_shrink > 0.2
    assert rule_result(c).recommend_add


def test_no_vix_effect_keeps_spy_only_and_the_df_loss_widens_the_ci() -> None:
    rng = np.random.default_rng(6)
    n = 25
    x, v = rng.normal(0, 0.01, n), rng.normal(0, 1.5, n)
    d = list(0.3 * x + rng.normal(0, 0.003, n))
    c = covariate_check(d, list(x), list(v), hac_lag=5, **KW)  # type: ignore[arg-type]
    assert c is not None
    r = rule_result(c)
    assert not r.recommend_add
    # with ΔVIX explaining ~nothing the extra df makes the n=20 CI wider, not narrower
    assert c.ci_shrink < 0.10


def test_check_needs_four_sessions_with_both_covariates() -> None:
    d = [0.001, 0.002, -0.001, 0.0, 0.003]
    x = [0.01, -0.01, 0.002, None, 0.004]
    v = [1.0, None, -0.5, 0.2, 0.3]
    assert covariate_check(d, x, v, hac_lag=5, **KW) is None  # type: ignore[arg-type]
    assert pooled_check([(d, x, v)], hac_lag=5, **KW) is None  # type: ignore[arg-type]


def test_small_n_uses_the_cap_factor_like_the_primary() -> None:
    rng = np.random.default_rng(8)
    x, v = list(rng.normal(0, 0.01, 8)), list(rng.normal(0, 1, 8))
    d = list(rng.normal(0, 0.003, 8))
    c = covariate_check(d, x, v, hac_lag=5, **KW)  # type: ignore[arg-type]
    assert c is not None and c.hac_source == "cap" and c.hac_factor == 4.0
    p = pooled_check([(d, x, v)], hac_lag=5, **KW)  # type: ignore[arg-type]
    assert p is not None and p.hac_source == "cap"


def test_pooled_check_sums_sse_and_df_with_one_restriction_per_pair() -> None:
    rng = np.random.default_rng(9)
    series = []
    for _ in range(3):
        x, v = rng.normal(0, 0.01, 20), rng.normal(0, 1.5, 20)
        series.append((list(0.3 * x + rng.normal(0, 0.003, 20)), list(x), list(v)))
    p = pooled_check(series, hac_lag=5, **KW)  # type: ignore[arg-type]
    assert p is not None and p.pairs == 3 and p.n == 60 and p.partial_df == (3, 3 * 17)
    sse_r = sse_f = 0.0
    for d, x, v in series:
        r1, r2 = stats.ols_fit(d, x), stats.ols_fit(d, np.column_stack([x, v]))
        assert r1 is not None and r2 is not None
        sse_r += r1.s_e**2 * r1.df
        sse_f += r2.s_e**2 * r2.df
    assert p.sigma_spy == pytest.approx(math.sqrt(sse_r / 54))
    assert p.sigma_spy_vix == pytest.approx(math.sqrt(sse_f / 51))
    assert p.partial_f_iid == pytest.approx((sse_r - sse_f) / 3 / (sse_f / 51))
    resid = [stats.ols_fit(d, np.column_stack([x, v])).resid for d, x, v in series]  # type: ignore[union-attr]
    _, f_run, _ = stats.pooled_hac(resid, [17, 17, 17], 5)
    assert p.hac_source == "running" and p.hac_factor == pytest.approx(f_run)


# ---------------------------------------------------------------------------
# spec: vix_chg is legal; every committed spec hashes as before
# ---------------------------------------------------------------------------


def test_vix_chg_is_a_legal_covariate_and_xp1_still_hashes_the_same() -> None:
    from arc.experiments.config import load_experiments_config
    from arc.experiments.models import spec_hash
    from arc.experiments.overlay import LIVE_SPECS_DIR, fill_defaults, load_spec

    sp = load_spec(LIVE_SPECS_DIR / "xp1_aa_k3.yaml")
    assert spec_hash(fill_defaults(sp, load_experiments_config().defaults)) == XP1_LIVE_HASH
    two = fx.spec("XP-2", analysis={"covariates": ["spy_ret", "vix_chg"], "hac_lag": 5})
    one = fx.spec("XP-2", analysis={"covariates": ["spy_ret"], "hac_lag": 5})
    assert spec_hash(two) != spec_hash(one)
    with pytest.raises(ValueError):  # noqa: PT011
        fx.spec("XP-2", analysis={"covariates": ["vix_chg", "vix_chg"], "hac_lag": 5})
    # no default names it (the D120 rule decides, in a separate owner-approved PR)
    assert load_experiments_config().defaults.analysis.covariates == ["spy_ret"]


# ---------------------------------------------------------------------------
# ΔVIX series: close-to-close, stored first, partial first session missing
# ---------------------------------------------------------------------------


def test_vix_changes_close_to_close_stored_first_and_one_fetch() -> None:
    days = fx.sessions(3)
    calls: list[int] = []

    def closes() -> dict:
        calls.append(1)
        return _vix_closes(days, [1.5, -0.5, 2.0])

    cached = CachedCloses(closes)
    got = vix_changes(days, t0=fx.T0, partial_first=False, stored={}, closes=cached)
    assert [got[d] for d in days] == pytest.approx([1.5, -0.5, 2.0])
    got2 = vix_changes(days, t0=fx.T0, partial_first=False, stored={days[0]: 9.0}, closes=cached)
    assert got2[days[0]] == 9.0 and calls == [1]  # cached: one GET per evaluation
    assert vix_changes(days, t0=fx.T0, partial_first=False, stored=got, closes=None) == got
    assert vix_changes(days, t0=fx.T0, partial_first=False, stored={}, closes=None) == {}


def test_vix_changes_failure_gap_and_partial_first_session() -> None:
    days = fx.sessions(3)

    def boom() -> dict:
        msg = "cboe down"
        raise RuntimeError(msg)

    assert vix_changes(days, t0=fx.T0, partial_first=False, stored={}, closes=boom) == {}
    px = _vix_closes(days, [1.0, 2.0, 3.0])
    del px[days[1]]
    got = vix_changes(days, t0=fx.T0, partial_first=False, stored={}, closes=lambda: px)
    assert set(got) == {days[0]}  # day 1 has no close, day 2 no previous close
    t0 = dt.datetime.combine(days[0], dt.time(11, 3), tzinfo=ET)
    assert partial_first_day(days, t0=t0, partial_first=True)
    full = _vix_closes(days, [1.0, 2.0, 3.0])
    got = vix_changes(days, t0=t0, partial_first=True, stored={}, closes=lambda: full)
    assert days[0] not in got and got[days[1]] == pytest.approx(2.0)
    # even a stored value for the partial day is ignored (it never exists)
    got = vix_changes(days, t0=t0, partial_first=True, stored={days[0]: 5.0}, closes=None)
    assert got == {}


# ---------------------------------------------------------------------------
# reports: persisted vix_chg, descriptive covariate_check, verdict untouched
# ---------------------------------------------------------------------------


def _xp(conn: sqlite3.Connection, n: int, *, analysis: dict | None, gamma: float = 0.0):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(23)
    x = list(rng.normal(0, 0.01, n))
    v = list(rng.normal(0, 1.5, n))
    ctrl = list(rng.normal(20, 300, n))
    treat = [
        c + 0.3 * xi * fx.T0_EQUITY + gamma * vi * fx.T0_EQUITY + e
        for c, xi, vi, e in zip(ctrl, x, v, rng.normal(0, 100, n), strict=True)
    ]
    kw = {"analysis": analysis} if analysis is not None else {}
    store = fx.start(conn, fx.spec("XP-2", **kw))
    days = fx.equity_curves(conn, "XP-2", ctrl, treat)
    return store, days, x, v


def test_report_persists_vix_chg_and_the_check_never_changes_the_verdict(
    conn: sqlite3.Connection,
) -> None:
    analysis = {"covariates": ["spy_ret"], "hac_lag": 5}
    store, days, x, v = _xp(conn, 25, analysis=analysis, gamma=0.002)
    st_ = store.require("XP-2")
    spy = FakeSpy(_closes(days, x))
    plain = build_report(conn, st_, CFG, now=_after(days), market=spy)
    rep = build_report(
        conn, st_, CFG, now=_after(days), market=FakeSpy(_closes(days, x)),
        vix=lambda: _vix_closes(days, v),
    )  # fmt: skip
    assert [r.vix_chg for r in rep.series] == pytest.approx(v)
    c = rep.primary.covariate_check
    assert c is not None and c.n == 25 and c.partial_p < 0.05
    assert rep.calibration.covariate_check is not None
    # the verdict and every number it reads are byte-identical with or without ΔVIX
    for k in ("mean", "ci", "sigma", "p_value", "adjustment", "hac", "lower_bound_positive"):
        assert getattr(rep.primary, k) == getattr(plain.primary, k), k
    assert rep.verdict == plain.verdict and rep.verdict_reason == plain.verdict_reason
    # no VIX: the D120 fields are absent from the JSON (stored reports byte-identical)
    doc = json.loads(plain.canonical_json())
    assert "covariate_check" not in doc["primary"] and "covariate_check" not in doc["calibration"]
    assert all("vix_chg" not in r for r in doc["series"])
    # stored-first offline reproduction
    with conn:
        store_report(conn, rep, run_id="t")
    assert stored_vix_changes(conn, "XP-2") == pytest.approx(dict(zip(days, v, strict=True)))
    offline = build_report(conn, st_, CFG, now=_after(days))
    assert offline.canonical_json() == rep.canonical_json()


def test_multi_arm_pairs_and_pooled_check(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(41)
    n = 22
    x, v = list(rng.normal(0, 0.01, n)), list(rng.normal(0, 1.5, n))
    over = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
    sp = fx.spec("XP-3", arms={"treatments": {f"t{i}": over for i in (1, 2)}}, max_sessions=40)
    store = fx.start(conn, sp)
    days = fx.sessions(n)
    fx.pnl_row(conn, previous_session(days[0]), fx.T0_EQUITY, None)
    eq = {"c": fx.T0_EQUITY, "t1": fx.T0_EQUITY, "t2": fx.T0_EQUITY}
    ctrl = list(rng.normal(20, 300, n))
    for i, day in enumerate(days):
        eq["c"] += ctrl[i]
        fx.pnl_row(conn, day, eq["c"], None)
        for name, g in (("t1", 0.003), ("t2", 0.0)):
            eq[name] += ctrl[i] + g * v[i] * fx.T0_EQUITY + rng.normal(0, 100)
            fx.pnl_row(conn, day, eq[name], f"XP-3:{name}")
    conn.commit()
    rep = build_report(
        conn, store.require("XP-3"), CFG, now=_after(days), market=FakeSpy(_closes(days, x)),
        vix=lambda: _vix_closes(days, v),
    )  # fmt: skip
    pairs = {p.b: p for p in rep.pairwise or [] if p.kind == "control_vs_treatment"}
    c1, c2 = pairs["t1"].covariate_check, pairs["t2"].covariate_check
    assert c1 is not None and c2 is not None
    assert c1.b_vix == pytest.approx(0.003, abs=5e-4) and c1.partial_p < 1e-4
    assert abs(c2.b_vix) < 5e-4
    pooled = rep.calibration.covariate_check
    assert pooled is not None and pooled.pairs == 2 and pooled.partial_df[0] == 2
    assert all(r.vix_chg is not None for r in rep.series)


def test_pre_registered_vix_chg_waits_on_a_missing_day_but_not_the_partial_one(
    conn: sqlite3.Connection,
) -> None:
    """A future spec *can* pre-register vix_chg; then a missing ΔVIX day holds its verdict."""
    analysis = {"covariates": ["spy_ret", "vix_chg"], "hac_lag": 5}
    store, days, x, v = _xp(conn, 25, analysis=analysis, gamma=0.002)
    st_ = store.require("XP-2")
    px = _vix_closes(days, v)
    del px[days[-1]]
    rep = build_report(
        conn, st_, CFG, now=_after(days), market=FakeSpy(_closes(days, x)), vix=lambda: px
    )
    a = rep.primary.adjustment
    assert a is not None and a.covariates == ["spy_ret", "vix_chg"] and a.status == "fitted"
    assert rep.covariate_missing == [days[-1]] and "waiting on" in rep.verdict_reason
    full = build_report(
        conn, st_, CFG, now=_after(days), market=FakeSpy(_closes(days, x)),
        vix=lambda: _vix_closes(days, v),
    )  # fmt: skip
    fa = full.primary.adjustment
    assert fa is not None and fa.n == 25 and full.covariate_missing == []
    fit = stats.ols_fit([r.d for r in full.series], np.column_stack([x, v]))
    assert fit is not None and fa.estimate == pytest.approx(fit.estimate)
    assert Analysis(**analysis).adjusted


def test_cli_covariate_check_prints_pairs_pooled_and_the_rule(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    store, days, x, v = _xp(c, 25, analysis=None, gamma=0.002)
    rep = build_report(
        c, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x)),
        vix=lambda: _vix_closes(days, v),
    )  # fmt: skip
    with c:
        store_report(c, rep, run_id="t")
    c.close()
    env = {**os.environ, "ARC_ENV": "paper"}
    args = [sys.executable, "-m", "arc.cli", "experiment", "covariate-check", "XP-2"]
    args += ["--db", str(db), "--now", _after(days).isoformat()]
    out = subprocess.run(args, env=env, capture_output=True, text=True, check=False, timeout=120)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "ΔVIX covariate check (D120" in text and "pooled (1)" in text
    assert "pre-registered rule (PLAN D120)" in text and "recommend adding vix_chg" in text
    js = subprocess.run(
        [*args, "--json"], env=env, capture_output=True, text=True, check=False, timeout=120
    )
    doc = json.loads(js.stdout)
    assert doc["rule"]["recommend_add"] is True and len(doc["series"]) == 25
    assert doc["pooled"]["partial_p"] < 0.05
    # read-only: the store has exactly the one report it had
    ro = connect(str(db))
    assert ro.execute("SELECT COUNT(*) FROM experiment_reports").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# D120 rule (3): 2-covariate Type-I with daily peeking (slow)
# ---------------------------------------------------------------------------

SIMS, H, MIN_N, ALPHA, SIG = 2000, 60, 20, 0.05, 0.004


def _ar1(rng: np.random.Generator, rho: float, shape: tuple[int, int]) -> np.ndarray:
    e = rng.normal(0, 1, shape)
    out = np.empty(shape)
    out[:, 0] = e[:, 0] / math.sqrt(1 - rho * rho)
    for t in range(1, shape[1]):
        out[:, t] = rho * out[:, t - 1] + e[:, t]
    return out * SIG * math.sqrt(1 - rho * rho)


@pytest.mark.slow
@pytest.mark.parametrize("rho", [0.0, 0.3, 0.5])
def test_two_covariate_type_i_with_daily_peeking(rho: float) -> None:
    """theta = 0, d = 0.3 x1 + 0.2 x2 + AR(1): the 2-covariate D119 method (HAC lag 5,
    the cap below 2L+2) rejects at most alpha + 2 MC SE with daily looks 4..60.

    x1 ~ SPY returns (sd 1%), x2 ~ ΔVIX scaled to the same sd so 0.2 x2 is a
    comparable share. Seeded, 2,000 reps (bound 0.0597): observed type-I 0.0085 at
    rho = 0, 0.0240 at 0.3, 0.0585 at 0.5 (D120 rule 3, recorded in the PR body).
    """
    rng = np.random.default_rng(120 + int(rho * 10))
    x1 = rng.normal(0, 0.01, (SIMS, H))
    x2 = rng.normal(0, 0.01, (SIMS, H))
    d = 0.3 * x1 + 0.2 * x2 + _ar1(rng, rho, (SIMS, H))
    ever = 0
    for i in range(SIMS):
        xx = np.column_stack([x1[i], x2[i]])
        for n in range(4, H + 1):
            r = stats.long_run_primary(
                d[i, :n], xx[:n], hac_lag=5, alpha=ALPHA, sigma_upper_q=0.05, mde=None,
                min_sessions=MIN_N, max_factor=4.0,
            )  # fmt: skip
            if r is not None and (r.ci.lo > 0 or r.ci.hi < 0):
                ever += 1
                break
    rate = ever / SIMS
    bound = ALPHA + 2 * math.sqrt(ALPHA * (1 - ALPHA) / SIMS)
    assert rate <= bound, (rho, rate)
