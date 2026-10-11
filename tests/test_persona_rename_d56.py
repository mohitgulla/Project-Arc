"""E13.1 (D56): Sweep -> Scalp, Director -> Research — migration, legacy chain, aliases."""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from arc.context.kinds import NotePayload
from arc.control.registry import REGISTRY, is_alias, lookup
from arc.ingest.sources import _feed
from arc.journal import legacy
from arc.journal.reasons import JournalPersona, ReasonCode
from arc.positions.portfolio import PortfolioThesis
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import resolve_handler
from arc.routines.runs import RoutineRunRepo
from arc.store.db import connect
from arc.store.migrate import MIGRATIONS_DIR, migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parent.parent
_MIG = "024_sweep_to_scalp.sql"

D54 = _dt.datetime(2026, 10, 5, 21, 20, tzinfo=ET)  # scout -> sweep
D56 = _dt.datetime(2026, 10, 6, 9, 0, tzinfo=ET)  # sweep -> scalp, director -> research
BOTH = {legacy.CUTOVER_KEY: D54, legacy.SCALP_CUTOVER_KEY: D56}


# ---------------------------------------------------------------------------
# Migration 024 on a pre-rename (023) store
# ---------------------------------------------------------------------------


def _store_at_023(path: Path) -> sqlite3.Connection:
    c = connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(sql.stem.split("_", 1)[0])
        if v >= 24:
            break
        c.executescript(sql.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    c.execute(
        "INSERT INTO raw_docs (id, source, url, published_at, text, content_hash, ingested_at,"
        " swept_at, sweep_run_id, sweep_status)"
        " VALUES ('d1','rss','u1','2026-10-05T12:00:00+00:00','t','h1',"
        " '2026-10-05T12:00:00.000000Z','2026-10-05T12:30:00.000000Z','run-1','scouted')"
    )
    c.execute(
        "INSERT INTO raw_docs (id, source, url, published_at, text, content_hash, ingested_at)"
        " VALUES ('d2','rss','u2','2026-10-05T12:00:00+00:00','t','h2',"
        " '2026-10-05T12:00:00.000000Z')"
    )
    for i, stage in enumerate(("digest", "sweep")):
        c.execute(
            "INSERT INTO sweep_batches (id, run_id, model, doc_ids, prompt_sha256, status,"
            " created_at, stage) VALUES (?, 'run-1', 'fixture', '[]', 'x', 'ok',"
            " '2026-10-05T12:30:00.000000Z', ?)",
            (f"b{i}", stage),
        )
    c.executemany(
        "INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("cursor:sweep", "2026-10-05T20:00:02.454498Z", "2026-10-05T20:00:02.454498Z"),
            ("cursor:sweep.overnight", "2026-10-06T02:00:02.5Z", "2026-10-06T02:00:02.5Z"),
            ("cursor:director", "2026-10-05T19:50:02.488841Z", "2026-10-05T19:50:02.488841Z"),
            ("missed:sweep:2026-10-05T13:00", "x", "2026-10-05T13:00:00.000000Z"),
        ],
    )
    c.commit()
    return c


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def test_migration_024_renames_identifiers_and_moves_cursors(tmp_path: Path) -> None:
    c = _store_at_023(tmp_path / "arc.db")
    applied = migrate(c)
    assert applied[0] == 24
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scalp_batches" in tables and "sweep_batches" not in tables
    assert {"scalp_status", "scalped_at", "scalp_run_id"} <= _cols(c, "raw_docs")
    assert not {"sweep_status", "swept_at", "sweep_run_id"} & _cols(c, "raw_docs")
    assert c.execute("SELECT COUNT(*) FROM raw_docs").fetchone()[0] == 2
    stages = sorted(r[0] for r in c.execute("SELECT stage FROM scalp_batches"))
    assert stages == ["digest", "scalp"]
    status = c.execute("SELECT scalp_status FROM raw_docs WHERE id='d1'").fetchone()[0]
    assert status == "scouted"  # stored doc status values are unchanged

    state = dict(c.execute("SELECT key, value FROM routine_state").fetchall())
    assert state["cursor:scalp"] == "2026-10-05T20:00:02.454498Z"
    assert state["cursor:scalp.overnight"] == "2026-10-06T02:00:02.5Z"
    assert state["cursor:research"] == "2026-10-05T19:50:02.488841Z"
    assert not {"cursor:sweep", "cursor:sweep.overnight", "cursor:director"} & set(state)
    assert "missed:sweep:2026-10-05T13:00" in state  # alert history stays
    cut = legacy.cutover(c, legacy.SCALP_CUTOVER_KEY)
    assert cut is not None and cut.tzinfo is not None

    from arc.ingest.store import RawDocRepo

    assert [r["id"] for r in RawDocRepo(c).list_unscalped(limit=None)] == ["d2"]
    indexes = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_raw_docs_unscalped", "idx_scalp_batches_run"} <= indexes
    assert not {"idx_raw_docs_unswept", "idx_sweep_batches_run"} & indexes


