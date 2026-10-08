"""E13.2 (D56): Investor + Auditor removed — Broker job, Quant marks/exits, Ops scorecard."""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from arc.control.registry import is_alias, lookup
from arc.experiments.config import RunnerConfig
from arc.experiments.runner import ACCOUNT_STEPS, STEP_TARGETS, arm_routines
from arc.journal import legacy
from arc.journal.reasons import JournalPersona
from arc.llm_routing import Persona, load_routing
from arc.routines.config import (
    TIMELINE_PERSONAS,
    RoutinesConfig,
    load_routines,
)
from arc.routines.handlers import BUILTIN_HANDLERS
from arc.routines.heartbeat import _PERSONA_LABELS
from arc.routines.runs import RoutineRunRepo
from arc.slack.personas import Persona as SlackPersona
from arc.store.db import connect
from arc.store.migrate import MIGRATIONS_DIR, migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parent.parent
_MIG = "025_investor_to_broker.sql"

CUT = _dt.datetime(2026, 10, 6, 9, 30, tzinfo=ET)
BEFORE = CUT - _dt.timedelta(days=4)
AFTER = CUT + _dt.timedelta(hours=1)
CUTS = {legacy.BROKER_CUTOVER_KEY: CUT}


# ---------------------------------------------------------------------------
# Migration 025 on a pre-rename (024) store
# ---------------------------------------------------------------------------


def _store_at_024(path: Path) -> sqlite3.Connection:
    c = connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(sql.stem.split("_", 1)[0])
        if v >= 25:
            break
        c.executescript(sql.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    c.executemany(
        "INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("cursor:auditor", "2026-10-05T20:30:02.475585Z", "2026-10-05T20:30:02.475585Z"),
            ("cursor:scorecard", "2026-10-02T20:47:00.801446Z", "2026-10-02T20:47:00.801446Z"),
            ("missed:auditor:2026-10-01T16:30", "x", "2026-10-01T20:30:00.000000Z"),
        ],
    )
    c.commit()
    return c


def test_migration_025_moves_the_reconcile_cursor(tmp_path: Path) -> None:
    c = _store_at_024(tmp_path / "arc.db")
    assert migrate(c)[0] == 25
    state = dict(c.execute("SELECT key, value FROM routine_state").fetchall())
    assert state["cursor:broker.reconcile"] == "2026-10-05T20:30:02.475585Z"
    assert "cursor:auditor" not in state
    assert state["cursor:scorecard"] == "2026-10-02T20:47:00.801446Z"  # job name kept
    assert "missed:auditor:2026-10-01T16:30" in state  # alert history stays
    cut = legacy.cutover(c, legacy.BROKER_CUTOVER_KEY)
    assert cut is not None and cut.tzinfo is not None


def test_broker_cutover_is_written_once() -> None:
    c = connect(":memory:")
    migrate(c)
    first = legacy.cutover(c, legacy.BROKER_CUTOVER_KEY)
    sql = (MIGRATIONS_DIR / _MIG).read_text()
    c.executescript(sql[sql.index("INSERT OR IGNORE INTO routine_state") :])
    assert legacy.cutover(c, legacy.BROKER_CUTOVER_KEY) == first
    assert legacy.BROKER_CUTOVER_KEY in legacy.cutovers(c)


# ---------------------------------------------------------------------------
# Legacy reads: both sides of the cutover
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "stage", "at", "cuts", "key", "label"),
    [
        ("investor", "order", BEFORE, CUTS, "broker", "Broker"),
        ("investor", "approval", BEFORE, CUTS, "broker", "Broker"),
        ("investor", "exit", BEFORE, CUTS, "quant", "Quant"),
        ("auditor", "reconcile", BEFORE, CUTS, "broker", "Broker (reconcile)"),
        ("investor.exits", None, BEFORE, CUTS, "quant", "Quant (exits)"),
        ("execute", None, BEFORE, CUTS, "broker", "Broker (execute)"),
        # no cutover recorded (a pre-025 copy): history either way
        ("investor", "exit", AFTER, {}, "quant", "Quant"),
        ("auditor", "reconcile", AFTER, {}, "broker", "Broker (reconcile)"),
        # after the cutover nothing writes the old names; the value is returned as is
        ("investor", "order", AFTER, CUTS, "investor", "Investor"),
        # new names never move
        ("broker", "order", BEFORE, CUTS, "broker", "Broker"),
        ("quant", "exit", BEFORE, CUTS, "quant", "Quant"),
        ("investors", "order", BEFORE, CUTS, "investors", "Investors"),
    ],
)
def test_legacy_persona_on_both_sides_of_the_cutover(
    value: str,
    stage: str | None,
    at: _dt.datetime,
    cuts: dict[str, _dt.datetime],
    key: str,
    label: str,
) -> None:
    assert legacy.persona_key(value, at, cuts, stage=stage) == key
    assert legacy.persona_label(value, at, cuts, stage=stage) == label


