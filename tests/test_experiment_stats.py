"""E10.3 (D44): experiment stats, verdicts, reports, routine and CLI.

Every test injects ``now``; nothing reads the wall clock. The stats properties
are checked by simulation: type-I error with daily peeking over 60 sessions
(2k simulations) and power at the always-valid MDE.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import uuid

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.context.ttl import to_db
from arc.experiments import stats
from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.config import ExperimentsConfig, load_experiments_config
from arc.experiments.evaluate import (
    build_report,
    evaluate,
    evaluate_running,
    latest_report,
)
from arc.experiments.models import ExperimentStatus, StopReason, arm_id
from arc.experiments.store import ExperimentError, ExperimentStore
from arc.journal.reasons import ReasonCode
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import experiment_fixtures as fx

CFG = ExperimentsConfig()
ALPHA = 0.05


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _after(days: list[dt.date]) -> dt.datetime:
    return fx.eod(days[-1]) + dt.timedelta(minutes=15)


def _codes(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT reason_code FROM decisions ORDER BY rowid")]


# ---------------------------------------------------------------------------
# stats: pure functions
# ---------------------------------------------------------------------------


def test_halfwidth_matches_closed_form_and_shrinks() -> None:
    n, s, t, a = 20, 0.01, 0.005, 0.05
    v = s * s + n * t * t
    want = math.sqrt(
        2 * s * s * v / (n * n * t * t) * (math.log(1 / a) + 0.5 * math.log(v / s / s))
    )
    assert float(stats.msprt_halfwidth(n, s, t, a)) == pytest.approx(want)
    hw = stats.msprt_halfwidth(np.arange(1, 200), s, t, a)
    assert np.all(np.diff(hw) < 0)  # narrows every session
    # wider than the fixed-horizon 95% CI at the same n: the price of peeking
    assert float(stats.msprt_halfwidth(n, s, t, a)) > 1.96 * s / math.sqrt(n)


@settings(max_examples=200, deadline=None)
@given(
    n=st.integers(1, 500),
    sigma=st.floats(1e-5, 0.1),
    tau=st.floats(1e-5, 0.1),
    alpha=st.floats(0.001, 0.2),
)
def test_halfwidth_positive_and_monotone_in_alpha_and_sigma(
    n: int, sigma: float, tau: float, alpha: float
) -> None:
    hw = float(stats.msprt_halfwidth(n, sigma, tau, alpha))
    assert hw > 0 and math.isfinite(hw)
    assert float(stats.msprt_halfwidth(n, sigma, tau, alpha / 2)) > hw  # stricter alpha: wider
    assert float(stats.msprt_halfwidth(n, sigma * 2, tau, alpha)) > hw  # noisier: wider


@settings(max_examples=100, deadline=None)
@given(
    d=st.lists(st.floats(-0.05, 0.05, allow_nan=False), min_size=2, max_size=80),
    shift=st.floats(-0.01, 0.01),
)
def test_confidence_sequence_is_translation_equivariant(d: list[float], shift: float) -> None:
    kw = {"alpha": ALPHA, "sigma": 0.01, "sigma_upper_q": 0.05, "mde": None, "min_sessions": 20}
    a, _, _ = stats.confidence_sequence(d, **kw)  # type: ignore[arg-type]
    b, _, _ = stats.confidence_sequence([x + shift for x in d], **kw)  # type: ignore[arg-type]
    assert a is not None and b is not None
    assert b.lo == pytest.approx(a.lo + shift, abs=1e-12)
    assert b.hi == pytest.approx(a.hi + shift, abs=1e-12)
    assert a.lo <= a.estimate <= a.hi


@settings(max_examples=100, deadline=None)
@given(d=st.lists(st.floats(-0.05, 0.05, allow_nan=False), min_size=2, max_size=80))
def test_corrected_sigma_never_below_sample_sd(d: list[float]) -> None:
    s = stats.sample_sd(d)
    c = stats.corrected_sigma(d, 0.05)
    if s <= 0:
        assert c is None
    else:
        assert c is not None and c >= s


def test_confidence_sequence_needs_spread_or_known_sigma() -> None:
    kw = {"alpha": ALPHA, "sigma_upper_q": 0.05, "mde": None, "min_sessions": 20}
    assert stats.confidence_sequence([], sigma=None, **kw)[0] is None  # type: ignore[arg-type]
    assert stats.confidence_sequence([0.001], sigma=None, **kw)[0] is None  # type: ignore[arg-type]
    assert stats.confidence_sequence([0.0, 0.0], sigma=None, **kw)[0] is None  # type: ignore[arg-type]
    ci, sig, tau = stats.confidence_sequence([0.001], sigma=0.01, **kw)  # type: ignore[arg-type]
    assert ci is not None and sig == 0.01 and tau == pytest.approx(stats.mde_fixed(0.01, 20))
    ci2, _, tau2 = stats.confidence_sequence([0.001], sigma=0.01, **{**kw, "mde": 0.002})  # type: ignore[arg-type]
    assert tau2 == 0.002 and ci2 is not None


def test_mde_formulas() -> None:
    assert stats.mde_fixed(0.01, 25) == pytest.approx(2.8 * 0.01 / 5)
    tau = stats.mixing_tau(0.01, mde=None, min_sessions=20)
    av = stats.mde_always_valid(0.01, 60, tau=tau, alpha=0.05, power=0.8)
    assert av > stats.mde_fixed(0.01, 60)  # peeking costs power


def test_sortino_and_drawdown() -> None:
    assert stats.sortino([]) is None
    r = [0.01, -0.005, 0.002, -0.001]
    dd = math.sqrt((0.005**2 + 0.001**2) / 4)
    assert stats.sortino(r) == pytest.approx(np.mean(r) / dd * math.sqrt(252))
    assert stats.sortino([0.001, 0.002]) == pytest.approx(0.0015 / 1e-4 * math.sqrt(252))
    assert stats.max_drawdown([]) == 0.0
    assert stats.max_drawdown([100, 110, 99, 120, 108]) == pytest.approx(0.1)
    assert stats.max_drawdown([0.0, 0.0]) == 0.0


def test_sortino_diff_ci_deterministic_and_paired() -> None:
    rng = np.random.default_rng(3)
    c = rng.normal(0.0005, 0.01, 40)
    t = c + 0.0001
    a = stats.sortino_diff_ci(t, c, level=0.9, resamples=500, seed=7)
    b = stats.sortino_diff_ci(t, c, level=0.9, resamples=500, seed=7)
    assert a == b and a is not None
    assert a.lo <= a.estimate <= a.hi and a.lo > -0.5  # near-identical arms: tight around 0+
    assert stats.non_inferior(a, 0.5)
    assert not stats.non_inferior(None, 0.5)
    assert stats.sortino_diff_ci([0.1], [0.1], level=0.9, resamples=200, seed=1) is None
    with pytest.raises(ValueError, match="differ in length"):
        stats.sortino_diff_ci([0.1, 0.2], [0.1], level=0.9, resamples=200, seed=1)
    assert stats.seed_for("XP-1", 3) == stats.seed_for("XP-1", 3) != stats.seed_for("XP-1", 4)


# ---------------------------------------------------------------------------
# stats: simulated error rates (card acceptance)
# ---------------------------------------------------------------------------

SIMS, HORIZON, SIGMA, MIN_N = 2000, 60, 0.004, 20


def _vector_bounds(
    csum: np.ndarray, csq: np.ndarray, n: int, *, known_sigma: float | None
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised ``stats.confidence_sequence`` at look *n* for every sim: (lo, hi).

    Same parameters as production (CFG's sigma_upper_q, mde=None -> tau from
    MIN_N); ``test_vectorised_bounds_equal_production_confidence_sequence`` proves
    the rows equal the production function, so the sims test production maths.
    """
    from scipy.stats import chi2

    mean = csum[:, n - 1] / n
    if known_sigma is None:
        var = (csq[:, n - 1] - n * mean * mean) / (n - 1)
        q = CFG.stats.sigma_upper_q
        s = np.sqrt(np.maximum(var, 0)) * math.sqrt((n - 1) / chi2.ppf(q, n - 1))
    else:
        s = np.full(csum.shape[0], known_sigma)
    tau = stats.mixing_tau(1.0, mde=None, min_sessions=MIN_N) * s
    hw = stats.msprt_halfwidth(n, s, tau, ALPHA)
    return mean - hw, mean + hw


