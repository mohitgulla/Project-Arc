"""E10.2b: an arm reviews its OWN open book for Research exits.

XP-10 (A/A, Oct 7-9): the arm reused control's Research verbatim, so its
``exit_watchlist`` named control's structures and no exit case was ever built for the
arm's own positions. An arm holding any open structure now forks its paired chain at
``research`` (its own Research call on its own ``portfolio_view``); with an empty book
it keeps reusing control's Research (fork at ``exits.mandatory``, the A/A invariant).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.control.effective import effective_routines
from arc.experiments.config import ArmRunner, RunnerConfig
from arc.experiments.runner import (
    _open_book,
    arm_stores,
    book_fork,
    fork_step,
    pair_chain,
    start_arms,
)
from arc.ingest.llm import FixtureScalpLLM
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.steps import pipeline_handlers
from arc.routines.config import AUTO_CHAINS
from arc.store.migrate import migrate
from tests.test_e59_research_portfolio import FIXTURES_DIR, LONG_CALL, _open_structure

if TYPE_CHECKING:
    from pathlib import Path

T0 = FIXTURE_NOW - dt.timedelta(hours=1)
SPEC = "config/experiments/live/xp1_aa_baseline.yaml"
FULL = ["research", *AUTO_CHAINS["research"]]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def _runner() -> RunnerConfig:
    return RunnerConfig(
        arms={
            "treatment": ArmRunner(
                spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
            )
        }
    )


def _settings() -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


# --- pure rule ----------------------------------------------------------------------


def test_book_fork_moves_to_research_only_with_an_own_book() -> None:
    planned = fork_step(FULL, {})
    assert planned == "exits.mandatory"
    # A/A invariant: an empty book keeps reusing control's Research
    assert book_fork(FULL, planned, []) == "exits.mandatory"
    # any open structure of the arm's own -> its own Research (own exit watchlist)
    assert book_fork(FULL, planned, ["os-arm1"]) == "research"
    # already at research (or a chain without research): unchanged
    assert book_fork(FULL, "research", ["os-arm1"]) == "research"
    assert book_fork(["exits.mandatory", "broker.execute"], "exits.mandatory", ["x"]) == (
        "exits.mandatory"
    )


def test_open_book_lists_only_open_structures() -> None:
    from arc.pipeline.runner import open_db

    conn = open_db(":memory:", copy=False)
    env = PipelineEnv.fixtures()
    keep = _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10")
    gone = _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10")
    conn.execute("UPDATE open_structures SET status = 'closed' WHERE id = ?", (gone,))
    assert _open_book(conn) == [keep]


# --- paired chain on fixture stores -------------------------------------------------


@pytest.fixture
def started(tmp_path: Path) -> tuple[Path, str]:
    """A running XP-1 with a treatment arm, and one control loop chain (fixtures)."""
    control = tmp_path / "control.db"
    assert _arc("experiment", "create", "--spec", SPEC, "--db", str(control)) == 0
    assert _arc("experiment", "register", "XP-1", "--db", str(control)) == 0
    conn = _db(control)
    start_arms(
        conn,
        "XP-1",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner(),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
    )
    conn.close()
    assert (
        _arc("propose", "--fixtures", "--fixture-set", "bullish", "--profile", "cash_debit",
             "--db", str(control), "--no-slack", "--lock-dir", str(tmp_path / "locks"))
        == 0
    )  # fmt: skip
    conn = _db(control)
    chain = conn.execute(
        "SELECT chain_run_id FROM routine_runs WHERE job = 'research' AND chain_run_id IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return control, chain


def _research_watch(sid: str) -> str:
    d = json.loads((FIXTURES_DIR / "research.json").read_text())
    d.update(
        portfolio_view={"verdict": "concentrated", "notes": "all SPY"},
        exit_watchlist=[
            {
                "structure_id": sid,
                "ticker": "SPY",
                "action": "review",
                "thesis_status": "broken",
                "evidence": ["capex guide cut"],
                "reason": "thesis broken",
            }
        ],
    )
    return json.dumps(d)


def _kind(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = ? ORDER BY rowid", (kind,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_arm_with_own_book_runs_research_and_builds_its_own_exit_case(
    started: tuple[Path, str],
) -> None:
    control_path, chain = started
    control = _db(control_path)
    control_book = {r[0] for r in control.execute("SELECT id FROM open_structures")}
    arm = _db(arm_stores(control, "XP-1")["treatment"])
    env = PipelineEnv.fixtures()
    sid = _open_structure(arm, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2)
    assert sid not in control_book  # control holds a different book
    env.llms["research"] = FixtureScalpLLM([_research_watch(sid)])
    quant_exit = json.dumps(
        {"cases": [{"structure_id": sid, "recommendation": "close", "rationale": "broken"}]}
    )
    quant_open = (FIXTURES_DIR / "quant.json").read_text()
    env.llms["quant"] = FixtureScalpLLM([quant_exit, quant_open])
    settings = _settings()

    res = pair_chain(
        control,
        arm,
        chain,
        routines=effective_routines(arm),
        runner=_runner(),
        now=FIXTURE_NOW,
        handlers=pipeline_handlers(env),
        settings_factory=lambda: settings,
        check_lag=False,
    )
    assert res.status == "ok", res.reason
    assert res.fork_step == "research"
    pair = arm.execute("SELECT fork_step, reason FROM arm_pairs").fetchone()
    assert pair["fork_step"] == "research"
    assert "own book: 1 open; plan exits.mandatory" in pair["reason"]
    # the arm made its own Research call: not reused from control
    (research,) = arm.execute("SELECT summary FROM routine_runs WHERE job = 'research'").fetchall()
    assert not research["summary"].startswith("paired: reused")
    assert sid in env.llms["research"].prompts[0]  # type: ignore[attr-defined]
    # its own watchlist names its own structure, never control's
    (wl,) = _kind(arm, "exit_watchlist")
    assert [i["structure_id"] for i in wl["items"]] == [sid]
    assert not {i["structure_id"] for i in wl["items"]} & control_book
    # ... and quant.exit builds the exit case for it
    q = arm.execute("SELECT status, summary FROM routine_runs WHERE job = 'quant.exit'").fetchone()
    assert q["status"] == "ok", q["summary"]
    (case,) = _kind(arm, "exit_case")
    assert case["structure_id"] == sid and case["recommendation"] == "close"
    assert case["triggers"][0]["kind"] == "research_review"
    built = arm.execute(
        "SELECT subject FROM decisions WHERE reason_code = 'exit:case_built'"
    ).fetchall()
    assert [r[0] for r in built] == [sid]


def test_arm_with_empty_book_still_reuses_control_research(started: tuple[Path, str]) -> None:
    """The A/A invariant: identical (empty) books -> fork at exits.mandatory."""
    control_path, chain = started
    control = _db(control_path)
    arm = _db(arm_stores(control, "XP-1")["treatment"])
    env = PipelineEnv.fixtures()
    settings = _settings()
    res = pair_chain(
        control,
        arm,
        chain,
        routines=effective_routines(arm),
        runner=_runner(),
        now=FIXTURE_NOW,
        handlers=pipeline_handlers(env),
        settings_factory=lambda: settings,
        check_lag=False,
    )
    assert res.status == "ok", res.reason
    assert res.fork_step == "exits.mandatory"
    pair = arm.execute("SELECT fork_step, reason FROM arm_pairs").fetchone()
    assert pair["fork_step"] == "exits.mandatory" and "own book" not in pair["reason"]
    (research,) = arm.execute("SELECT summary FROM routine_runs WHERE job = 'research'").fetchall()
    assert research["summary"].startswith("paired: reused")