@pytest.mark.parametrize(
    ("job", "new"),
    [
        ("investor", "broker"),
        ("auditor", "broker.reconcile"),
        ("execute", "broker.execute"),
        ("investor.exits", "quant.exits"),  # its own hop, never broker.exits
        ("scorecard", "scorecard"),
        ("monitor", "monitor"),
    ],
)
def test_legacy_job_names(job: str, new: str) -> None:
    assert legacy.job_name(job, BEFORE, CUTS) == new


def test_legacy_names_cover_the_new_jobs() -> None:
    assert legacy.legacy_names("broker") == [("investor", legacy.BROKER_CUTOVER_KEY)]
    assert legacy.legacy_names("broker.reconcile") == [("auditor", legacy.BROKER_CUTOVER_KEY)]
    assert legacy.legacy_names("broker.execute") == [("execute", legacy.BROKER_CUTOVER_KEY)]
    assert legacy.legacy_names("quant.exits") == [("investor.exits", legacy.BROKER_CUTOVER_KEY)]
    assert legacy.legacy_names("quant") == []


def test_run_history_reads_pre_rename_jobs(tmp_path: Path) -> None:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    old = "2026-10-01T14:00:00.000000Z"  # before the cutover (written at migrate time)
    for job in ("investor", "auditor", "investor.exits"):
        c.execute(
            "INSERT INTO routine_runs (run_id, job, scheduled_for, started_at, status, reason,"
            " step_index) VALUES (?, ?, ?, ?, 'ok', 'schedule', 0)",
            (f"r-{job}", job, old, old),
        )
    c.commit()
    repo = RoutineRunRepo(c)
    assert {r.run_id for r in repo.history(job="broker")} == {"r-investor"}
    rec = repo.history(job="broker.reconcile")
    assert {r.run_id for r in rec} == {"r-auditor"}
    assert {r.job for r in rec} == {"broker.reconcile"}
    assert {r.run_id for r in repo.history(job="quant.exits")} == {"r-investor.exits"}


def test_journal_reads_legacy_rows_with_stage(tmp_path: Path) -> None:
    from arc.journal.store import JournalStore

    c = connect(tmp_path / "arc.db")
    migrate(c)
    rows = [
        ("d-order", "investor", "order", "2026-10-02T15:00:00.000000Z"),
        ("d-exit", "investor", "exit", "2026-10-01T15:00:00.000000Z"),
        ("d-rec", "auditor", "reconcile", "2026-10-01T20:30:00.000000Z"),
    ]
    for did, persona, stage, at in rows:
        c.execute(
            "INSERT INTO decisions (id, persona, stage, subject, choice, reason_code,"
            " reason_text, at) VALUES (?, ?, ?, 'SPY', 'passed', 'order:filled', 'x', ?)",
            (did, persona, stage, at),
        )
    c.commit()
    got = {d.id: d.persona for d in JournalStore(c).decisions()}
    assert got == {
        "d-order": JournalPersona.BROKER,
        "d-exit": JournalPersona.QUANT,
        "d-rec": JournalPersona.BROKER,
    }


# ---------------------------------------------------------------------------
# Enums, labels, routing
# ---------------------------------------------------------------------------


def test_llm_persona_enum_drops_investor_and_auditor() -> None:
    assert not {"investor", "auditor", "broker", "ops"} & {p.value for p in Persona}
    routing = load_routing()
    assert set(routing.personas) == {p.value for p in Persona}


def test_journal_persona_has_broker_and_ops() -> None:
    values = {p.value for p in JournalPersona}
    assert {"broker", "ops", "quant"} <= values
    assert {"investor", "auditor"} <= values  # read-only legacy members


def test_slack_and_heartbeat_labels() -> None:
    assert SlackPersona.BROKER.value == "Broker"
    assert SlackPersona.OPS.value == "Ops"
    assert not {"Investor", "Auditor"} & {p.value for p in SlackPersona}
    assert _PERSONA_LABELS["broker"] == "🏦 [Broker]"
    assert _PERSONA_LABELS["ops"] == _PERSONA_LABELS["scorecard"] == "⚙️ [Ops]"
    assert {"broker", "ops", "quant"} <= set(TIMELINE_PERSONAS)
    assert not {"investor", "auditor"} & set(TIMELINE_PERSONAS)


# ---------------------------------------------------------------------------
# Handlers, aliases, config
# ---------------------------------------------------------------------------


def test_builtin_handlers_use_new_names() -> None:
    assert BUILTIN_HANDLERS["broker"] == "arc.broker.ladder_job:broker_step"
    assert BUILTIN_HANDLERS["broker.execute"] == "arc.broker.ladder_job:execute_step"
    assert BUILTIN_HANDLERS["broker.reconcile"] == (
        "arc.broker.reconcile_job:broker_reconcile_step"
    )
    assert BUILTIN_HANDLERS["exits.mandatory"] == "arc.positions.steps:exits_mandatory_step"
    old = {"investor", "auditor", "execute", "investor.exits", "quant.exits", "risk.reallocate"}
    assert not old & set(BUILTIN_HANDLERS)


