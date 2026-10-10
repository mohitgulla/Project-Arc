"""E15.5: multi-arm reports (D69) — Holm, pairwise, all-arms view, K=1 byte-identity."""

from __future__ import annotations

import datetime as dt
import json
import math
from unittest import mock

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.experiments import stats
from arc.experiments.config import ExperimentsConfig
from arc.experiments.evaluate import ExperimentReport, build_report
from arc.experiments.multi import cross_arm_conflicts
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import previous_session
from tests import experiment_fixtures as fx
from tests.experiment_k1_scenarios import GOLDEN_DIR, SCENARIOS, render

OVERLAY = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}


def _conn():  # type: ignore[no-untyped-def]
    c = connect(":memory:")
    migrate(c)
    return c


def _spec(k: int, *, kind: str = "ab", **kw: object):  # type: ignore[no-untyped-def]
    arms = {"treatments": {f"t{i}": OVERLAY for i in range(1, k + 1)}}
    if kind == "aa":
        aa_arms = {"treatments": {f"t{i}": {} for i in range(1, k + 1)}}
        return fx.spec("XP-1", kind="aa", arms=aa_arms, **kw)
    return fx.spec("XP-2", arms=arms, **kw)


def _curves(conn, eid: str, ctrl: list[float], treats: dict[str, list[float]]) -> list[dt.date]:  # type: ignore[no-untyped-def]
    days = fx.sessions(len(ctrl))
    fx.pnl_row(conn, previous_session(days[0]), fx.T0_EQUITY, None)
    c = fx.T0_EQUITY
    eq = dict.fromkeys(treats, fx.T0_EQUITY)
    for i, day in enumerate(days):
        c += ctrl[i]
        fx.pnl_row(conn, day, c, None)
        for name, pnl in treats.items():
            if pnl[i] is None:
                continue
            eq[name] += pnl[i]
            fx.pnl_row(conn, day, eq[name], f"{eid}:{name}")
    conn.commit()
    return days


def _report(
    conn, eid: str, days: list[dt.date], *, aa_sigma: float | None = None
) -> ExperimentReport:  # type: ignore[no-untyped-def]
    from arc.experiments.store import ExperimentStore

    st_ = ExperimentStore(conn, now=lambda: fx.T0).require(eid)
    now = fx.eod(days[-1]) + dt.timedelta(minutes=15)
    with mock.patch("arc.experiments.evaluate._evaluator_sha", return_value="e" * 40):
        return build_report(conn, st_, ExperimentsConfig(), now=now, aa_sigma=aa_sigma)


# -- K=1 regression: byte-identical to the pre-E15.5 evaluator ---------------------