def test_scalp_cutover_is_written_once() -> None:
    c = connect(":memory:")
    migrate(c)
    first = legacy.cutover(c, legacy.SCALP_CUTOVER_KEY)
    sql = (MIGRATIONS_DIR / _MIG).read_text()
    c.executescript(sql[sql.index("INSERT OR IGNORE INTO routine_state") :])
    assert legacy.cutover(c, legacy.SCALP_CUTOVER_KEY) == first
    assert {legacy.CUTOVER_KEY, legacy.SCALP_CUTOVER_KEY} <= set(legacy.cutovers(c))


# ---------------------------------------------------------------------------
# Rename chain: both cutovers present / absent
# ---------------------------------------------------------------------------

PRE_D54 = D54 - _dt.timedelta(hours=1)
BETWEEN = D54 + _dt.timedelta(hours=1)
POST_D56 = D56 + _dt.timedelta(hours=1)


@pytest.mark.parametrize(
    ("value", "at", "cuts", "key"),
    [
        # both cutovers recorded
        ("scout", PRE_D54, BOTH, "scalp"),  # scout -> sweep -> scalp
        ("scout.overnight", PRE_D54, BOTH, "scalp.overnight"),
        ("scout", BETWEEN, BOTH, "scout"),  # after 022: the slow-feed Scout, stops at hop 1
        ("scout", POST_D56, BOTH, "scout"),
        ("sweep", BETWEEN, BOTH, "scalp"),
        ("sweep.overnight", BETWEEN, BOTH, "scalp.overnight"),
        ("director", PRE_D54, BOTH, "research"),
        ("director", BETWEEN, BOTH, "research"),
        ("scalp", POST_D56, BOTH, "scalp"),
        ("research", POST_D56, BOTH, "research"),
        ("quant", PRE_D54, BOTH, "quant"),
        ("directorate", PRE_D54, BOTH, "directorate"),  # prefix must be a dotted segment
        # no cutover recorded (a store copy from before the migrations): all history
        ("scout", POST_D56, {}, "scalp"),
        ("sweep", POST_D56, {}, "scalp"),
        ("director", POST_D56, {}, "research"),
        # only D54 recorded (024 not applied yet): every sweep/director row is history
        ("sweep", POST_D56, {legacy.CUTOVER_KEY: D54}, "scalp"),
        ("scout", POST_D56, {legacy.CUTOVER_KEY: D54}, "scout"),
        # no timestamp: treated as history
        ("scout", None, BOTH, "scalp"),
    ],
)
def test_persona_key_chain(
    value: str, at: _dt.datetime | None, cuts: dict[str, _dt.datetime], key: str
) -> None:
    assert legacy.persona_key(value, at, cuts) == key


def test_director_reads_as_research_label() -> None:
    assert legacy.persona_label("director", BETWEEN, BOTH) == "Research"
    assert legacy.persona_label("sweep.overnight", BETWEEN, BOTH) == "Scalp (overnight)"
    assert legacy.persona_label("scout", BETWEEN, BOTH) == "Scout"
    assert legacy.persona_label("director", None) == "Research"


def test_reason_code_composition() -> None:
    assert legacy.reason_code("scout_candidate") == "scalp_candidate"
    assert legacy.reason_code("sweep_candidate") == "scalp_candidate"
    assert legacy.reason_code("director_excluded") == "research_excluded"
    assert legacy.reason_code("director_no_trade") == "research_no_trade"
    assert legacy.reason_code("gate_reject") == "gate_reject"
    for new in legacy.LEGACY_REASON_CODES.values():
        ReasonCode(new)  # every mapped-to code is a current one


