"""K=1 report scenarios for the E15.5 regression (D69: K=1 reports stay byte-identical).

Each scenario builds one single-treatment experiment on a fresh in-memory store and
returns what :func:`arc.experiments.evaluate.build_report` needs. The canonical JSON
of every scenario's report was captured from the pre-E15.5 evaluator (main at
0f72bb3) into ``tests/fixtures/experiments/k1_reports/<name>.json``;
``tests/test_experiment_holm.py`` rebuilds each one with the current code and
compares bytes. Regenerate only on purpose (a deliberate v1 change):
``.venv/bin/python -m tests.experiment_k1_scenarios``.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from arc.context.ttl import to_db
from arc.experiments.models import ExperimentState, arm_id
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import previous_session
from tests import experiment_fixtures as fx

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "experiments" / "k1_reports"
EVALUATOR_SHA = "e155e155e155e155e155e155e155e155e155e155"


@dataclass
class Scenario:
    conn: sqlite3.Connection
    state: ExperimentState
    now: dt.datetime
    aa_sigma: float | None = None


def _conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _after(days: list[dt.date]) -> dt.datetime:
    return fx.eod(days[-1]) + dt.timedelta(minutes=15)


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


def _manifest(conn: sqlite3.Connection, chain: str, aid: str | None, payload: dict) -> None:  # type: ignore[type-arg]
    rid = uuid.uuid4().hex
    at = to_db(fx.T0 + dt.timedelta(hours=2))
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, chain_run_id, reason, scheduled_for,
               status, started_at) VALUES (?, ?, ?, 'schedule', ?, 'ok', ?)""",
        (rid, f"research:{chain}", chain, at, at),
    )
    conn.execute(
        """INSERT INTO run_manifests (id, run_id, attempt, job, chain_run_id, status,
               schema_version, payload, created_at, arm_id)
           VALUES (?, ?, 1, 'research', ?, 'ok', 1, ?, ?, ?)""",
        (uuid.uuid4().hex, rid, chain, json.dumps(payload), at, aid),
    )


def ab_win() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(5)
    ctrl = list(rng.normal(20, 300, 25))
    treat = [x + 400 + e for x, e in zip(ctrl, rng.normal(0, 100, 25), strict=True)]
    store = fx.start(c, fx.spec("XP-2"))
    days = fx.equity_curves(c, "XP-2", ctrl, treat)
    return Scenario(c, store.require("XP-2"), _after(days))


def ab_inferior_sortino() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(9)
    ctrl = list(np.abs(rng.normal(30, 5, 30)))
    treat = [x + (900 if i % 2 else -650) for i, x in enumerate(ctrl)]
    store = fx.start(c, fx.spec("XP-2", max_sessions=40))
    days = fx.equity_curves(c, "XP-2", ctrl, treat)
    return Scenario(c, store.require("XP-2"), _after(days))


def ab_futility_with_aa_sigma() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(2)
    store = fx.start(c, fx.spec("XP-2", min_sessions=10, max_sessions=20))
    days = fx.equity_curves(c, "XP-2", list(rng.normal(0, 300, 20)), list(rng.normal(0, 300, 20)))
    return Scenario(c, store.require("XP-2"), _after(days), aa_sigma=0.003)


def aa_futility() -> Scenario:
    c = _conn()
    rng = np.random.default_rng(4)
    ctrl = list(rng.normal(0, 300, 10))
    treat = [x + e for x, e in zip(ctrl, rng.normal(0, 150, 10), strict=True)]
    store = fx.start(c, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(c, "XP-1", ctrl, treat)
    return Scenario(c, store.require("XP-1"), _after(days))


def aa_invalid() -> Scenario:
    c = _conn()
    store = fx.start(c, fx.spec("XP-1", kind="aa"))
    days = fx.equity_curves(c, "XP-1", [0.0] * 10, [300.0 + (i % 3) * 10 for i in range(10)])
    return Scenario(c, store.require("XP-1"), _after(days))


def legacy_and_missing() -> Scenario:
    """Legacy book excluded from control, one missing arm session, provenance rows."""
    c = _conn()
    sid = "os-legacy"
    store = fx.start(c, fx.spec("XP-2"), legacy=[sid])
    days = fx.sessions(4)
    t_arm = arm_id("XP-2", "treatment")
    fx.legacy_snapshot(c, previous_session(days[0]), sid, 500.0)
    for day, v in zip(days, (700.0, 600.0, 650.0, 640.0), strict=True):
        fx.legacy_snapshot(c, day, sid, v)
    fx.pnl_row(c, previous_session(days[0]), 100_000, None)
    ctrl = [100_300, 100_150, 100_260, 100_200]
    treat = [100_100, None, 100_160, 100_400]
    for day, cv, tv in zip(days, ctrl, treat, strict=True):
        fx.pnl_row(c, day, cv, None)
        if tv is not None:
            fx.pnl_row(c, day, tv, t_arm)
    fx.executions(c, None, 2, day=days[0])
    fx.executions(c, t_arm, 3, attempts=2, day=days[1])
    _manifest(c, "chain-c1", None, {"git_sha": "c" * 40})
    _manifest(
        c, "chain-t1", t_arm, {"git_sha": fx.TREATMENT_SHA, "paired_chain_run_id": "chain-c1"}
    )
    _outcome(c, None, slippage=4.0, regime="bull", pnl="120", day=days[1])
    _outcome(c, t_arm, slippage=9.0, regime="bear", pnl="-30", day=days[2])
    c.commit()
    return Scenario(c, store.require("XP-2"), _after(days))


def no_sessions_yet() -> Scenario:
    c = _conn()
    store = fx.start(c, fx.spec("XP-2"))
    return Scenario(c, store.require("XP-2"), fx.T0 + dt.timedelta(hours=1))


SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "ab_win": ab_win,
    "ab_inferior_sortino": ab_inferior_sortino,
    "ab_futility_with_aa_sigma": ab_futility_with_aa_sigma,
    "aa_futility": aa_futility,
    "aa_invalid": aa_invalid,
    "legacy_and_missing": legacy_and_missing,
    "no_sessions_yet": no_sessions_yet,
}


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