def _peeking_rejections(
    d: np.ndarray, *, known_sigma: float | None, first: int = 2
) -> tuple[np.ndarray, np.ndarray]:
    """(CI ever excluded 0 at any daily look, ever lower bound > 0 at a look >= MIN_N)."""
    sims = d.shape[0]
    any_ = np.zeros(sims, bool)
    win = np.zeros(sims, bool)
    csum = np.cumsum(d, axis=1)
    csq = np.cumsum(d * d, axis=1)
    for n in range(first, d.shape[1] + 1):
        lo, hi = _vector_bounds(csum, csq, n, known_sigma=known_sigma)
        any_ |= (lo > 0) | (hi < 0)
        if n >= MIN_N:
            win |= lo > 0
    return any_, win


@pytest.mark.parametrize("known_sigma", [SIGMA, None], ids=["aa_sigma", "running_corrected"])
def test_vectorised_bounds_equal_production_confidence_sequence(
    known_sigma: float | None,
) -> None:
    rng = np.random.default_rng(3)
    d = rng.normal(0.0002, SIGMA, size=(5, HORIZON))
    csum, csq = np.cumsum(d, axis=1), np.cumsum(d * d, axis=1)
    for n in (2, MIN_N, 37, HORIZON):
        lo, hi = _vector_bounds(csum, csq, n, known_sigma=known_sigma)
        for i in range(d.shape[0]):
            ci, _, _ = stats.confidence_sequence(
                d[i, :n],
                alpha=ALPHA,
                sigma=known_sigma,
                sigma_upper_q=CFG.stats.sigma_upper_q,
                mde=None,
                min_sessions=MIN_N,
            )
            assert ci is not None
            assert (lo[i], hi[i]) == pytest.approx((ci.lo, ci.hi), rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("known_sigma", [SIGMA, None], ids=["aa_sigma", "running_corrected"])
def test_type_one_error_with_daily_peeking_is_below_alpha(known_sigma: float | None) -> None:
    """Null (no effect), 2k sims x 60 daily looks: P(ever excluding 0) <= alpha."""
    rng = np.random.default_rng(20261003)
    d = rng.normal(0.0, SIGMA, size=(SIMS, HORIZON))
    any_, win = _peeking_rejections(d, known_sigma=known_sigma)
    # binomial SE at alpha with 2k sims is ~0.005; the bound holds with room
    assert any_.mean() <= ALPHA
    assert win.mean() <= ALPHA / 2


def test_type_one_error_holds_for_fat_tailed_days() -> None:
    """Student-t(4) days (fat tails, as option P&L is) keep the error below alpha."""
    rng = np.random.default_rng(7)
    d = rng.standard_t(4, size=(SIMS, HORIZON)) * SIGMA / math.sqrt(2)
    any_, _ = _peeking_rejections(d, known_sigma=None)
    assert any_.mean() <= ALPHA


@pytest.mark.parametrize("known_sigma", [SIGMA, None], ids=["aa_sigma", "running_corrected"])
def test_power_at_the_always_valid_mde(known_sigma: float | None) -> None:
    """A true shift of the always-valid MDE(60) is detected by session 60 with >= power."""
    tau = stats.mixing_tau(SIGMA, mde=None, min_sessions=MIN_N)
    mde = stats.mde_always_valid(SIGMA, HORIZON, tau=tau, alpha=ALPHA, power=0.8)
    rng = np.random.default_rng(11)
    d = rng.normal(mde, SIGMA, size=(SIMS, HORIZON))
    _, win = _peeking_rejections(d, known_sigma=known_sigma)
    if known_sigma is not None:
        assert win.mean() >= 0.8
    else:
        # the running estimate pays for not knowing sigma; documented in the report
        assert win.mean() >= 0.6
    # the fixed-horizon MDE at min_sessions is caught almost always by 60 sessions
    d2 = rng.normal(stats.mde_fixed(SIGMA, MIN_N), SIGMA, size=(SIMS, HORIZON))
    assert _peeking_rejections(d2, known_sigma=known_sigma)[1].mean() >= 0.9


# ---------------------------------------------------------------------------
# evaluation: series, legacy book, verdicts
# ---------------------------------------------------------------------------


def test_series_pairs_eod_snapshots_per_arm(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec())
    days = fx.equity_curves(conn, "XP-2", [100, -50, 30], [150, -20, 10])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert [s.day for s in r.series] == days
    assert [s.d for s in r.series] == pytest.approx([50e-5, 30e-5, -20e-5])
    assert r.primary.n == 3 and r.primary.mean == pytest.approx(20e-5)
    assert r.primary.sigma_source == "running_corrected"
    assert r.verdict == "continue" and r.missing_sessions == []
    assert r.arms[0].arm_id is None and r.arms[1].arm_id == "XP-2:treatment"
    assert r.arms[1].total_pnl == pytest.approx(140)
    assert r.as_of_day == days[-1]


def test_mid_session_t0_starts_next_session_and_today_not_missing(
    conn: sqlite3.Connection,
) -> None:
    t0 = dt.datetime(2026, 10, 5, 11, 0, tzinfo=ET)
    store = fx.start(conn, fx.spec(), t0=t0)
    days = fx.sessions(3)
    fx.pnl_row(conn, days[0], 100_000, None)  # t0's day: excluded, it is the baseline
    fx.pnl_row(conn, days[1], 100_100, None)
    fx.pnl_row(conn, days[1], 100_050, arm_id("XP-2", "treatment"))
    now = dt.datetime.combine(days[2], dt.time(12, 0), tzinfo=ET)  # today: no EOD yet
    r = build_report(conn, store.require("XP-2"), CFG, now=now)
    assert [s.day for s in r.series] == [days[1]]
    assert r.series[0].d == pytest.approx(-50 / 100_000)
    assert r.missing_sessions == []


def test_missing_arm_session_is_skipped_and_listed(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec())
    days = fx.sessions(3)
    t_arm = arm_id("XP-2", "treatment")
    from arc.utils.calendar import previous_session

    fx.pnl_row(conn, previous_session(days[0]), 100_000, None)
    for day, c, t in ((days[0], 100_100, 100_200), (days[2], 100_300, 100_100)):
        fx.pnl_row(conn, day, c, None)
        fx.pnl_row(conn, day, t, t_arm)
    fx.pnl_row(conn, days[1], 100_200, None)  # control only: the arm missed reconcile
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert [s.day for s in r.series] == [days[0]]  # day 3 needs day 2's arm close too
    assert r.missing_sessions == days[1:]


def test_treatment_reads_virtual_equity_not_broker_equity(conn: sqlite3.Connection) -> None:
    """Review E10.3 #1: the experiment account's broker equity is never reset.

    Broker $95k vs t0 $100k (and vs a $10k control), equal P&L, no pre-t0 arm row:
    the first session's d is 0 (not -5% / -850%) and nothing stops.
    """
    for t0_equity in (100_000.0, 10_000.0):
        c = connect(":memory:")
        migrate(c)
        store = fx.start(c, fx.spec(), t0_equity=t0_equity)
        days = fx.equity_curves(
            c, "XP-2", [10, 10, 10], [10, 10, 10], t0_equity=t0_equity, prior_close=t0_equity
        )
        raw = c.execute(
            "SELECT json_extract(details_json, '$.equity') FROM pnl_snapshots"
            " WHERE arm_id IS NOT NULL ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        assert float(raw) == t0_equity + 10 + fx.BROKER_EXCESS  # broker != virtual
        r = build_report(c, store.require("XP-2"), CFG, now=_after(days))
        assert [s.d for s in r.series] == pytest.approx([0.0, 0.0, 0.0])
        assert [s.treatment_equity for s in r.series] == pytest.approx(
            [t0_equity + 10, t0_equity + 20, t0_equity + 30]
        )
        assert r.arms[1].total_pnl == pytest.approx(30)
        assert r.arms[1].max_drawdown == pytest.approx(0.0)
        assert r.verdict == "continue" and r.missing_sessions == []


def test_treatment_row_without_virtual_equity_is_missing_not_fabricated(
    conn: sqlite3.Connection,
) -> None:
    """No ``virtual_equity`` -> the session is missing; broker ``equity`` is no fallback."""
    store = fx.start(conn, fx.spec())
    days = fx.sessions(3)
    t_arm = arm_id("XP-2", "treatment")
    from arc.utils.calendar import previous_session

    fx.pnl_row(conn, previous_session(days[0]), 100_000, None)
    for i, day in enumerate(days, start=1):
        fx.pnl_row(conn, day, 100_000 + 10 * i, None)
        fx.pnl_row(conn, day, 100_000 + 10 * i, t_arm, virtual=day != days[0])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    # day 1 has only broker equity (95,010): missing; day 2 then lacks its previous
    # virtual close and is missing too; day 3 pairs on virtual equity alone
    assert r.missing_sessions == days[:2]
    assert [s.day for s in r.series] == [days[2]]
    assert r.series[0].d == pytest.approx(0.0)
    assert r.series[0].treatment_equity == pytest.approx(100_030)


def test_aa_sigma_is_not_inflated_by_the_broker_excess(conn: sqlite3.Connection) -> None:
    """An A/A with identical arms on a $95k account vs $100k control: d == 0, never invalid."""
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", [25, -40, 15, 5], [25, -40, 15, 5])
    r = build_report(conn, store.require("XP-1"), CFG, now=_after(days))
    assert [s.d for s in r.series] == pytest.approx([0.0] * 4)
    assert r.verdict == "continue"


def test_legacy_book_excluded_from_control(conn: sqlite3.Connection) -> None:
    """Control's equity moves with a legacy structure; its P&L is removed from the series."""
    sid = "os-legacy"
    store = fx.start(conn, fx.spec(), legacy=[sid])
    days = fx.sessions(3)
    from arc.utils.calendar import previous_session

    prev = previous_session(days[0])
    fx.legacy_snapshot(conn, prev, sid, 500.0)
    # day 1: legacy +200, control's own trades +100 -> equity +300
    # day 2: legacy -100, own -50                    -> equity -150
    # day 3: legacy closed for 650 cash (mark 400 -> gone): legacy +250, own +10 -> +260
    fx.legacy_snapshot(conn, days[0], sid, 700.0)
    fx.legacy_snapshot(conn, days[1], sid, 600.0)
    fx.legacy_snapshot(conn, days[2], sid, None)
    _close_legacy(conn, sid, days[2], cash=850.0)
    days2 = fx.equity_curves(conn, "XP-2", [300, -150, 260], [100, -50, 10])
    assert days2 == days
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert [s.legacy_pnl for s in r.series] == pytest.approx([200, -100, 250])
    assert [s.control_pnl for s in r.series] == pytest.approx([100, -50, 10])
    assert all(s.d == pytest.approx(0.0) for s in r.series)


def _close_legacy(conn: sqlite3.Connection, sid: str, day: dt.date, *, cash: float) -> None:
    h = uuid.uuid4().hex
    at = to_db(fx.eod(day) - dt.timedelta(hours=3))
    conn.execute(
        """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)
           VALUES (?, 'SPY', 'bullish', 'm', 0.5, ?)""",
        (h, at),
    )
    conn.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at)
           VALUES (?, ?, ?, '{}', 't', '{}', '{}', ?, ?)""",
        (h, h, h, at, at),
    )
    conn.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, opened_at, status, closed_at, close_net)
           VALUES (?, 'SPY', ?, ?, '{}', 1, '5.0', ?, 'closed', ?, ?)""",
        (sid, h, h, to_db(fx.T0 - dt.timedelta(days=3)), at, str(-cash / 100)),
    )
    c = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, kind)
           VALUES (?, ?, ?, '{}', 't', '{}', '{}', ?, ?, 'close')""",
        (c, h, c, at, at),
    )
    conn.execute(
        """INSERT INTO executions (proposal_hash, kind, structure_id, status, token_version,
               band_lo, band_hi, max_steps, attempts, contracts, filled_qty, fill_price,
               started_at, finished_at)
           VALUES (?, 'close', ?, 'filled', 'arc2', '-9', '-8', 4, 2, 1, 1, ?, ?, ?)""",
        (c, sid, str(-cash / 100), at, at),
    )
    conn.commit()


def test_legacy_expiry_without_execution_uses_close_net(conn: sqlite3.Connection) -> None:
    sid = "os-exp"
    store = fx.start(conn, fx.spec(), legacy=[sid])
    days = fx.sessions(1)
    from arc.utils.calendar import previous_session

    fx.legacy_snapshot(conn, previous_session(days[0]), sid, 300.0)
    fx.legacy_snapshot(conn, days[0], sid, None)
    _close_legacy(conn, sid, days[0], cash=0.0)
    conn.execute("DELETE FROM executions WHERE structure_id = ?", (sid,))  # expired: no order
    fx.equity_curves(conn, "XP-2", [-300], [0])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert r.series[0].legacy_pnl == pytest.approx(-300)
    assert r.series[0].d == pytest.approx(0.0)


def test_legacy_mark_missing_skips_the_session(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec(), legacy=["os-x"])
    days = fx.equity_curves(conn, "XP-2", [10], [10])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert r.series == [] and r.missing_sessions == days


def _ab_with(
    conn: sqlite3.Connection, ctrl: list[float], treat: list[float], **kw: object
) -> tuple[ExperimentStore, list[dt.date]]:
    store = fx.start(conn, fx.spec("XP-2", **kw))
    days = fx.equity_curves(conn, "XP-2", ctrl, treat)
    return store, days


def test_win_needs_min_sessions_lower_bound_and_non_inferiority(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(5)
    ctrl = list(rng.normal(20, 300, 25))
    treat = [c + 400 + e for c, e in zip(ctrl, rng.normal(0, 100, 25), strict=True)]
    store, days = _ab_with(conn, ctrl, treat)
    st_ = store.require("XP-2")
    # before min_sessions: CI may already exclude 0 but no win
    early = build_report(conn, st_, CFG, now=_after(days[:10]))
    assert early.primary.lower_bound_positive and early.verdict == "continue"
    r = evaluate(store, "XP-2", CFG, now=_after(days))
    assert r.verdict == "win" and r.secondary.non_inferior is True
    s = store.require("XP-2")
    assert s.status is ExperimentStatus.STOPPED and s.reason is StopReason.WIN
    assert s.stop is not None and s.stop.sessions == 25 and s.stop.sigma is None
    assert ReasonCode.EXPERIMENT_EVALUATED.value in _codes(conn)


def test_positive_primary_but_inferior_sortino_is_not_a_win(conn: sqlite3.Connection) -> None:
    """Treatment earns more on average but with far worse downside: no win (continue)."""
    rng = np.random.default_rng(9)
    n = 30
    ctrl = list(np.abs(rng.normal(30, 5, n)))  # steady small gains, no losing day
    treat = [c + (900 if i % 2 else -650) for i, c in enumerate(ctrl)]
    store, days = _ab_with(conn, ctrl, treat, non_inferiority_margin=0.5, max_sessions=40)
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert r.primary.mean is not None and r.primary.mean > 0
    assert r.secondary.non_inferior is False
    assert r.verdict == "continue"


def test_futility_at_max_sessions(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(2)
    ctrl = list(rng.normal(0, 300, 20))
    treat = list(rng.normal(0, 300, 20))
    store, days = _ab_with(conn, ctrl, treat, min_sessions=10, max_sessions=20)
    r = evaluate(store, "XP-2", CFG, now=_after(days))
    assert r.verdict == "futility"
    assert store.require("XP-2").reason is StopReason.FUTILITY


def test_aa_never_wins_records_sigma_and_unlocks_ab(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(4)
    ctrl = list(rng.normal(0, 300, 10))
    treat = [c + e for c, e in zip(ctrl, rng.normal(0, 150, 10), strict=True)]
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", ctrl, treat)
    r = evaluate(store, "XP-1", CFG, now=_after(days))
    assert r.kind.value == "aa" and r.verdict == "futility"
    assert r.secondary.non_inferior is None
    sd = stats.sample_sd([s.d for s in r.series])
    assert r.calibration.sigma == pytest.approx(sd)
    assert set(r.calibration.mde_fixed) == {10, 20, 40, 60}
    assert r.calibration.mde_fixed[20] == pytest.approx(2.8 * sd / math.sqrt(20))
    assert all(r.calibration.mde_always_valid[k] > r.calibration.mde_fixed[k] for k in (10, 20, 60))
    assert store.aa_sigma() == pytest.approx(sd)  # E10.1's gate for ab starts


def test_aa_that_wins_is_invalid(conn: sqlite3.Connection) -> None:
    ctrl = [0.0] * 10
    treat = [300.0 + (i % 3) * 10 for i in range(10)]  # systematic gap: harness broken
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", ctrl, treat)
    r = evaluate(store, "XP-1", CFG, now=_after(days))
    assert r.verdict == "invalid"
    assert store.require("XP-1").reason is StopReason.INVALID
    assert ReasonCode.EXPERIMENT_INVALID.value in _codes(conn)


def test_ab_uses_aa_sigma_when_recorded(conn: sqlite3.Connection) -> None:
    aa = fx.start(conn, fx.spec("XP-1", kind="aa", max_sessions=10, min_sessions=10))
    aa.stop("XP-1", StopReason.FUTILITY, actor="arc.experiments",
            detail=__import__("arc.experiments.models", fromlist=["StopDetail"]).StopDetail(
                sigma=0.002))  # fmt: skip
    store = ExperimentStore(conn, now=lambda: fx.T0)
    store.create(fx.spec(), actor="local", owner_approval="P-1")
    store.register("XP-2", actor="local", owner_approval="P-1")
    from arc.experiments.models import RunningDetail

    store.start("XP-2", RunningDetail(t0=fx.T0, t0_equity=1e5, control_sha=fx.CONTROL_SHA),
                actor="arc.runner")  # fmt: skip
    days = fx.equity_curves(conn, "XP-2", [10, 20], [30, 10])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days), aa_sigma=0.002)
    assert r.primary.sigma == 0.002 and r.primary.sigma_source == "aa"


# ---------------------------------------------------------------------------
# no guardrails (owner 2026-10-03): losses are reported, never a stop
# ---------------------------------------------------------------------------


def test_large_losses_are_reported_not_stopped(conn: sqlite3.Connection) -> None:
    """A 3.5% drawdown and a -2.2% day would have tripped the old D44 guardrails."""
    store, days = _ab_with(conn, [0.0] * 5, [-900.0, -900.0, -900.0, -800.0, -2_200.0])
    t_arm = arm_id("XP-2", "treatment")
    fx.executions(conn, None, 8, day=days[0])
    fx.executions(conn, t_arm, 10, attempts=2, day=days[0])  # 2.5x control's orders
    r = evaluate(store, "XP-2", CFG, now=_after(days))
    assert r.verdict == "continue"
    assert store.require("XP-2").status is ExperimentStatus.RUNNING
    treat = r.arms[1]
    assert treat.max_drawdown == pytest.approx(0.057, abs=1e-3)
    assert treat.worst_day is not None and treat.worst_day < -0.02
    assert (r.arms[0].orders, treat.orders) == (8, 20)
    assert "guardrails" not in r.model_dump()
    assert "experiment:guardrail" not in _codes(conn)


def test_legacy_executions_not_counted_as_control_orders(conn: sqlite3.Connection) -> None:
    sid = "os-legacy"
    store = fx.start(conn, fx.spec(), legacy=[sid])
    days = fx.sessions(1)
    _close_legacy(conn, sid, days[0], cash=100)
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert r.arms[0].orders == 0


# ---------------------------------------------------------------------------
# report storage, calibration, breakdowns, provenance
# ---------------------------------------------------------------------------


def test_report_stored_append_only_and_round_trips(conn: sqlite3.Connection) -> None:
    store, days = _ab_with(conn, [10.0, 20.0], [15.0, 10.0])
    r1 = evaluate(store, "XP-2", CFG, now=_after(days[:1]))
    r2 = evaluate(store, "XP-2", CFG, now=_after(days))
    assert latest_report(conn, "XP-2") == r2 != r1
    row = conn.execute(
        "SELECT verdict, sessions, spec_hash, config_hash, control_sha, report_hash, payload "
        "FROM experiment_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["sessions"] == 2 and row["verdict"] == "continue"
    assert row["spec_hash"] == store.require("XP-2").registered_hash
    assert row["control_sha"] == fx.CONTROL_SHA and row["report_hash"] == r2.report_hash()
    assert r2.config["stats"]["sigma_upper_q"] == 0.05
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE experiment_reports SET verdict = 'win'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM experiment_reports")
    assert latest_report(conn, "XP-9") is None


def test_report_is_deterministic(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(1)
    store, days = _ab_with(conn, list(rng.normal(0, 200, 8)), list(rng.normal(0, 200, 8)))
    st_ = store.require("XP-2")
    a = build_report(conn, st_, CFG, now=_after(days))
    b = build_report(conn, st_, CFG, now=_after(days))
    assert a.canonical_json() == b.canonical_json()


def test_provenance_and_calibration_gaps(conn: sqlite3.Connection) -> None:
    store, days = _ab_with(conn, [10.0, 20.0, -5.0], [15.0, 10.0, 0.0])
    t_arm = arm_id("XP-2", "treatment")
    fx.executions(conn, None, 2, day=days[0])
    fx.executions(conn, t_arm, 2, day=days[0])
    _manifest(conn, "chain-c1", None, payload={"git_sha": "c" * 40})
    _manifest(conn, "chain-t1", t_arm, payload={"git_sha": fx.TREATMENT_SHA,
                                                 "paired_chain_run_id": "chain-c1"})  # fmt: skip
    _manifest(conn, "chain-t2", t_arm, payload={"paired_chain_run_id": "chain-c2"})
    for chain, aid, choice in (
        ("chain-c1", None, "selected"),
        ("chain-t1", t_arm, "selected"),
        ("chain-c2", None, "selected"),
        ("chain-t2", t_arm, "rejected"),
    ):
        conn.execute(
            """INSERT INTO decisions (id, chain_run_id, persona, stage, subject, choice,
                   reason_code, at, arm_id)
               VALUES (?, ?, 'research', 'shortlist', 'SPY', ?, 'research:selected', ?, ?)""",
            (uuid.uuid4().hex, chain, choice, to_db(fx.eod(days[0])), aid),
        )
    _outcome(conn, None, slippage=4.0, regime="bull", pnl="120", day=days[1])
    _outcome(conn, t_arm, slippage=9.0, regime="bull", pnl="-30", day=days[1])
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert r.treatment_sha == fx.TREATMENT_SHA and r.control_sha == fx.CONTROL_SHA
    c = r.calibration
    assert (c.paired_chains, c.divergent_chains, c.llm_divergence_rate) == (2, 1, 0.5)
    assert c.slippage_gap_bps == pytest.approx(5.0)
    assert c.fill_rate_gap == pytest.approx(0.0)
    by = {(b.by, b.key, b.arm): b for b in r.breakdowns}
    assert by[("regime", "bull", "control")].realised_pnl == 120
    assert by[("structure_kind", "vertical_debit", "treatment")].trades == 1


def _manifest(
    conn: sqlite3.Connection, chain: str, aid: str | None, *, payload: dict[str, object]
) -> None:
    rid = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, chain_run_id, reason, scheduled_for,
               status, started_at)
           VALUES (?, ?, ?, 'schedule', ?, 'ok', ?)""",
        (rid, f"research:{chain}", chain, to_db(fx.T0 + dt.timedelta(hours=2)),
         to_db(fx.T0 + dt.timedelta(hours=2))),
    )  # fmt: skip
    conn.execute(
        """INSERT INTO run_manifests (id, run_id, attempt, job, chain_run_id, status,
               schema_version, payload, created_at, arm_id)
           VALUES (?, ?, 1, 'research', ?, 'ok', 1, ?, ?, ?)""",
        (uuid.uuid4().hex, rid, chain, json.dumps(payload),
         to_db(fx.T0 + dt.timedelta(hours=2)), aid),
    )  # fmt: skip