def test_legacy_names_cover_the_chain() -> None:
    assert legacy.legacy_names("scalp.overnight") == [
        ("sweep.overnight", legacy.SCALP_CUTOVER_KEY),
        ("scout.overnight", legacy.CUTOVER_KEY),
    ]
    assert legacy.legacy_names("research") == [("director", legacy.SCALP_CUTOVER_KEY)]
    assert legacy.legacy_names("quant") == []


def test_journal_and_history_read_pre_rename_rows(tmp_path: Path) -> None:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    old = "2026-10-01T14:00:00.000000Z"  # before both cutovers (written at migrate time)
    for job in ("director", "sweep", "scout"):
        c.execute(
            "INSERT INTO routine_runs (run_id, job, scheduled_for, started_at, status, reason,"
            " step_index) VALUES (?, ?, ?, ?, 'ok', 'schedule', 0)",
            (f"r-{job}", job, old, old),
        )
    c.commit()
    repo = RoutineRunRepo(c)
    assert {r.run_id for r in repo.history(job="research")} == {"r-director"}
    scalp = repo.history(job="scalp")
    assert {r.run_id for r in scalp} == {"r-sweep", "r-scout"}
    assert {r.job for r in scalp} == {"scalp"}


def test_journal_persona_enum_has_new_names_only() -> None:
    values = {p.value for p in JournalPersona}
    assert {"scalp", "research", "scout"} <= values
    assert not {"sweep", "director"} & values


# ---------------------------------------------------------------------------
# Old names as aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("old", ["sweep", "scout"])
def test_old_setting_names_resolve(old: str) -> None:
    assert is_alias(f"{old}_doc_budget")
    assert lookup(f"{old}_doc_budget").key == "scalp_doc_budget"
    # E13.15: the Scalp floor became the core-tier floor; the new-ticker cap is gone
    assert lookup(f"{old}_min_confidence").key == "universe_floor_core"
    assert f"{old}_max_new_tickers" not in REGISTRY


def test_old_shortlist_key_resolves() -> None:
    key = "order_budget.restrictive.director_max_shortlist"
    assert lookup(key).key == "order_budget.restrictive.research_max_shortlist"


def test_stored_payloads_with_old_names_still_validate() -> None:
    t = PortfolioThesis.model_validate(
        {"director": "x", "sweep_catalyst": "beat", "sweep_confidence": 0.7}
    )
    assert t.research == "x" and t.scalp_catalyst == "beat" and t.scalp_confidence == 0.7
    n = NotePayload.model_validate(
        {"persona": "director", "topic": "thesis", "title": "t", "body": "b"}
    )
    assert n.persona == "research"


def test_shipped_routines_use_new_names(shipped: RoutinesConfig) -> None:
    assert {"scalp", "scalp.overnight", "research"} <= set(shipped.personas)
    assert not {"sweep", "sweep.overnight", "director"} & set(shipped.personas)


# ---------------------------------------------------------------------------
# Grep gate: the old persona names are gone outside history
# ---------------------------------------------------------------------------

_OLD = re.compile(r"\b(Director|Sweep)\b")
_ALLOWED = ("arc/journal/legacy.py", "arc/store/migrations/")


def test_no_old_persona_names_in_code() -> None:
    files = subprocess.run(
        ["git", "ls-files", "arc", "web/src", "hermes/skills"],  # noqa: S607
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


def test_e13_15_aliases_are_gone(shipped_raw: dict) -> None:
    """E13.15: the one-release D56 names no longer resolve."""
    from arc.ingest import llm
    from arc.personas import builders, schemas
    from arc.routines.config import StepSpec
    from arc.routines.handlers import not_implemented

    assert resolve_handler("sweep", StepSpec()) is not_implemented
    assert resolve_handler("director", StepSpec()) is not_implemented
    assert not hasattr(llm, "SweepLLM") and not hasattr(builders, "build_director_prompt")
    assert not hasattr(schemas, "DirectorOutput")
    with pytest.raises(ValueError, match="sweep"):
        _feed("sweep")
