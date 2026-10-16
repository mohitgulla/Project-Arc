"""E25.7 (D119): pre-registered SPY regression adjustment + HAC long-run sigma.

Every test injects ``now`` and fakes the SPY bar provider; nothing reads the wall
clock or the network. The simulations are seeded (daily peeking over 60 sessions).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.data.base import HistoryBar
from arc.experiments import adopt as ad
from arc.experiments import stats
from arc.experiments.config import ExperimentsConfig, load_experiments_config
from arc.experiments.covariate import FeedPinned, spy_returns, stored_spy_returns
from arc.experiments.evaluate import build_report, evaluate, store_report
from arc.experiments.models import Analysis, ExperimentSpec, StopDetail, spec_hash
from arc.experiments.overlay import LIVE_SPECS_DIR, fill_defaults, load_spec
from arc.experiments.store import ExperimentStore
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, previous_session, session_close
from tests import experiment_fixtures as fx

if TYPE_CHECKING:
    import sqlite3

CFG = ExperimentsConfig()
ANALYSIS = {"covariates": ["spy_ret"], "hac_lag": 5}
FIXTURES = Path(__file__).parent / "fixtures" / "experiments"
#: the running live XP-1's locked hash (registered before D119): must never move
XP1_LIVE_HASH = "7e73456196a92c197d1abce80daa9a6aa0b8e115c5ae49734576937b4a746919"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _after(days: list[dt.date]) -> dt.datetime:
    return fx.eod(days[-1]) + dt.timedelta(minutes=15)


class FakeSpy:
    """SPY daily bars: ``closes`` by day; ``minute`` = 1-min bars (t0 price)."""

    def __init__(
        self,
        closes: dict[dt.date, float],
        *,
        minute: list[tuple[dt.datetime, float]] | None = None,
        fail: bool = False,
    ) -> None:
        self.closes = closes
        self.minute = minute or []
        self.fail = fail
        self.calls: list[tuple[str, dt.date, dt.date, str, Any]] = []

    def history_bars(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
        *,
        feed: Any = None,
    ) -> list[HistoryBar]:
        self.calls.append((symbol, start, end, timeframe, feed))
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        assert symbol == "SPY"
        if timeframe == "1Min":
            return [_bar(t, p) for t, p in self.minute if start <= t.astimezone(ET).date() <= end]
        return [
            _bar(session_close(d) - dt.timedelta(minutes=1), c)
            for d, c in sorted(self.closes.items())
            if start <= d <= end
        ]


def _bar(t: dt.datetime, p: float) -> HistoryBar:
    return HistoryBar(timestamp=t, open=p, high=p, low=p, close=p, volume=1.0)


def _closes(days: list[dt.date], rets: list[float], start: float = 600.0) -> dict[dt.date, float]:
    out = {previous_session(days[0]): start}
    px = start
    for d, r in zip(days, rets, strict=True):
        px *= 1.0 + r
        out[d] = px
    return out


# ---------------------------------------------------------------------------
# stats: hand-computed golden (12 points)
# ---------------------------------------------------------------------------

X12 = [0.01, -0.02, 0.005, 0.0, 0.015, -0.01, 0.02, -0.005, 0.0, 0.01, -0.015, 0.005]
E12 = [
    0.0005, -0.0003, 0.0002, 0.0004, -0.0006, 0.0001,
    0.0003, -0.0002, -0.0004, 0.0, 0.0002, -0.0001,
]  # fmt: skip


def test_golden_ols_and_bartlett_on_12_points() -> None:
    """d = 0.0002 + 0.3 x + e: beta, residual sigma and the Bartlett factor by hand."""
    d = [0.0002 + 0.3 * x + e for x, e in zip(X12, E12, strict=True)]
    fit = stats.ols_fit(d, X12)
    assert fit is not None and fit.adjusted and fit.n == 12 and fit.df == 10
    # hand computation (independent of the implementation)
    n = 12
    xm, dm = sum(X12) / n, sum(d) / n
    sxx = sum((x - xm) ** 2 for x in X12)
    sxy = sum((x - xm) * (v - dm) for x, v in zip(X12, d, strict=True))
    b = sxy / sxx
    a = dm - b * xm
    e = [v - a - b * x for x, v in zip(X12, d, strict=True)]
    s_e = math.sqrt(sum(r * r for r in e) / (n - 2))
    assert fit.beta == pytest.approx(b, rel=1e-12)
    assert fit.estimate == pytest.approx(a, rel=1e-12)
    assert fit.s_e == pytest.approx(s_e, rel=1e-12)
    assert fit.intercept_factor == pytest.approx(math.sqrt(1 + n * xm * xm / sxx), rel=1e-12)
    # pinned literals (the numbers above, so a silent formula change is caught)
    assert b == pytest.approx(0.3033463035, rel=1e-9)
    assert a == pytest.approx(2.0415045396e-4, rel=1e-9)
    assert s_e == pytest.approx(3.5088178713e-4, rel=1e-9)
    den = sum(r * r for r in e)
    rho = [sum(e[t] * e[t - k] for t in range(k, n)) / den for k in (1, 2, 3)]
    assert stats.autocorrelations(fit.resid, 3) == pytest.approx(rho, abs=1e-12)
    f = 1 + 2 * sum((1 - k / 4) * r for k, r in enumerate(rho, start=1))
    assert stats.bartlett_factor(rho, 3) == pytest.approx(max(1.0, f), rel=1e-12)


def test_bartlett_factor_is_floored_at_one_and_matches_known_values() -> None:
    assert stats.bartlett_factor([-0.5, -0.4, -0.3], 3) == 1.0
    # L=2: 1 + 2 (2/3 * 0.5 + 1/3 * 0.25) = 1.8333...
    assert stats.bartlett_factor([0.5, 0.25], 2) == pytest.approx(1 + 2 * (0.5 * 2 / 3 + 0.25 / 3))
    assert stats.bartlett_factor([], 5) == 1.0
    # running factor: never below the AR(1)-implied (1 + r1)/(1 - r1) on the corrected r1
    rc = stats.corrected_autocorrelations([0.4, 0.1], 30)
    assert rc[0] == pytest.approx(0.4 + (1 + 1.2) / 30) and rc[1] == pytest.approx(0.1 + 1 / 30)
    f = stats.running_hac_factor([0.4, 0.1], 30, 2)
    assert f >= (1 + rc[0]) / (1 - rc[0]) - 1e-12


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.floats(-0.05, 0.05, allow_nan=False), min_size=4, max_size=40),
    st.integers(1, 10),
)
def test_long_run_sigma_never_narrower_than_the_iid_sigma(xs: list[float], lag: int) -> None:
    rng = np.random.default_rng(len(xs))
    d = list(0.2 * np.asarray(xs) + rng.normal(0, 0.003, len(xs)))
    kw: dict[str, Any] = dict(
        alpha=0.05, sigma_upper_q=0.05, mde=None, min_sessions=20, max_factor=4.0
    )
    iid = stats.long_run_primary(d, xs, hac_lag=None, **kw)
    hac = stats.long_run_primary(d, xs, hac_lag=lag, **kw)
    if iid is None:
        assert hac is None
        return
    assert hac is not None and hac.factor >= 1.0
    assert hac.sigma_lr >= iid.sigma_lr * (1 - 1e-12)
    assert hac.ci.hi - hac.ci.lo >= (iid.ci.hi - iid.ci.lo) * (1 - 1e-12)


def test_long_run_small_n_uses_the_aa_factor_else_the_cap() -> None:
    d = list(np.random.default_rng(1).normal(0, 0.003, 8))
    x = list(np.random.default_rng(2).normal(0, 0.01, 8))
    kw: dict[str, Any] = dict(
        alpha=0.05, sigma_upper_q=0.05, mde=None, min_sessions=20, max_factor=4.0
    )
    cap = stats.long_run_primary(d, x, hac_lag=5, **kw)
    assert cap is not None and cap.hac_source == "cap" and cap.factor == 4.0
    aa = stats.long_run_primary(d, x, hac_lag=5, aa_factor=1.7, **kw)
    assert aa is not None and aa.hac_source == "aa" and aa.factor == 1.7
    # from 2L + 2 sessions on: the running factor
    d20 = list(np.random.default_rng(3).normal(0, 0.003, 12))
    x20 = list(np.random.default_rng(4).normal(0, 0.01, 12))
    run = stats.long_run_primary(d20, x20, hac_lag=5, **kw)
    assert run is not None and run.hac_source == "running"
    assert run.factor == pytest.approx(run.factor_running)
    # an A/A sigma *and* factor: both from the A/A at any n
    both = stats.long_run_primary(d20, x20, hac_lag=5, aa_sigma=0.002, aa_factor=1.4, **kw)
    assert both is not None and both.hac_source == "aa" and both.sigma_source == "aa"
    assert both.sigma_lr == pytest.approx(0.002 * math.sqrt(1.4) * both.fit.intercept_factor)


def test_unadjusted_fit_is_the_mean_and_short_series_have_none() -> None:
    assert stats.ols_fit([0.1], None) is None
    f = stats.ols_fit([0.001, 0.003], [0.01, 0.02])  # n < 3: mean, no beta
    assert f is not None and not f.adjusted and f.estimate == pytest.approx(0.002)
    flat = stats.ols_fit([0.001, 0.002, 0.003], [0.01, 0.01, 0.01])  # Sxx = 0
    assert flat is not None and not flat.adjusted
    with pytest.raises(ValueError, match="differ in length"):
        stats.ols_fit([0.1, 0.2, 0.3], [0.1, 0.2])


def test_pooled_hac_pools_residual_cross_products() -> None:
    e1, e2 = [0.1, -0.1, 0.2, -0.2], [0.05, 0.05, -0.05, -0.05]
    sigma, f, rho = stats.pooled_hac([e1, e2], [2, 2], 1)
    sse = sum(v * v for v in e1 + e2)
    assert sigma == pytest.approx(math.sqrt(sse / 4))
    num = sum(e1[t] * e1[t - 1] for t in (1, 2, 3)) + sum(e2[t] * e2[t - 1] for t in (1, 2, 3))
    assert rho == pytest.approx([num / sse])
    assert f == pytest.approx(stats.running_hac_factor(rho, 4, 1))
    assert stats.pooled_hac([], [], 5) == (None, None, [])
    assert stats.pooled_hac([[0.0, 0.0]], [1], 5) == (None, None, [])


# ---------------------------------------------------------------------------
# simulations: type-I with daily peeking, power (acceptance 1 + 2)
# ---------------------------------------------------------------------------

SIMS, H, MIN_N, ALPHA, SIG = 2000, 60, 20, 0.05, 0.004


def _ar1(rng: np.random.Generator, rho: float, shape: tuple[int, int]) -> np.ndarray:
    e = rng.normal(0, 1, shape)
    out = np.empty(shape)
    out[:, 0] = e[:, 0] / math.sqrt(1 - rho * rho)
    for t in range(1, shape[1]):
        out[:, t] = rho * out[:, t - 1] + e[:, t]
    return out * SIG * math.sqrt(1 - rho * rho)


def _peek(d: np.ndarray, x: np.ndarray, *, lag: int | None, adjust: bool) -> tuple[float, float]:
    """(any-rejection rate, win rate) with daily looks from session 3 to H."""
    ever = np.zeros(d.shape[0], bool)
    win = np.zeros(d.shape[0], bool)
    for i in range(d.shape[0]):
        for n in range(3, H + 1):
            r = stats.long_run_primary(
                d[i, :n], x[i, :n] if adjust else None, hac_lag=lag, alpha=ALPHA,
                sigma_upper_q=0.05, mde=None, min_sessions=MIN_N, max_factor=4.0,
            )  # fmt: skip
            if r is None:
                continue
            if r.ci.lo > 0 or r.ci.hi < 0:
                ever[i] = True
                win[i] = n >= MIN_N and r.ci.lo > 0
                break
    return float(ever.mean()), float(win.mean())


@pytest.mark.slow
@pytest.mark.parametrize("rho", [0.0, 0.3, 0.5])
def test_type_i_with_daily_peeking_under_autocorrelation(rho: float) -> None:
    """d = beta x + AR(1) noise, theta = 0: the D119 method's false-rejection rate is
    within alpha + 2 MC SE at every rho; the old iid method breaks it at rho = 0.5."""
    rng = np.random.default_rng(7 + int(rho * 10))
    x = rng.normal(0, 0.01, (SIMS, H))
    d = 0.3 * x + _ar1(rng, rho, (SIMS, H))
    bound = ALPHA + 2 * math.sqrt(ALPHA * (1 - ALPHA) / SIMS)
    new, _ = _peek(d, x, lag=5, adjust=True)
    assert new <= bound, (rho, new)
    if rho == 0.5:
        old, _ = _peek(d, x, lag=None, adjust=False)
        assert old > bound, old  # why D119: iid under-covers autocorrelated days


@pytest.mark.slow
@pytest.mark.parametrize("corr", [0.3, 0.5, 0.7])
def test_adjustment_adds_power_when_spy_explains_the_diff(corr: float) -> None:
    """theta > 0, |corr(d, x)| = corr: adjusted+HAC power >= unadjusted+HAC power.

    Common random numbers: the adjusted fit strips b x exactly, so its power is the same
    at every corr while the unadjusted power falls (2000 sims: 0.584 vs 0.580 at 0.3,
    0.584 vs 0.548 at 0.5, 0.584 vs 0.457 at 0.7)."""
    sims = SIMS
    rng = np.random.default_rng(99)
    x = rng.normal(0, 0.01, (sims, H))
    noise = _ar1(rng, 0.3, (sims, H))
    beta = corr * SIG / (0.01 * math.sqrt(1 - corr * corr))
    d = 0.003 + beta * x + noise
    adj = _peek(d, x, lag=5, adjust=True)[1]
    unadj = _peek(d, x, lag=5, adjust=False)[1]
    assert adj >= unadj, (corr, adj, unadj)
    if corr >= 0.5:
        assert adj - unadj >= 2 * math.sqrt(0.25 / sims), (corr, adj, unadj)


# ---------------------------------------------------------------------------
# spec: pre-registration and hash stability (acceptance 4)
# ---------------------------------------------------------------------------


def test_spec_without_analysis_hashes_as_before_d119() -> None:
    sp = load_spec(LIVE_SPECS_DIR / "xp1_aa_k3.yaml")
    assert sp.analysis is None
    assert spec_hash(fill_defaults(sp, load_experiments_config().defaults)) == XP1_LIVE_HASH
    assert "analysis" not in json.dumps(sp.model_dump(mode="json", exclude={"analysis"}))


@pytest.mark.parametrize(
    "path",
    sorted(LIVE_SPECS_DIR.glob("*.yaml")) + sorted(FIXTURES.glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_committed_specs_hash_unchanged_by_the_analysis_field(path: Path) -> None:
    sp = load_spec(path)
    doc = sp.model_dump(mode="json")
    doc.pop("analysis")
    pre = ExperimentSpec.model_validate(doc)
    assert spec_hash(sp) == spec_hash(pre)
    # the canonical document carries no analysis key at all when absent
    from arc.experiments.models import canonical_json

    assert '"analysis"' not in canonical_json(sp)


def test_analysis_is_hash_locked_and_validated() -> None:
    base = fx.spec("XP-2")
    with_a = fx.spec("XP-2", analysis=ANALYSIS)
    assert spec_hash(base) != spec_hash(with_a)
    assert spec_hash(with_a) != spec_hash(fx.spec("XP-2", analysis={**ANALYSIS, "hac_lag": 4}))
    for bad in (
        {"covariates": ["qqq_ret"], "hac_lag": 5},
        {"covariates": ["spy_ret", "spy_ret"], "hac_lag": 5},
        {"covariates": ["spy_ret"], "hac_lag": 0},
        {"covariates": ["spy_ret"], "hac_lag": 21},
        {"covariates": ["spy_ret"]},
        {"covariates": ["spy_ret"], "hac_lag": 5, "x": 1},
    ):
        with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError
            fx.spec("XP-2", analysis=bad)
    assert fx.spec("XP-2", analysis={"covariates": [], "hac_lag": None}).analysis is not None


def test_adopt_writes_the_default_analysis_unless_the_proposal_names_one() -> None:
    cfg = load_experiments_config()
    assert cfg.defaults.analysis == Analysis(covariates=["spy_ret"], hac_lag=5)
    spec = {
        "id": "XP-<next>",
        "kind": "ab",
        "spec_version": 2,
        "title": "t",
        "hypothesis": "h",
        "area": "exits",
        "arms": {
            "treatments": {
                f"t{i}": {"overlay": {"exits": {"default": {"take_profit_pct_of_max_gain": v}}}}
                for i, v in ((1, 0.35), (2, 0.4), (3, 0.45))
            }
        },
        "backtest_ref": "BT-3",
        "non_inferiority_margin": 0.1,
    }
    built = ad._build_spec(ad.load_proposal(spec, proposal_id="P-1"), "XP-9", cfg)  # noqa: SLF001
    assert built.analysis == Analysis(covariates=["spy_ret"], hac_lag=5)
    own = {**spec, "analysis": {"covariates": [], "hac_lag": 3}}
    built = ad._build_spec(ad.load_proposal(own, proposal_id="P-1"), "XP-9", cfg)  # noqa: SLF001
    assert built.analysis == Analysis(covariates=[], hac_lag=3)


def test_the_analysis_defaults_and_stats_knobs_are_classified() -> None:
    from arc.control.registry import NOT_EXPOSED_PATHS, REGISTRY

    assert "experiments.stats.hac_lag" in REGISTRY
    assert "experiments.stats.hac_max_factor" in REGISTRY
    assert "experiments.defaults.analysis.covariates" in NOT_EXPOSED_PATHS
    assert "experiments.defaults.analysis.hac_lag" in NOT_EXPOSED_PATHS
    cfg = load_experiments_config()
    assert cfg.stats.hac_lag == 5 and cfg.stats.hac_max_factor == 4.0


# ---------------------------------------------------------------------------
# covariate: SPY returns, stored first, partial first session
# ---------------------------------------------------------------------------


def test_spy_returns_close_to_close_and_stored_first() -> None:
    days = fx.sessions(3)
    spy = FakeSpy(_closes(days, [0.01, -0.02, 0.005]))
    got = spy_returns(days, t0=fx.T0, partial_first=False, stored={}, market=spy)
    assert [got[d] for d in days] == pytest.approx([0.01, -0.02, 0.005])
    # stored values win and only the rest is fetched
    spy2 = FakeSpy(_closes(days, [0.01, -0.02, 0.005]))
    got2 = spy_returns(
        days, t0=fx.T0, partial_first=False, stored={days[0]: 0.5, days[1]: 0.6}, market=spy2
    )
    assert got2[days[0]] == 0.5 and got2[days[1]] == 0.6
    assert got2[days[2]] == pytest.approx(0.005)
    assert spy2.calls[0][1:3] == (days[1], days[2])
    # everything stored: no call; no market: stored only
    assert spy_returns(days, t0=fx.T0, partial_first=False, stored=got, market=FakeSpy({})) == got
    assert spy_returns(days, t0=fx.T0, partial_first=False, stored={}, market=None) == {}


def test_spy_returns_partial_first_session_uses_the_t0_price() -> None:
    days = fx.sessions(2)
    t0 = dt.datetime.combine(days[0], dt.time(11, 3, 20), tzinfo=ET)
    closes = _closes(days, [0.01, 0.002])
    minute = [(t0.replace(second=0) - dt.timedelta(minutes=1), 1.0), (t0.replace(second=0), 605.0)]
    got = spy_returns(
        days, t0=t0, partial_first=True, stored={}, market=FakeSpy(closes, minute=minute)
    )
    assert got[days[0]] == pytest.approx(closes[days[0]] / 605.0 - 1)
    assert got[days[1]] == pytest.approx(0.002)


def test_spy_returns_fetch_failure_or_gap_leaves_days_missing() -> None:
    days = fx.sessions(3)
    bad = FakeSpy({}, fail=True)
    assert spy_returns(days, t0=fx.T0, partial_first=False, stored={}, market=bad) == {}
    closes = _closes(days, [0.01, -0.02, 0.005])
    del closes[days[1]]
    got = spy_returns(days, t0=fx.T0, partial_first=False, stored={}, market=FakeSpy(closes))
    assert days[1] not in got and days[2] not in got  # no previous close either
    assert got[days[0]] == pytest.approx(0.01)


def test_feed_pinned_passes_the_history_feed() -> None:
    spy = FakeSpy({})
    FeedPinned(spy, feed="sip").history_bars("SPY", dt.date(2026, 10, 5), dt.date(2026, 10, 6))
    assert spy.calls[-1][-1] == "sip"


# ---------------------------------------------------------------------------
# evaluation: pre-registered vs descriptive, stored covariate, missing day waits
# ---------------------------------------------------------------------------


def _xp(conn: sqlite3.Connection, n: int, *, analysis: dict | None, beta: float = 0.0):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(17)
    x = list(rng.normal(0, 0.01, n))
    ctrl = list(rng.normal(20, 300, n))
    treat = [
        c + 400 + beta * xi * fx.T0_EQUITY + e
        for c, xi, e in zip(ctrl, x, rng.normal(0, 100, n), strict=True)
    ]
    kw = {"analysis": analysis} if analysis is not None else {}
    store = fx.start(conn, fx.spec("XP-2", **kw))
    days = fx.equity_curves(conn, "XP-2", ctrl, treat)
    return store, days, x


def test_spec_without_analysis_keeps_the_d44_verdict_and_adds_descriptive_numbers(
    conn: sqlite3.Connection,
) -> None:
    store, days, x = _xp(conn, 25, analysis=None, beta=0.5)
    st_ = store.require("XP-2")
    plain = build_report(conn, st_, CFG, now=_after(days))
    spy = FakeSpy(_closes(days, x))
    rep = build_report(conn, st_, CFG, now=_after(days), market=spy)
    lr = rep.primary.adjustment
    assert lr is not None and not lr.pre_registered and lr.status == "fitted"
    assert rep.primary.hac is not None and rep.primary.hac.lag == CFG.stats.hac_lag
    assert lr.beta == pytest.approx(0.5, abs=0.05)
    assert rep.primary.sigma_raw is not None and rep.primary.sigma_adj < rep.primary.sigma_raw
    # the verdict and the D44 numbers are untouched
    for k in ("mean", "ci", "sigma", "sigma_source", "tau", "p_value", "lower_bound_positive"):
        assert getattr(rep.primary, k) == getattr(plain.primary, k), k
    assert rep.verdict == plain.verdict and rep.primary.primary_unadjusted is None
    assert rep.analysis is None and rep.covariate_missing is None
    assert [r.spy_ret for r in rep.series] == pytest.approx(x)
    assert rep.calibration.sigma_adj is not None and rep.calibration.hac_factor >= 1.0
    # no SPY: the D119 fields are absent from the JSON entirely
    doc = json.loads(plain.canonical_json())
    for k in ("adjustment", "hac", "sigma_raw", "sigma_adj", "sigma_lr", "primary_unadjusted"):
        assert k not in doc["primary"], k
    assert all("spy_ret" not in r for r in doc["series"])
    assert "sigma_adj" not in doc["calibration"] and "analysis" not in doc


def test_two_sessions_report_insufficient_n(conn: sqlite3.Connection) -> None:
    store, days, x = _xp(conn, 2, analysis=None)
    rep = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    a = rep.primary.adjustment
    assert a is not None and a.status == "insufficient_n" and a.beta is None and a.n == 2
    assert rep.primary.sigma_raw is not None and rep.primary.primary_unadjusted is None
    one = build_report(
        conn, store.require("XP-2"), CFG, now=fx.eod(days[0]) + dt.timedelta(minutes=15),
        market=FakeSpy(_closes(days, x)),
    )  # fmt: skip
    assert one.primary.adjustment is None


def test_pre_registered_adjusted_primary_drives_the_verdict(conn: sqlite3.Connection) -> None:
    store, days, x = _xp(conn, 25, analysis=ANALYSIS, beta=0.5)
    rep = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    lr = rep.primary.adjustment
    assert lr is not None and lr.pre_registered and lr.covariates == ["spy_ret"]
    assert rep.primary.mean == lr.estimate and rep.primary.ci == lr.ci
    assert rep.primary.sigma == rep.primary.sigma_lr and rep.primary.p_value == lr.p_value
    pu = rep.primary.primary_unadjusted
    assert pu is not None and pu.mean == pytest.approx(
        sum(r.d for r in rep.series) / len(rep.series)
    )
    # 400/day edge on 100k = 0.4%: the intercept recovers it once beta is stripped
    assert lr.estimate == pytest.approx(0.004, abs=0.0006)
    assert rep.analysis == Analysis(**ANALYSIS) and rep.covariate_missing == []
    assert rep.verdict == "win", rep.verdict_reason


def test_missing_spy_day_holds_an_adjusted_verdict(conn: sqlite3.Connection) -> None:
    store, days, x = _xp(conn, 25, analysis=ANALYSIS)
    closes = _closes(days, x)
    del closes[days[-1]]
    rep = build_report(conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(closes))
    assert rep.covariate_missing == [days[-1]]
    assert rep.verdict == "continue" and "waiting on SPY" in rep.verdict_reason
    assert not rep.primary.lower_bound_positive
    # no provider at all: every day missing, the verdict still waits (never the iid CI)
    none = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert none.verdict == "continue" and len(none.covariate_missing or []) == 25


def test_slack_line_notes_the_missing_spy_day_once(conn: sqlite3.Connection) -> None:
    from arc.slack.experiments import experiment_line

    store, days, x = _xp(conn, 25, analysis=ANALYSIS)
    closes = _closes(days, x)
    del closes[days[-1]]
    rep = build_report(conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(closes))
    text = experiment_line(rep).text
    assert text.count("Verdict on hold: no SPY return yet for") == 1
    assert days[-1].strftime("%b %-d") in text
    full = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    assert "on hold" not in experiment_line(full).text


def test_stored_spy_returns_reproduce_the_verdict_offline(conn: sqlite3.Connection) -> None:
    store, days, x = _xp(conn, 25, analysis=ANALYSIS)
    st_ = store.require("XP-2")
    online = build_report(conn, st_, CFG, now=_after(days), market=FakeSpy(_closes(days, x)))
    with conn:
        store_report(conn, online, run_id="t")
    assert stored_spy_returns(conn, "XP-2") == pytest.approx(dict(zip(days, x, strict=True)))
    offline = build_report(conn, st_, CFG, now=_after(days))  # no market: stored values
    assert offline.canonical_json() == online.canonical_json()
    # a later day fetches only that day
    spy = FakeSpy(_closes(days, x))
    build_report(conn, st_, CFG, now=_after(days), market=spy)
    assert spy.calls == []


def test_multi_arm_pre_registered_pairs_and_holm(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(31)
    n = 24
    x = list(rng.normal(0, 0.01, n))
    over = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
    sp = fx.spec(
        "XP-3", arms={"treatments": {f"t{i}": over for i in (1, 2, 3)}}, max_sessions=40,
        analysis=ANALYSIS,
    )  # fmt: skip
    store = fx.start(conn, sp)
    days = fx.sessions(n)
    fx.pnl_row(conn, previous_session(days[0]), fx.T0_EQUITY, None)
    ctrl = list(rng.normal(20, 300, n))
    eq = {"c": fx.T0_EQUITY, "t1": fx.T0_EQUITY, "t2": fx.T0_EQUITY, "t3": fx.T0_EQUITY}
    for i, day in enumerate(days):
        eq["c"] += ctrl[i]
        fx.pnl_row(conn, day, eq["c"], None)
        for name, edge, b in (("t1", 400, 0.6), ("t2", 0, 0.6), ("t3", 0, 0.0)):
            eq[name] += ctrl[i] + edge + b * x[i] * fx.T0_EQUITY + rng.normal(0, 100)
            fx.pnl_row(conn, day, eq[name], f"XP-3:{name}")
    conn.commit()
    rep = build_report(
        conn, store.require("XP-3"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    pairs = {p.b: p for p in rep.pairwise or [] if p.kind == "control_vs_treatment"}
    assert all(p.adjustment is not None and p.adjustment.pre_registered for p in pairs.values())
    assert pairs["t1"].verdict == "win"
    # t2's gain is pure SPY beta: the adjusted estimand does not call it a win
    assert pairs["t2"].adjustment.beta == pytest.approx(0.6, abs=0.1)  # type: ignore[union-attr]
    assert pairs["t2"].verdict != "win"
    assert pairs["t2"].primary_unadjusted is not None
    tt = [p for p in rep.pairwise or [] if p.kind == "treatment_vs_treatment"]
    assert all(p.adjustment is not None and p.hac is not None for p in tt)
    assert rep.calibration.beta_spread == pytest.approx(0.6, abs=0.15)


# ---------------------------------------------------------------------------
# A/A calibration (acceptance 5)
# ---------------------------------------------------------------------------


def test_aa_stop_records_adjusted_and_long_run_sigma(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(4)
    ctrl = list(rng.normal(0, 300, 10))
    treat = [c + e for c, e in zip(ctrl, rng.normal(0, 150, 10), strict=True)]
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", ctrl, treat)
    x = list(rng.normal(0, 0.01, 10))
    rep = evaluate(store, "XP-1", CFG, now=_after(days), market=FakeSpy(_closes(days, x)))
    assert rep.verdict == "futility"
    cal = store.aa_calibration()
    assert cal is not None and cal.sigma == pytest.approx(rep.calibration.sigma)
    assert cal.sigma_adj == pytest.approx(rep.calibration.sigma_adj)
    assert cal.hac_factor == pytest.approx(rep.calibration.hac_factor) and cal.hac_lag == 5
    assert cal.beta_spread == 0.0  # one pair
    assert store.aa_sigma() == cal.sigma
    assert store.aa_sigma(Analysis(**ANALYSIS)) == cal.sigma_adj
    assert store.aa_sigma(Analysis(covariates=[], hac_lag=5)) == cal.sigma
    c = rep.calibration
    assert (
        c.mde_fixed_lr
        and c.mde_always_valid_adj
        and c.sigma_lr == pytest.approx(c.sigma_adj * math.sqrt(c.hac_factor))
    )


def test_pre_d119_aa_stop_event_still_loads_and_adjusted_falls_back(
    conn: sqlite3.Connection,
) -> None:
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    from arc.experiments.models import StopReason

    store.stop("XP-1", StopReason.FUTILITY, actor="local", detail=StopDetail(sigma=0.003))
    # an old event's detail JSON carries only sigma/sessions/note
    raw = conn.execute("SELECT detail FROM experiment_events WHERE status = 'stopped'").fetchone()[
        0
    ]
    assert set(json.loads(raw)) == {"sigma"}  # exactly a pre-D119 stop row
    st2 = ExperimentStore(conn, now=lambda: fx.T0)
    assert st2.aa_sigma() == 0.003
    assert st2.aa_sigma(Analysis(**ANALYSIS)) is None  # running sigma for adjusted XPs
    cal = st2.aa_calibration()
    assert cal is not None and cal.sigma_adj is None and cal.hac_factor is None


def test_adjusted_ab_reads_the_aa_adjusted_sigma_and_factor(conn: sqlite3.Connection) -> None:
    store, days, x = _xp(conn, 25, analysis=ANALYSIS)
    cal = StopDetail(sigma=0.004, sigma_adj=0.0012, hac_factor=1.5, hac_lag=5)
    rep = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), aa_sigma=0.004, aa_cal=cal,
        market=FakeSpy(_closes(days, x)),
    )  # fmt: skip
    lr, h = rep.primary.adjustment, rep.primary.hac
    assert lr is not None and lr.sigma_source == "aa" and h is not None and h.source == "aa"
    assert h.factor == 1.5
    pu = rep.primary.primary_unadjusted
    assert pu is not None and pu.sigma == 0.004
    # a factor measured at another lag is not reused
    other = cal.model_copy(update={"hac_lag": 3})
    rep2 = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), aa_sigma=0.004, aa_cal=other,
        market=FakeSpy(_closes(days, x)),
    )  # fmt: skip
    assert rep2.primary.hac is not None and rep2.primary.hac.source == "running"


def test_report_lines_show_the_d119_numbers(conn: sqlite3.Connection) -> None:
    from arc.experiments.cli import report_lines

    store, days, x = _xp(conn, 25, analysis=ANALYSIS)
    rep = build_report(
        conn, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    text = "\n".join(report_lines(rep))
    assert "D119 pre-registered (spy_ret fitted, HAC lag 5)" in text
    assert "D44 iid unadjusted (transparency)" in text
    assert "SPY-adjusted" in text and "long-run MDE" in text


def test_tower_api_passes_the_d119_fields_through(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arc.tower.api import create_app

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    store, days, x = _xp(c, 25, analysis=ANALYSIS)
    rep = build_report(
        c, store.require("XP-2"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
    )
    with c:
        store_report(c, rep, run_id="t")
    c.close()
    client = TestClient(create_app(db, clock=lambda: _after(days)))
    r = client.get("/api/experiments/XP-2")
    assert r.status_code == 200
    doc = r.json()
    p = doc["report"]["primary"]
    assert p["adjustment"]["pre_registered"] is True and p["adjustment"]["status"] == "fitted"
    assert p["adjustment"]["beta"] is not None and p["adjustment"]["r2"] is not None
    assert p["hac"]["lag"] == 5 and len(p["hac"]["rho"]) == 5 and p["hac"]["source"]
    assert p["sigma_raw"] > 0 and p["sigma_adj"] > 0 and p["sigma_lr"] > 0
    assert p["primary_unadjusted"]["n"] == 25
    assert doc["report"]["series"][0]["spy_ret"] == pytest.approx(x[0])
    assert doc["report"]["analysis"] == ANALYSIS
    assert doc["spec"]["analysis"] == ANALYSIS
    assert doc["report"]["calibration"]["sigma_adj"] > 0
    # E25.8 (D120): the list row carries the headline primary's adjustment + unadjusted
    row = client.get("/api/experiments").json()["items"][0]
    assert row["adjustment"]["estimate"] == pytest.approx(p["adjustment"]["estimate"])
    assert row["primary_unadjusted"]["mean"] == pytest.approx(p["primary_unadjusted"]["mean"])


def test_tower_list_row_has_no_d119_fields_before_d119(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arc.tower.api import create_app

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    store, days, _ = _xp(c, 5, analysis=None)
    rep = build_report(c, store.require("XP-2"), CFG, now=_after(days))  # no SPY: pre-D119 shape
    with c:
        store_report(c, rep, run_id="t")
    c.close()
    client = TestClient(create_app(db, clock=lambda: _after(days)))
    row = client.get("/api/experiments").json()["items"][0]
    assert row["adjustment"] is None and row["primary_unadjusted"] is None
    assert row["primary_mean"] == pytest.approx(rep.primary.mean)