def _outcome(
    conn: sqlite3.Connection, aid: str | None, *, slippage: float, regime: str, pnl: str,
    day: dt.date,
) -> None:  # fmt: skip
    h = uuid.uuid4().hex
    at = to_db(fx.eod(day))
    conn.execute(
        """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)
           VALUES (?, 'SPY', 'bullish', 'm', 0.5, ?)""",
        (h, at),
    )
    conn.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, regime, arm_id)
           VALUES (?, ?, ?, '{"kind": "vertical_debit"}', 't', '{}', '{}', ?, ?, ?, ?)""",
        (h, h, h, at, at, regime, aid),
    )
    conn.execute(
        """INSERT INTO outcomes (id, proposal_hash, status, slippage_bps, realised_pnl, at,
               arm_id)
           VALUES (?, ?, 'closed', ?, ?, ?, ?)""",
        (uuid.uuid4().hex, h, slippage, pnl, at, aid),
    )


def test_evaluate_refuses_non_running(conn: sqlite3.Connection) -> None:
    store = ExperimentStore(conn, now=lambda: fx.T0)
    store.create(fx.spec(), actor="local", owner_approval="P-1")
    with pytest.raises(ExperimentError, match="not running"):
        evaluate(store, "XP-2", CFG, now=fx.T0)
    with pytest.raises(ExperimentError, match="never started"):
        build_report(conn, store.require("XP-2"), CFG, now=fx.T0)


def test_evaluate_running_covers_every_running_experiment(conn: sqlite3.Connection) -> None:
    fx.start(conn, fx.spec("XP-1", kind="aa"))
    store = fx.start(conn, fx.spec("XP-2"))
    days = fx.equity_curves(conn, "XP-2", [1.0], [2.0])
    fx.pnl_row(conn, days[0], 100_003, arm_id("XP-1", "treatment"))
    out = evaluate_running(store, CFG, now=_after(days))
    assert [r.experiment_id for r in out] == ["XP-1", "XP-2"]


def test_effective_stats_config_tunable(conn: sqlite3.Connection) -> None:
    from arc.config import ArcSettings
    from arc.control.effective import effective_settings, experiments_config
    from arc.control.service import ControlService

    svc = ControlService(conn, base=ArcSettings(_env_file=None))  # type: ignore[call-arg]
    r = svc.set("experiments.stats.sigma_upper_q", "0.1", actor="local", source="cli")
    if r.pending is not None:
        svc.confirm(r.pending.code, actor="local", source="cli")
    assert experiments_config(effective_settings(conn)).stats.sigma_upper_q == 0.1
    assert load_experiments_config().stats.bootstrap_resamples == 2000


# ---------------------------------------------------------------------------
# routine + CLI
# ---------------------------------------------------------------------------


def test_routine_declared_after_auditor_and_resolves() -> None:
    from arc.routines.config import load_routines
    from arc.routines.handlers import resolve_handler

    rc = load_routines()
    spec = rc.personas["experiments.evaluate"]
    assert spec.schedule == [dt.time(16, 40)] and spec.halt_exempt and spec.llm is False
    assert spec.writes == []
    assert min(rc.personas["broker.reconcile"].schedule) < spec.schedule[0]
    assert resolve_handler("experiments.evaluate", spec).__name__ == "experiments_evaluate_step"


def test_routine_step_stops_invalid_aa_with_notice(conn: sqlite3.Connection) -> None:
    from arc.routines.experiments import experiments_evaluate_step

    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(conn, "XP-1", [0.0] * 10, [300.0 + (i % 3) * 10 for i in range(10)])
    ctx = _ctx(conn, _after(days))
    res = experiments_evaluate_step(ctx)  # type: ignore[arg-type]
    assert "XP-1 invalid n=10" in res.summary
    # E10.5: the stop card is the alert (no duplicate notice in the same thread)
    assert res.notice == ""
    assert [c.text.split(" • ")[0] for c in res.extra_cards] == ["[XP-1] A/A Day 10"]
    assert "Invalid" in res.extra_cards[0].text
    assert res.metrics["stopped"] == 1 and ctx.inputs == ["experiment:XP-1"]
    assert store.require("XP-1").reason is StopReason.INVALID


def test_routine_step_no_notice_while_running(conn: sqlite3.Connection) -> None:
    from arc.routines.experiments import experiments_evaluate_step

    store, days = _ab_with(conn, [0.0, 0.0], [500.0, -2_200.0])
    res = experiments_evaluate_step(_ctx(conn, _after(days)))  # type: ignore[arg-type]
    assert "XP-2 continue n=2" in res.summary and res.notice == ""
    assert [c.text for c in res.extra_cards] == [
        "[XP-2] Day 2 • " + res.extra_cards[0].text.split(" • ", 1)[1]
    ]
    assert res.extra_cards[0].blocks == []
    assert store.require("XP-2").status is ExperimentStatus.RUNNING


class _Ctx:
    def __init__(self, conn: sqlite3.Connection, now: dt.datetime) -> None:
        from arc.config import ArcSettings
        from arc.routines.config import load_routines

        self.conn, self.now, self.run_id = conn, now, "run-1"
        self.settings = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        self.inputs: list[str] = []
        self.options: dict[str, object] = {}
        self.routines = load_routines()

    def record_input(self, name: str, *_: object, **__: object) -> None:
        self.inputs.append(name)


def _ctx(conn: sqlite3.Connection, now: dt.datetime) -> _Ctx:
    return _Ctx(conn, now)


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["experiment", *argv])


def test_cli_report_and_evaluate(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "x.db"
    c = connect(db)
    migrate(c)
    fx.start(c, fx.spec())
    days = fx.equity_curves(c, "XP-2", [100, -50, 30], [150, -20, 10])
    c.close()
    now = _after(days).isoformat()
    assert run_experiment(_cli("report", "XP-2", "--db", str(db), "--now", now)) == 0
    out = capsys.readouterr().out
    assert "verdict CONTINUE" in out and "always-valid 95% CI" in out
    assert "guardrails" not in out and "arms:" in out
    assert "Changes shipped to all arms during this run" in out  # D86 (E21.1)
    assert run_experiment(_cli("report", "XP-2", "--db", str(db), "--stored")) == 2  # none yet
    capsys.readouterr()
    assert run_experiment(_cli("evaluate", "--db", str(db), "--now", now, "--json")) == 0
    out = capsys.readouterr().out
    stored = json.loads(out[out.index("[\n") :])  # structlog prints to stdout under pytest
    assert stored[0]["sessions"] == 3
    assert run_experiment(_cli("report", "XP-2", "--db", str(db), "--stored", "--json")) == 0
    out = capsys.readouterr().out
    again = json.loads(out[out.index("{\n") :])
    assert again == stored[0]
    assert run_experiment(_cli("evaluate", "XP-2", "--db", str(db), "--now", now)) == 0
    out = capsys.readouterr().out
    assert "XP-2" in out and "Changes shipped" not in out  # report only
    assert run_experiment(_cli("report", "XP-9", "--db", str(db))) == 2
    assert run_experiment(_cli("evaluate", "--db", str(tmp_path / "e.db"))) == 0
    assert "no running experiments" in capsys.readouterr().out


def test_cli_evaluate_refuses_stopped(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "x.db"
    c = connect(db)
    migrate(c)
    store = fx.start(c, fx.spec())
    store.stop("XP-2", StopReason.OWNER, actor="local")
    c.close()
    assert run_experiment(_cli("evaluate", "XP-2", "--db", str(db))) == 2
    assert "is stopped" in capsys.readouterr().err
    assert run_experiment(_cli("report", "XP-2", "--db", str(db), "--now", fx.T0.isoformat())) == 0


def test_robust_to_bad_rows_and_excludes_legacy_outcomes(conn: sqlite3.Connection) -> None:
    """Unparseable equity rows are ignored; the legacy book's outcomes and open trades never
    enter the arm summaries or breakdowns; a structure with no kind reports `unknown`."""
    sid = "os-legacy"
    store = fx.start(conn, fx.spec(), legacy=[sid])
    days = fx.sessions(2)
    from arc.utils.calendar import previous_session

    fx.legacy_snapshot(conn, previous_session(days[0]), sid, 100.0)
    fx.legacy_snapshot(conn, days[0], sid, 100.0)
    fx.legacy_snapshot(conn, days[1], sid, None)
    _close_legacy(conn, sid, days[1], cash=100.0)
    fx.equity_curves(conn, "XP-2", [0.0, 0.0], [0.0, 0.0])
    for bad in ("n/a", "", "NaN"):
        conn.execute(
            """INSERT INTO pnl_snapshots (id, snapshot_at, realized, unrealized, total,
                   details_json) VALUES (?, ?, '0', '0', '0', ?)""",
            (uuid.uuid4().hex, to_db(fx.eod(days[0]) - dt.timedelta(hours=1)),
             json.dumps({"day": days[0].isoformat(), "equity": bad})),
        )  # fmt: skip
    legacy_hash = conn.execute(
        "SELECT open_proposal_hash FROM open_structures WHERE id = ?", (sid,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO outcomes (id, proposal_hash, status, realised_pnl, at)
           VALUES ('o-legacy', ?, 'closed', '999', ?)""",
        (legacy_hash, to_db(fx.eod(days[1]))),
    )
    _outcome(conn, None, slippage=1.0, regime="bear", pnl="5", day=days[1])
    conn.execute("UPDATE proposals SET structure_json = 'not json' WHERE regime = 'bear'")
    h = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO outcomes (id, proposal_hash, status, at) VALUES (?, ?, 'open', ?)""",
        (h, h, to_db(fx.eod(days[1]))),
    )
    r = build_report(conn, store.require("XP-2"), CFG, now=_after(days))
    assert len(r.series) == 2 and all(s.d == pytest.approx(0) for s in r.series)
    keys = {(b.by, b.key, b.arm, b.trades) for b in r.breakdowns}
    assert keys == {
        ("regime", "bear", "control", 1),
        ("structure_kind", "unknown", "control", 1),
    }
    assert r.arms[0].mean_slippage_bps == 1.0
    from arc.experiments.evaluate import _structure_kind

    assert _structure_kind(None) == "unknown" and _structure_kind("[]") == "unknown"
