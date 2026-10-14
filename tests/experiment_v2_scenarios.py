"""K=3 spec v2 report scenarios for the E25.3 regression (D108: v1/v2 reports unchanged).

Each scenario builds a three-treatment spec v2 experiment (control = production,
legacy book excluded) on a fresh in-memory store. The canonical JSON of every
scenario's report was captured from the pre-E25.3 evaluator (main at df587d2) into
``tests/fixtures/experiments/v2_reports/<name>.json``; ``tests/test_experiment_production.py``
rebuilds each one with the current code and compares them. Regenerate only on purpose
(a deliberate v2 change): ``.venv/bin/python -m tests.experiment_v2_scenarios``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np

from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import previous_session
from tests import experiment_fixtures as fx
from tests.experiment_k1_scenarios import EVALUATOR_SHA, Scenario, _outcome

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "experiments" / "v2_reports"
OVERLAY = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
LEGACY = "legacy-structure-1"


def _conn():  # type: ignore[no-untyped-def]
    c = connect(":memory:")
    migrate(c)
    return c


def _curves(c, eid: str, ctrl: list[float], treats: dict[str, list[float]]) -> list[dt.date]:  # type: ignore[no-untyped-def]
    days = fx.sessions(len(ctrl))
    fx.pnl_row(c, previous_session(days[0]), fx.T0_EQUITY, None)
    ce = fx.T0_EQUITY
    eq = dict.fromkeys(treats, fx.T0_EQUITY)
    for i, day in enumerate(days):
        ce += ctrl[i]
        fx.pnl_row(c, day, ce, None)
        for name, pnl in treats.items():
            eq[name] += pnl[i]
            fx.pnl_row(c, day, eq[name], f"{eid}:{name}")
    c.commit()
    return days


def ab_k3_with_legacy() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(25)
    n = 24
    sp = fx.spec(
        "XP-2", arms={"treatments": {f"t{i}": OVERLAY for i in (1, 2, 3)}}, max_sessions=40
    )
    st = fx.start(c, sp, legacy=[LEGACY])
    ctrl = list(rng.normal(20, 300, n))
    noise = rng.normal(0, 100, (3, n))
    treats = {
        "t1": [x + 350 + e for x, e in zip(ctrl, noise[0], strict=True)],
        "t2": [x - 350 + e for x, e in zip(ctrl, noise[1], strict=True)],
        "t3": [x + e for x, e in zip(ctrl, noise[2], strict=True)],
    }
    days = fx.sessions(n)
    fx.legacy_snapshot(c, previous_session(days[0]), LEGACY, 500.0)
    for i, day in enumerate(days):
        fx.legacy_snapshot(c, day, LEGACY, 500.0 + 10 * i)
    days = _curves(c, "XP-2", ctrl, treats)
    for aid in (None, "XP-2:t1", "XP-2:t2", "XP-2:t3"):
        fx.executions(c, aid, 2, day=days[1])
        _outcome(c, aid, slippage=3.0, regime="bull", pnl="12.5", day=days[2])
    c.commit()
    return Scenario(c, st.require("XP-2"), fx.eod(days[-1]) + dt.timedelta(minutes=15))


def aa_k3_null() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(3)
    n = 12
    sp = fx.spec("XP-1", kind="aa", arms={"treatments": {f"t{i}": {} for i in (1, 2, 3)}})
    st = fx.start(c, sp)
    ctrl = list(rng.normal(0, 300, n))
    treats = {
        f"t{i}": [x + e for x, e in zip(ctrl, rng.normal(0, 150, n), strict=True)]
        for i in (1, 2, 3)
    }
    days = _curves(c, "XP-1", ctrl, treats)
    return Scenario(c, st.require("XP-1"), fx.eod(days[-1]) + dt.timedelta(minutes=15))


SCENARIOS = {"ab_k3_with_legacy": ab_k3_with_legacy, "aa_k3_null": aa_k3_null}


def render(name: str) -> str:
    """The scenario's report as canonical JSON (evaluator sha pinned)."""
    from unittest import mock

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.evaluate import build_report

    sc = SCENARIOS[name]()
    with mock.patch("arc.experiments.evaluate._evaluator_sha", return_value=EVALUATOR_SHA):
        rep = build_report(sc.conn, sc.state, ExperimentsConfig(), now=sc.now, aa_sigma=sc.aa_sigma)
    return rep.canonical_json()


def main() -> None:  # pragma: no cover - regenerates the goldens on purpose
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCENARIOS:
        (GOLDEN_DIR / f"{name}.json").write_text(render(name) + "\n")


if __name__ == "__main__":  # pragma: no cover
    main()