def _assert_same_report(got: object, want: object, path: str = "$") -> None:
    """Exact keys/order, strings, ints, bools and nulls; floats to 1e-12 relative.

    The goldens were captured on macOS; Linux numpy/libm can differ in the last ULP
    of a few floats (e.g. a p-value), which is not a v1 behaviour change.
    """
    if isinstance(want, float) or isinstance(got, float):
        assert isinstance(got, (int, float)) and isinstance(want, (int, float)), path
        assert not isinstance(got, bool) and not isinstance(want, bool), path
        assert math.isclose(got, want, rel_tol=1e-12, abs_tol=1e-300), (path, got, want)
    elif isinstance(want, dict):
        assert isinstance(got, dict) and list(got) == list(want), path
        for k in want:
            _assert_same_report(got[k], want[k], f"{path}.{k}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), path
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _assert_same_report(g, w, f"{path}[{i}]")
    else:
        assert type(got) is type(want) and got == want, (path, got, want)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_k1_report_is_byte_identical_to_pre_e155(name: str) -> None:
    want = (GOLDEN_DIR / f"{name}.json").read_text().rstrip("\n")
    got = render(name)
    _assert_same_report(json.loads(got), json.loads(want))
    assert '"report_version":1' in got
    for key in ("pairwise", "omnibus", "headline_arm", "cum_pnl_pct", "rank"):
        assert f'"{key}"' not in got


# -- Holm ---------------------------------------------------------------------------


def test_holm_known_values() -> None:
    adj = stats.holm_adjust([0.01, 0.04, 0.03, 0.2])
    np.testing.assert_allclose(adj, [0.04, 0.09, 0.09, 0.2])
    assert stats.holm_adjust([0.3]).tolist() == [0.3]
    assert stats.holm_adjust([]).size == 0
    assert stats.holm_adjust([0.9, 0.8]).tolist() == [1.0, 1.0]


@given(st.lists(st.floats(0, 1), min_size=1, max_size=12))
def test_holm_properties(ps: list[float]) -> None:
    adj = stats.holm_adjust(ps)
    a = np.asarray(ps)
    assert np.all(adj >= a - 1e-12)
    assert np.all(adj <= 1.0)
    order = np.argsort(a, kind="stable")
    assert np.all(np.diff(adj[order]) >= -1e-12)  # monotone in the p order
    # Holm rejects at least what Bonferroni rejects, never more than unadjusted
    m = len(ps)
    assert np.all((np.minimum(1, a * m) < 0.05) <= (adj < 0.05))
    assert np.all((adj < 0.05) <= (a < 0.05))


@given(st.lists(st.floats(0, 1), min_size=1, max_size=8), st.integers(0, 7), st.floats(0, 1))
def test_holm_monotone_in_each_p(ps: list[float], i: int, lower: float) -> None:
    i %= len(ps)
    smaller = list(ps)
    smaller[i] = min(ps[i], lower)
    assert np.all(stats.holm_adjust(smaller) <= stats.holm_adjust(ps) + 1e-12)


def test_msprt_p_values_matches_scalar() -> None:
    rng = np.random.default_rng(0)
    for _ in range(50):
        n, m, s, t = (
            int(rng.integers(1, 80)),
            rng.normal(0, 0.01),
            rng.uniform(1e-3, 0.02),
            rng.uniform(1e-3, 0.02),
        )
        assert stats.msprt_p_values(n, m, s, t) == pytest.approx(stats.msprt_p_value(n, m, s, t))


def test_sessions_needed_grows_with_k() -> None:
    a = stats.sessions_needed(0.004, 0.002, tau=0.002, alpha=0.05, power=0.8, k=1)
    b = stats.sessions_needed(0.004, 0.002, tau=0.002, alpha=0.05, power=0.8, k=4)
    assert a is not None and b is not None and b > a
    assert stats.sessions_needed(0.004, 1e-9, tau=0.002, alpha=0.05, power=0.8, cap=10) is None


@settings(deadline=None, max_examples=1)
@given(st.just(None))
def test_familywise_error_at_k4_under_global_null(_: None) -> None:
    """Simulated: K=4 null arms sharing control, daily looks, running-min p + Holm.

    FWER over the whole run must be <= alpha (+ simulation error). Unadjusted testing
    of the 4 arms must be visibly worse (the reason for the correction).
    """
    rng = np.random.default_rng(155)
    k, sims, n_max, alpha, sigma = 4, 2000, 60, 0.05, 0.004
    tau = stats.mixing_tau(sigma, mde=None, min_sessions=20)
    holm_err = raw_err = 0
    for _ in range(sims):
        ctrl = rng.normal(0, sigma / np.sqrt(2), n_max)
        arms = rng.normal(0, sigma / np.sqrt(2), (k, n_max))
        d = arms - ctrl  # correlated through the shared control (rho = 0.5)
        n = np.arange(1, n_max + 1)
        means = np.cumsum(d, axis=1) / n
        p = stats.msprt_p_values(n, means, sigma, tau)
        pmin = np.minimum.accumulate(p, axis=1)
        holm = stats.holm_adjust(pmin.T)  # looks x k
        holm_err += bool(np.any(holm[19:] < alpha))
        raw_err += bool(np.any(pmin[:, 19:] < alpha))
    fwer = holm_err / sims
    assert fwer <= alpha + 2 * np.sqrt(alpha * (1 - alpha) / sims)
    assert raw_err / sims > fwer


# -- multi-arm report -------------------------------------------------------------


def test_k3_report_shape_and_verdicts() -> None:
    c = _conn()
    rng = np.random.default_rng(7)
    n = 30
    ctrl = list(rng.normal(20, 300, n))
    noise = rng.normal(0, 100, (3, n))
    treats = {
        "t1": [x + 400 + e for x, e in zip(ctrl, noise[0], strict=True)],  # clear win
        "t2": [x - 400 + e for x, e in zip(ctrl, noise[1], strict=True)],  # clear loss
        "t3": [x + e for x, e in zip(ctrl, noise[2], strict=True)],  # null
    }
    fx.start(c, _spec(3, max_sessions=40))
    days = _curves(c, "XP-2", ctrl, treats)
    rep = _report(c, "XP-2", days)

    assert rep.report_version == 2 and rep.multi_arm
    assert [a.arm for a in rep.arms] == ["control", "t1", "t2", "t3"]
    by = {p.b: p for p in rep.pairwise or [] if p.kind == "control_vs_treatment"}
    assert by["t1"].verdict == "win" and by["t2"].verdict == "loss"
    assert by["t3"].verdict == "continue"
    assert by["t1"].decided_day is not None and by["t1"].n < n  # stopped early, frozen
    assert len(by["t1"].series) == by["t1"].n
    tt = [p for p in rep.pairwise or [] if p.kind == "treatment_vs_treatment"]
    assert [(p.a, p.b) for p in tt] == [("t1", "t2"), ("t1", "t3"), ("t2", "t3")]
    assert all(p.descriptive and p.verdict is None and p.holm_p is None for p in tt)
    assert rep.verdict == "continue"  # t3 still running
    assert rep.headline_arm == "t1"
    ranks = {a.arm: a.rank for a in rep.arms}
    assert ranks["t1"] == 1 and ranks["t2"] == 4 and ranks["control"] in (2, 3)
    assert rep.omnibus is not None and rep.omnibus.k == 3 and rep.omnibus.differs
    t1 = rep.arms[1]
    assert t1.cum_pnl_pct == pytest.approx(sum(treats["t1"]) / fx.T0_EQUITY)
    assert t1.cross_arm_conflicts == 0 and t1.verdict == "win"
    # round-trips through JSON (stored payload) unchanged
    again = ExperimentReport.model_validate_json(rep.canonical_json())
    assert again.canonical_json() == rep.canonical_json()


def test_k2_all_stop_futility_and_missing_session() -> None:
    c = _conn()
    rng = np.random.default_rng(3)
    n = 20
    ctrl = list(rng.normal(0, 300, n))
    t1 = [x + e for x, e in zip(ctrl, rng.normal(0, 200, n), strict=True)]
    t2: list[float | None] = [x + e for x, e in zip(ctrl, rng.normal(0, 200, n), strict=True)]
    t2[4] = None  # t2 missed a session; t1 did not
    fx.start(c, _spec(2, min_sessions=10, max_sessions=n - 2))
    days = _curves(c, "XP-2", ctrl, {"t1": t1, "t2": t2})  # type: ignore[dict-item]
    rep = _report(c, "XP-2", days)
    by = {p.b: p for p in rep.pairwise or [] if p.kind == "control_vs_treatment"}
    assert by["t1"].verdict == "futility" and by["t2"].verdict == "futility"
    # the gap also unpairs the next session (no prior arm close), as in v1
    assert by["t2"].missing_sessions == [days[4], days[5]] and not by["t1"].missing_sessions
    assert rep.missing_sessions == [days[4], days[5]]
    assert rep.verdict == "futility" and "no arm won" in rep.verdict_reason


def test_multi_arm_aa_invalid_on_any_pair() -> None:
    c = _conn()
    n = 12
    ctrl = [0.0] * n
    fx.start(c, _spec(3, kind="aa"))
    days = _curves(
        c, "XP-1", ctrl,
        {"t1": [5.0 * (i % 2) for i in range(n)], "t2": [300.0 + (i % 3) * 10 for i in range(n)],
         "t3": [-5.0 * (i % 2) for i in range(n)]},
    )  # fmt: skip
    rep = _report(c, "XP-1", days)
    assert rep.verdict == "invalid"
    assert all(p.holm_p is not None for p in rep.pairwise or [])  # every pair in the family


def test_multi_arm_aa_null_completes() -> None:
    c = _conn()
    rng = np.random.default_rng(1)
    n = 10
    ctrl = list(rng.normal(0, 300, n))
    treats = {
        f"t{i}": [x + e for x, e in zip(ctrl, rng.normal(0, 150, n), strict=True)] for i in (1, 2)
    }
    fx.start(c, _spec(2, kind="aa"))
    rep = _report(c, "XP-1", _curves(c, "XP-1", ctrl, treats))
    assert rep.verdict == "futility" and "every pair null" in rep.verdict_reason
    assert rep.sessions == n


def test_multi_arm_aa_partial_continues() -> None:
    c = _conn()
    rng = np.random.default_rng(1)
    ctrl = list(rng.normal(0, 300, 10))[:5]
    treats = {
        f"t{i}": [x + e for x, e in zip(ctrl, rng.normal(0, 150, 5), strict=True)] for i in (1, 2)
    }
    fx.start(c, _spec(2, kind="aa"))
    rep = _report(c, "XP-1", _curves(c, "XP-1", ctrl, treats))
    assert rep.verdict == "continue"


def test_cross_arm_conflicts_counted() -> None:
    c = _conn()
    fx.start(c, _spec(2))
    from arc.context.ttl import to_db

    for code, aid in (
        ("cross_arm_conflict", "XP-2:t1"),
        ("execution:cross_arm_conflict", "XP-2:t1"),
        ("cross_arm_conflict", "XP-2:t2"),
        ("gate_reject", "XP-2:t1"),
    ):
        c.execute(
            """INSERT INTO decisions (id, persona, stage, subject, choice, reason_code, at,
                   arm_id)
               VALUES (lower(hex(randomblob(8))), 'broker', 'execution', 'SPY', 'rejected',
                   ?, ?, ?)""",
            (code, to_db(fx.T0 + dt.timedelta(hours=1)), aid),
        )
    assert cross_arm_conflicts(c, "XP-2:t1", fx.T0) == 2
    assert cross_arm_conflicts(c, "XP-2:t2", fx.T0) == 1
    assert cross_arm_conflicts(c, None, fx.T0) == 0


def test_k1_spec_with_t1_name_stays_v1() -> None:
    c = _conn()
    fx.start(c, fx.spec("XP-2"))
    days = fx.equity_curves(c, "XP-2", [10.0] * 5, [20.0] * 5)
    rep = _report(c, "XP-2", days)
    assert rep.report_version == 1 and rep.pairwise is None
    assert json.loads(rep.canonical_json()).get("pairwise", "absent") == "absent"
