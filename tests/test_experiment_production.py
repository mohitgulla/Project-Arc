"""E25.3 (D108): spec v3 reports — Ti vs control verdicts, production panel, v2 unchanged."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any
from unittest import mock

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.context.ttl import to_db
from arc.experiments import stats
from arc.experiments.config import ExperimentsConfig
from arc.experiments.evaluate import ExperimentReport, build_report
from arc.experiments.production import RAMP_WINDOW, ramp_flags
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import previous_session
from tests import experiment_fixtures as fx
from tests.experiment_v2_scenarios import GOLDEN_DIR, SCENARIOS, render
from tests.golden_compare import assert_json_close

OVERLAY = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
BOOK = "start-book-1"


def _conn():  # type: ignore[no-untyped-def]
    c = connect(":memory:")
    migrate(c)
    return c


def _v3(eid: str = "XP-30", *, k: int = 3, kind: str = "ab", **kw: Any):  # type: ignore[no-untyped-def]
    if kind == "aa":
        arms = {"treatments": {f"t{i}": {} for i in range(1, k + 1)}}
    else:
        arms = {"treatments": {f"t{i}": OVERLAY for i in range(1, k + 1)}}
    return fx.spec(eid, kind=kind, arms=arms, spec_version=3, **kw)


def _snap(c, day: dt.date, aid: str | None, held: dict[str, float]) -> None:  # type: ignore[no-untyped-def]
    """An EOD positions snapshot holding *held* (structure id -> market value)."""
    structures, broker = [], []
    for i, (sid, value) in enumerate(sorted(held.items())):
        sym = f"SPY26112{i}C00600000"
        structures.append({"structure_id": sid, "ticker": "SPY", "legs": {sym: 1}, "held": True})
        broker.append({"symbol": sym, "qty": "1", "market_value": str(value)})
    c.execute(
        """INSERT INTO positions_snapshots (id, snapshot_at, positions_json, arm_id)
           VALUES (?, ?, ?, ?)""",
        (
            uuid.uuid4().hex,
            to_db(fx.eod(day)),
            json.dumps({"day": day.isoformat(), "structures": structures, "broker": broker}),
            aid,
        ),
    )


def _book(
    c,  # type: ignore[no-untyped-def]
    eid: str,
    *,
    p: list[float],
    ctrl: list[float],
    treats: dict[str, list[float]],
    book_marks: list[float | None] | None = None,
    p_open: list[int] | None = None,
    c_open: list[int] | None = None,
) -> list[dt.date]:
    """Production (NULL, broker equity), control and treatment arms (virtual equity)."""
    days = fx.sessions(len(ctrl))
    pe = fx.T0_EQUITY
    fx.pnl_row(c, previous_session(days[0]), pe, None)
    if book_marks is not None:
        _snap(c, previous_session(days[0]), None, {BOOK: 500.0})
    ce = fx.T0_EQUITY
    te = dict.fromkeys(treats, fx.T0_EQUITY)
    for i, day in enumerate(days):
        pe += p[i]
        ce += ctrl[i]
        fx.pnl_row(c, day, pe, None)
        fx.pnl_row(c, day, ce, f"{eid}:control")
        for name, pnl in treats.items():
            te[name] += pnl[i]
            fx.pnl_row(c, day, te[name], f"{eid}:{name}")
        held: dict[str, float] = {}
        if book_marks is not None and book_marks[i] is not None:
            held[BOOK] = book_marks[i]  # type: ignore[assignment]
        if p_open is not None:
            held |= {f"p-{j}": 100.0 for j in range(p_open[i] - len(held))}
        if book_marks is not None or p_open is not None:
            _snap(c, day, None, held)
        if c_open is not None:
            _snap(c, day, f"{eid}:control", {f"c-{j}": 100.0 for j in range(c_open[i])})
    c.commit()
    return days


def _report(c, eid: str, days: list[dt.date], *, aa_sigma: float | None = None) -> ExperimentReport:  # type: ignore[no-untyped-def]
    from arc.experiments.store import ExperimentStore

    st_ = ExperimentStore(c, now=lambda: fx.T0).require(eid)
    now = fx.eod(days[-1]) + dt.timedelta(minutes=15)
    with mock.patch("arc.experiments.evaluate._evaluator_sha", return_value="e" * 40):
        return build_report(c, st_, ExperimentsConfig(), now=now, aa_sigma=aa_sigma)


def _k3_scenario(c, *, p_noise_seed: int = 0, p_scale: float = 1.0) -> list[dt.date]:  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(7)
    n = 30
    ctrl = list(rng.normal(20, 300, n))
    noise = rng.normal(0, 100, (3, n))
    treats = {
        "t1": [x + 400 + e for x, e in zip(ctrl, noise[0], strict=True)],  # clear win
        "t2": [x - 400 + e for x, e in zip(ctrl, noise[1], strict=True)],  # clear loss
        "t3": [x + e for x, e in zip(ctrl, noise[2], strict=True)],  # null
    }
    p = list(np.random.default_rng(p_noise_seed).normal(0, 2000, n) * p_scale)
    marks: list[float | None] = [500.0 + 15 * i for i in range(n)]
    return _book(c, "XP-30", p=p, ctrl=ctrl, treats=treats, book_marks=marks)


# -- v1 / v2 unchanged -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_v2_report_is_unchanged_by_e253(name: str) -> None:
    want = (GOLDEN_DIR / f"{name}.json").read_text().rstrip("\n")
    got = render(name)
    assert_json_close(json.loads(got), json.loads(want))
    assert '"report_version":2' in got
    for key in ('"production"', '"reference"'):
        assert key not in got


def test_v2_legacy_scenario_still_subtracts_the_legacy_book() -> None:
    rep = ExperimentReport.model_validate_json(render("ab_k3_with_legacy"))
    assert rep.legacy_book == ["legacy-structure-1"]
    assert [r.legacy_pnl for r in rep.series] == pytest.approx([0.0] + [10.0] * 23)
    assert rep.arms[0].arm_id is None  # control = production up to v2


# -- Holm at K = 3 ----------------------------------------------------------------


@settings(deadline=None, max_examples=1)
@given(st.just(None))
def test_familywise_error_at_k3_under_global_null(_: None) -> None:
    """K=3 null arms sharing the control arm, daily looks, running-min p + Holm (D108).

    FWER over the whole run <= alpha (+ simulation error); unadjusted testing of the
    three arms is visibly worse.
    """
    rng = np.random.default_rng(253)
    k, sims, n_max, alpha, sigma = 3, 2000, 60, 0.05, 0.004
    tau = stats.mixing_tau(sigma, mde=None, min_sessions=20)
    holm_err = raw_err = 0
    for _ in range(sims):
        ctrl = rng.normal(0, sigma / np.sqrt(2), n_max)
        arms = rng.normal(0, sigma / np.sqrt(2), (k, n_max))
        d = arms - ctrl  # correlated through the shared control arm (rho = 0.5)
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


# -- v3 verdicts: Ti vs the control arm ---------------------------------------------


def test_v3_k3_verdicts_are_ti_vs_the_control_arm() -> None:
    c = _conn()
    fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
    days = _k3_scenario(c)
    rep = _report(c, "XP-30", days)
    assert rep.report_version == 3 and rep.reference == "control"
    assert [a.arm for a in rep.arms] == ["control", "t1", "t2", "t3"]
    assert rep.arms[0].arm_id == "XP-30:control"
    by = {p.b: p for p in rep.pairwise or [] if p.kind == "control_vs_treatment"}
    assert by["t1"].verdict == "win" and by["t2"].verdict == "loss"
    assert by["t3"].verdict == "continue"
    assert all(p.holm_p is not None for p in by.values())
    tt = [p for p in rep.pairwise or [] if p.kind == "treatment_vs_treatment"]
    assert len(tt) == 3 and all(p.descriptive and p.holm_p is None for p in tt)
    # control's series is its virtual equity, nothing subtracted (no legacy book)
    assert all(r.legacy_pnl == 0.0 for r in by["t3"].series)
    assert rep.production is not None and rep.production.descriptive
    again = ExperimentReport.model_validate_json(rep.canonical_json())
    assert again.canonical_json() == rep.canonical_json()


def test_production_never_enters_the_verdict() -> None:
    """Perturbing the production series moves the panel, never a verdict or a Holm p."""
    reps = []
    for seed, scale in ((0, 1.0), (99, 5.0), (3, -3.0)):
        c = _conn()
        fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
        reps.append(_report(c, "XP-30", _k3_scenario(c, p_noise_seed=seed, p_scale=scale)))
    base = reps[0]

    def decision(r: ExperimentReport) -> Any:
        d = r.model_dump(mode="json", exclude={"production", "evaluated_at"})
        return d

    for other in reps[1:]:
        assert decision(other) == decision(base)
        assert other.production != base.production  # the panel did see the change
    assert {r.verdict for r in reps} == {base.verdict}


def test_v3_one_treatment_is_a_v3_report_against_the_control_arm() -> None:
    c = _conn()
    fx.start(c, _v3(k=1))
    days = _book(c, "XP-30", p=[50.0] * 6, ctrl=[10.0] * 6, treats={"t1": [30.0] * 6})
    rep = _report(c, "XP-30", days)
    assert rep.report_version == 3 and rep.headline_arm == "t1"
    pair = next(p for p in rep.pairwise or [] if p.kind == "control_vs_treatment")
    assert pair.arm_id == "XP-30:t1"
    assert pair.mean == pytest.approx(20.0 / fx.T0_EQUITY)


def test_v3_control_without_eod_rows_leaves_sessions_missing() -> None:
    """No production fallback: a session without the control arm's row is not paired."""
    c = _conn()
    fx.start(c, _v3(k=2))
    days = fx.sessions(4)
    fx.pnl_row(c, previous_session(days[0]), fx.T0_EQUITY, None)
    for i, day in enumerate(days):
        fx.pnl_row(c, day, fx.T0_EQUITY + 10 * (i + 1), None)  # production only
        for t in ("t1", "t2"):
            fx.pnl_row(c, day, fx.T0_EQUITY + 5 * (i + 1), f"XP-30:{t}")
    c.commit()
    rep = _report(c, "XP-30", days)
    assert rep.sessions == 0 and rep.missing_sessions == days