@pytest.mark.parametrize("old", ["investor", "auditor", "execute", "investor.exits"])
def test_old_handler_names_no_longer_resolve(old: str) -> None:
    """E13.15: the one-release D56 job aliases are gone."""
    from arc.routines.config import StepSpec
    from arc.routines.handlers import not_implemented, resolve_handler

    assert resolve_handler(old, StepSpec()) is not_implemented


def test_shipped_routines_use_new_names(shipped: RoutinesConfig) -> None:
    p = shipped.personas
    assert {"broker", "broker.reconcile", "scorecard", "positions.evaluate"} <= set(p)
    assert not {"investor", "auditor"} & set(p)
    assert p["broker"].trigger == "approval" and p["broker"].options["persona"] == "broker"
    assert (
        p["broker.reconcile"].halt_exempt and p["broker.reconcile"].options["persona"] == "broker"
    )
    assert p["scorecard"].options["persona"] == "ops"
    ev = p["positions.evaluate"]
    assert (
        ev.options["label"] == "Position marks" and ev.options["persona"] == "quant" and not ev.llm
    )
    assert ev.chain == ["exits.mandatory", "broker.execute"]
    assert p["research"].chain[-1] == "broker.execute"
    assert {"exits.mandatory", "broker.execute"} <= set(shipped.steps)
    assert not {"execute", "investor.exits", "quant.exits", "risk.reallocate"} & set(shipped.steps)


@pytest.mark.parametrize("old", ["routines.investor.enabled", "routines.auditor.enabled"])
def test_old_routine_keys_no_longer_resolve(old: str) -> None:
    # E13.15: no longer renamed (a pattern key for an unknown job, which matches nothing)
    assert not is_alias(old)
    assert lookup(old).key == old


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def test_experiment_runner_step_names(shipped: RoutinesConfig) -> None:
    # E13.18: exits.mandatory closes against the arm's own book
    assert frozenset({"exits.mandatory", "quant.propose", "broker.execute"}) == ACCOUNT_STEPS
    assert "broker.execute" in STEP_TARGETS and "execute" not in STEP_TARGETS
    jobs = RunnerConfig().arm_jobs
    assert jobs == ["monitor", "positions.evaluate", "broker.reconcile", "broker"]
    arm = arm_routines(shipped, jobs)
    assert arm.personas["broker.reconcile"].enabled
    assert not arm.personas["research"].enabled


# ---------------------------------------------------------------------------
# Deterministic rules
# ---------------------------------------------------------------------------


def test_only_submission_calls_submit_mleg() -> None:
    out = subprocess.run(
        ["git", "grep", "-n", r"\.submit_mleg(", "--", "arc"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    callers = {ln.split(":", 1)[0] for ln in out if "def submit_mleg" not in ln}
    callers.discard("arc/broker/base.py")  # the docstring example
    assert callers == {"arc/execution/submission.py"}


def test_mandatory_exits_record_quant_persona() -> None:
    text = (REPO / "arc/positions/steps.py").read_text()
    body = text[text.index("def _close_on_signals(") : text.index("# Close-to-reallocate swaps")]
    assert "JournalPersona.QUANT" in body and "JournalPersona.INVESTOR" not in body
    assert "llm" not in body.lower()


# ---------------------------------------------------------------------------
# Grep gate: no Investor / Auditor persona names outside history
# ---------------------------------------------------------------------------

_OLD = re.compile(r"\b(Investor|Auditor|INVESTOR|AUDITOR)\b")
_ALLOWED = (
    "arc/journal/legacy.py",
    "arc/journal/reasons.py",  # read-only legacy enum members
    "arc/store/migrations/",
    "arc/personas/schemas.py",  # section headers name the move
    "arc/broker/ladder_job.py",  # module doc names the move
    "arc/broker/reconcile_job.py",
    "arc/routines/spawn.py",
    "arc/routines/config.py",
    "arc/context/kinds.py",
    "arc/positions/evaluate.py",  # field description is in the position_review v2 schema
    "arc/ingest/channels/ibd/",  # D60: the channel's name, "Investor's Business Daily"
)


def test_no_old_persona_names_in_code() -> None:
    files = subprocess.run(
        ["git", "ls-files", "arc", "web/src", "hermes/skills", "config"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    hits = []
    for f in files:
        if f.startswith(_ALLOWED) or not (REPO / f).is_file():
            continue
        try:
            text = (REPO / f).read_text()
        except UnicodeDecodeError:
            continue
        hits += [f"{f}:{i}" for i, ln in enumerate(text.splitlines(), 1) if _OLD.search(ln)]
    assert hits == []


@pytest.fixture(scope="module")
def shipped() -> RoutinesConfig:
    return load_routines()


@pytest.fixture
def shipped_raw() -> dict:
    import yaml

    from arc.routines.config import DEFAULT_ROUTINES_PATH

    return yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