# -- v3 A/A ------------------------------------------------------------------------


def test_v3_aa_invalid_when_a_ti_vs_c_ci_excludes_zero() -> None:
    c = _conn()
    n = 12
    fx.start(c, _v3("XP-31", kind="aa"))
    days = _book(
        c, "XP-31", p=[0.0] * n, ctrl=[0.0] * n,
        treats={"t1": [5.0 * (i % 2) for i in range(n)],
                "t2": [300.0 + (i % 3) * 10 for i in range(n)],
                "t3": [-5.0 * (i % 2) for i in range(n)]},
    )  # fmt: skip
    rep = _report(c, "XP-31", days)
    assert rep.verdict == "invalid" and "t2 vs control" in rep.verdict_reason
    tt = [p for p in rep.pairwise or [] if p.kind == "treatment_vs_treatment"]
    assert all(p.holm_p is None for p in tt)  # v3: the family is the K control pairs


def test_v3_aa_ignores_production_and_ti_vs_tj() -> None:
    """Production far from every arm (its start book, lag) never makes a v3 A/A invalid."""
    c = _conn()
    rng = np.random.default_rng(1)
    n = 10
    ctrl = list(rng.normal(0, 300, n))
    treats = {
        f"t{i}": [x + e for x, e in zip(ctrl, rng.normal(0, 150, n), strict=True)]
        for i in (1, 2, 3)
    }
    fx.start(c, _v3("XP-31", kind="aa"))
    days = _book(c, "XP-31", p=[900.0] * n, ctrl=ctrl, treats=treats)
    rep = _report(c, "XP-31", days)
    assert rep.verdict == "futility" and "every pair null" in rep.verdict_reason
    assert rep.production is not None
    total = {x.arm: x for x in rep.production.compare if x.basis == "total"}
    assert total["control"].ci is not None and total["control"].ci.hi < 0  # C - P < 0


# -- production panel ----------------------------------------------------------------


def test_decomposition_identity_is_exact_and_start_book_is_tracked() -> None:
    c = _conn()
    rng = np.random.default_rng(11)
    n = 8
    marks: list[float | None] = [520.0, 480.0, 610.0, 590.0, None, None, None, None]
    fx.start(c, _v3(k=2), legacy=[BOOK])
    p = list(rng.normal(30, 250, n))
    ctrl = list(rng.normal(10, 200, n))
    days = _book(
        c, "XP-30", p=p, ctrl=ctrl,
        treats={"t1": list(rng.normal(0, 200, n)), "t2": list(rng.normal(0, 200, n))},
        book_marks=marks,
    )  # fmt: skip
    # the start book closes on session 5 for 0.75 credit-back (filled close order)
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, status, opened_at, closed_at)
           VALUES (?, 'SPY', ?, 'c', '{}', 1, '-1.00', 'closed', ?, ?)""",
        (BOOK, "b" * 64, to_db(fx.T0 - dt.timedelta(days=3)), to_db(fx.eod(days[4]))),
    )
    c.execute(
        """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
               band_hi, max_steps, attempts, contracts, filled_qty, fill_price,
               started_at, finished_at, structure_id, arm_id)
           VALUES ('h', 'close', 'filled', 'arc2', '1', '1', 4, 1, 1, 1, '-6.40', ?, ?, ?, NULL)""",
        (to_db(fx.eod(days[4]) - dt.timedelta(hours=2)), to_db(fx.eod(days[4])), BOOK),
    )
    c.commit()
    rep = _report(c, "XP-30", days)
    pp = rep.production
    assert pp is not None and len(pp.sessions) == n
    for s in pp.sessions:
        assert s.d_p_c == s.start_book + s.residual  # exact, by construction
        assert s.d_p_c == pytest.approx((s.p_pnl - s.c_pnl) / fx.T0_EQUITY, abs=1e-15)
        assert s.p_ex_start_pnl == pytest.approx(s.p_pnl - s.p_start_book_pnl)
        assert set(s.ti_pnl) == {"t1", "t2"}
    sb = [s.p_start_book_pnl for s in pp.sessions]
    assert sb[:4] == pytest.approx([20.0, -40.0, 130.0, -20.0])
    assert sb[4] == pytest.approx(-590.0 + 640.0)  # mark gone, close cash in
    assert sb[5:] == [0.0, 0.0, 0.0]
    assert pp.start_book.ids == [BOOK]
    assert [d.open for d in pp.start_book.days] == [1, 1, 1, 1, 0, 0, 0, 0]
    assert [d.closed for d in pp.start_book.days] == [0, 0, 0, 0, 1, 1, 1, 1]
    assert pp.start_book.close_dates == {BOOK: days[4]}
    assert pp.start_book.cum_pnl == pytest.approx(sum(sb))
    assert pp.cum_d_p_c == pytest.approx(pp.cum_start_book + pp.cum_residual)
    # the verdict pairs never saw the start book
    by = {x.b: x for x in rep.pairwise or [] if x.kind == "control_vs_treatment"}
    assert all(r.legacy_pnl == 0.0 for r in by["t1"].series)


def test_start_book_close_dates_from_open_structures() -> None:
    from arc.experiments.production import _close_dates

    c = _conn()
    c.execute("PRAGMA foreign_keys = OFF")
    closed_at = fx.eod(fx.sessions(3)[2])
    for sid, status, at in (("a", "closed", to_db(closed_at)), ("b", "open", None)):
        c.execute(
            """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
                   structure_json, contracts, entry_net, status, opened_at, closed_at)
               VALUES (?, 'SPY', ?, 'c', '{}', 1, '-1.00', ?, ?, ?)""",
            (sid, sid * 64, status, to_db(fx.T0), at),
        )
    assert _close_dates(c, ["a", "b"]) == {"a": closed_at.date(), "b": None}


def test_ramp_flags_trailing_median_and_sticky() -> None:
    assert ramp_flags([], []) == []
    # control flat for 4 sessions, then catches up: median over 5 reaches P on day 7
    p = [6, 6, 6, 6, 6, 6, 6, 6, 6]
    cc = [0, 2, 4, 5, 6, 7, 7, 7, 1]
    assert ramp_flags(p, cc) == [False] * 6 + [True] * 3  # sticky after a dip
    # ties count as ramped; unknown counts are left out of the window
    assert ramp_flags([3, None, 3], [3, 9, 2]) == [True, True, True]
    assert ramp_flags([None, 4], [None, 4]) == [False, True]
    assert ramp_flags([0], [0]) == [True]
    assert RAMP_WINDOW == 5


def test_post_ramp_compare_uses_only_ramped_sessions() -> None:
    c = _conn()
    n = 9
    fx.start(c, _v3(k=1))
    days = _book(
        c, "XP-30", p=[10.0] * n, ctrl=[0.0] * n, treats={"t1": [50.0] * n},
        p_open=[6] * n, c_open=[0, 2, 4, 5, 6, 7, 7, 7, 7],
    )  # fmt: skip
    pp = _report(c, "XP-30", days).production
    assert pp is not None
    assert [s.ramped for s in pp.sessions] == [False] * 6 + [True] * 3
    assert pp.ramped_from == days[6]
    assert [s.p_open for s in pp.sessions] == [6] * n
    cmp = {(x.arm, x.basis): x for x in pp.compare}
    assert cmp[("t1", "total")].n == n and cmp[("t1", "post_ramp")].n == 3
    assert cmp[("t1", "total")].mean == pytest.approx(40.0 / fx.T0_EQUITY)
    assert cmp[("t1", "ex_start_book")].mean == pytest.approx(40.0 / fx.T0_EQUITY)  # no book
    assert cmp[("control", "total")].mean == pytest.approx(-10.0 / fx.T0_EQUITY)
    assert {x.arm for x in pp.compare} == {"control", "t1"}


# -- surfaces -----------------------------------------------------------------------


def test_cli_prints_ti_vs_c_before_the_production_panel() -> None:
    from arc.experiments.calibration import calibration_markdown
    from arc.experiments.cli import report_lines

    c = _conn()
    fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
    rep = _report(c, "XP-30", _k3_scenario(c))
    text = "\n".join(report_lines(rep))
    assert "reference control arm (D108); production start book 1" in text
    pairs = text.index("pairs (b - a")
    panel = text.index("production panel (descriptive only")
    assert pairs < panel
    assert "t1        total" in text and "post_ramp" in text
    md = "\n".join(calibration_markdown(rep))
    assert "Production (descriptive" in md and "reference: the control arm" in md


def test_v2_cli_text_has_no_production_panel() -> None:
    from arc.experiments.cli import report_lines

    rep = ExperimentReport.model_validate_json(render("ab_k3_with_legacy"))
    text = "\n".join(report_lines(rep))
    assert "production panel" not in text and "legacy book 1" in text


def test_verdict_file_names_the_control_reference_for_v3_only() -> None:
    from arc.experiments.models import StopReason
    from arc.experiments.store import ExperimentStore
    from arc.experiments.verdicts import verdict_doc

    c = _conn()
    s3 = fx.start(c, _v3())
    s3.stop("XP-30", StopReason.FUTILITY, actor="arc.experiments")
    assert verdict_doc(ExperimentStore(c).require("XP-30"), None)["reference"] == "control"
    c2 = _conn()
    s2 = fx.start(c2, fx.spec("XP-2", arms={"treatments": {"t1": OVERLAY, "t2": OVERLAY}}))
    s2.stop("XP-2", StopReason.FUTILITY, actor="arc.experiments")
    assert "reference" not in verdict_doc(ExperimentStore(c2).require("XP-2"), None)


def test_show_names_the_control_arm_for_v3() -> None:
    from arc.experiments.cli import _detail
    from arc.experiments.store import ExperimentStore

    c = _conn()
    fx.start(c, _v3())
    text = "\n".join(_detail(ExperimentStore(c).require("XP-30")))
    assert "control: arm XP-30:control (spec v3, D108)" in text
