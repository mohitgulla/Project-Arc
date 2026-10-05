"""E5.12 (D54): Scout -> Sweep rename — migration, aliases, legacy readers, slow feed."""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import TYPE_CHECKING

import pytest

from arc.control.registry import is_alias, lookup
from arc.ingest.sources import SourceCategory, SourceRegistry
from arc.ingest.store import RawDocRepo
from arc.ingest.sweep import _load_docs, select_docs
from arc.journal import legacy
from arc.pipeline.market import next_earnings
from arc.positions.portfolio import PortfolioThesis
from arc.routines.config import RoutinesConfig, load_routines
from arc.store.db import connect
from arc.store.migrate import MIGRATIONS_DIR, migrate
from arc.universe.config import EarningsConfig
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

_MIG = "022_scout_to_sweep.sql"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(scope="module")
def shipped() -> RoutinesConfig:
    return load_routines()


# ---------------------------------------------------------------------------
# Migration 022 on a pre-rename store
# ---------------------------------------------------------------------------


def _pre_rename_store(path: Path) -> sqlite3.Connection:
    """A store migrated up to 021, with rows written under the old names."""
    c = connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(sql.stem.split("_", 1)[0])
        if v >= 22:
            break
        c.executescript(sql.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    c.execute(
        "INSERT INTO raw_docs (id, source, url, published_at, text, content_hash, ingested_at,"
        " scouted_at, scout_run_id, scout_status)"
        " VALUES ('d1','rss','u1','2026-10-05T12:00:00+00:00','t','h1',"
        " '2026-10-05T12:00:00.000000Z','2026-10-05T12:30:00.000000Z','run-1','scouted')"
    )
    c.execute(
        "INSERT INTO raw_docs (id, source, url, published_at, text, content_hash, ingested_at)"
        " VALUES ('d2','rss','u2','2026-10-05T12:00:00+00:00','t','h2',"
        " '2026-10-05T12:00:00.000000Z')"
    )
    c.execute(
        "INSERT INTO routine_state (key, value, updated_at) VALUES"
        " ('cursor:scout', '2026-10-05T16:00:00.000000Z', '2026-10-05T16:00:00.000000Z'),"
        " ('cursor:scout.overnight', '2026-10-05T08:00:00.000000Z',"
        " '2026-10-05T08:00:00.000000Z'),"
        " ('missed:scout:2026-10-05T13:00', 'x', '2026-10-05T13:00:00.000000Z')"
    )
    c.commit()
    return c


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def test_migration_renames_identifiers_and_moves_cursors(tmp_path: Path) -> None:
    c = _pre_rename_store(tmp_path / "arc.db")
    for i, stage in enumerate(("digest", "scout")):
        c.execute(
            "INSERT INTO scout_batches (id, run_id, model, doc_ids, prompt_sha256, status,"
            " created_at, stage) VALUES (?, 'run-1', 'fixture', '[]', 'x', 'ok',"
            " '2026-10-05T12:30:00.000000Z', ?)",
            (f"b{i}", stage),
        )
    before_docs = c.execute("SELECT COUNT(*) FROM raw_docs").fetchone()[0]
    before_batches = c.execute("SELECT COUNT(*) FROM scout_batches").fetchone()[0]

    applied = migrate(c)
    assert applied[0] == 22  # 022 is the next migration on a 021 store
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sweep_batches" in tables and "scout_batches" not in tables
    assert {"sweep_status", "swept_at", "sweep_run_id"} <= _cols(c, "raw_docs")
    assert not {"scout_status", "scouted_at", "scout_run_id"} & _cols(c, "raw_docs")
    assert c.execute("SELECT COUNT(*) FROM raw_docs").fetchone()[0] == before_docs
    assert c.execute("SELECT COUNT(*) FROM sweep_batches").fetchone()[0] == before_batches
    stages = sorted(r[0] for r in c.execute("SELECT stage FROM sweep_batches"))
    assert stages == ["digest", "sweep"]

    state = dict(c.execute("SELECT key, value FROM routine_state").fetchall())
    assert state["cursor:sweep"] == "2026-10-05T16:00:00.000000Z"
    assert state["cursor:sweep.overnight"] == "2026-10-05T08:00:00.000000Z"
    assert "cursor:scout" not in state and "cursor:scout.overnight" not in state
    assert "missed:scout:2026-10-05T13:00" in state  # alert history stays
    cut = legacy.cutover(c)
    assert cut is not None and cut.tzinfo is not None

    # The swept doc is not re-queued; only the never-read one is pending.
    assert [r["id"] for r in RawDocRepo(c).list_unswept(limit=None)] == ["d2"]
    indexes = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_raw_docs_unswept" in indexes and "idx_raw_docs_unscouted" not in indexes


def test_cutover_is_written_once(conn: sqlite3.Connection) -> None:
    first = legacy.cutover(conn)
    sql = (MIGRATIONS_DIR / _MIG).read_text()
    conn.executescript(sql[sql.index("INSERT OR IGNORE INTO routine_state") :])
    assert legacy.cutover(conn) == first


# ---------------------------------------------------------------------------
# Legacy label helper: rows on both sides of the cutover
# ---------------------------------------------------------------------------

CUT = _dt.datetime(2026, 10, 5, 17, 0, tzinfo=ET)
BEFORE = CUT - _dt.timedelta(minutes=30)
AFTER = CUT + _dt.timedelta(minutes=30)


@pytest.mark.parametrize(
    ("value", "at", "label"),
    [
        ("scout", BEFORE, "Sweep"),
        ("scout.overnight", BEFORE, "Sweep (overnight)"),
        ("scout", AFTER, "Scout"),
        ("scout", None, "Sweep"),
        ("sweep", AFTER, "Sweep"),
        ("director", BEFORE, "Director"),
        ("scouting", BEFORE, "Scouting"),
    ],
)
def test_persona_label_both_sides_of_cutover(
    value: str, at: _dt.datetime | None, label: str
) -> None:
    assert legacy.persona_label(value, at, CUT) == label


def test_no_cutover_means_all_scout_rows_are_sweep() -> None:
    assert legacy.persona_label("scout", AFTER, None) == "Sweep"
    assert legacy.job_name("scout.overnight", AFTER, None) == "sweep.overnight"


def test_reason_code_legacy() -> None:
    assert legacy.reason_code("scout_candidate") == "sweep_candidate"
    assert legacy.reason_code("not_a_candidate") == "not_a_candidate"


def test_cutover_without_routine_state() -> None:
    assert legacy.cutover(sqlite3.connect(":memory:")) is None


# ---------------------------------------------------------------------------
# Old names as aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["doc_budget", "min_confidence", "max_new_tickers"])
def test_old_setting_names_resolve(name: str) -> None:
    assert is_alias(f"scout_{name}")
    assert lookup(f"scout_{name}").key == lookup(f"sweep_{name}").key == f"sweep_{name}"


def test_universe_earnings_scout_key_alias() -> None:
    assert EarningsConfig.model_validate({"scout": "all"}).sweep == "all"
    assert EarningsConfig.model_validate({"scout": "all", "sweep": "seed"}).sweep == "seed"


def test_portfolio_thesis_reads_pre_rename_payload() -> None:
    t = PortfolioThesis.model_validate(
        {"director": "x", "scout_catalyst": "beat", "scout_confidence": 0.7}
    )
    assert t.sweep_catalyst == "beat" and t.sweep_confidence == 0.7


# ---------------------------------------------------------------------------
# D54 revision: the earnings calendar is the slow feed
# ---------------------------------------------------------------------------


def test_earnings_source_is_slow_feed(shipped: RoutinesConfig) -> None:
    reg = SourceRegistry.from_routines(shipped)
    assert reg.sources["earnings"].feed == "scout"
    assert {k for k, s in reg.sources.items() if s.feed == "scout"} == {"earnings"}
    assert "earnings" not in reg.effective_weights()
    # company_data still gets its share through SA / EDGAR
    assert SourceCategory.COMPANY_DATA in reg.category_weights()


def test_sweep_budget_ignores_earnings_and_next_earnings_still_reads_them(
    conn: sqlite3.Connection, shipped: RoutinesConfig
) -> None:
    repo = RawDocRepo(conn)
    for i in range(10):
        repo.insert(
            source="earnings",
            url=f"https://finnhub.io/calendar/earnings/AAPL/2026-10-{10 + i:02d}",
            published_at=f"2026-10-{10 + i:02d}T00:00:00+00:00",
            text="Earnings report for AAPL",
            tickers_hint=["AAPL"],
            source_key="earnings",
        )
    for i in range(5):
        repo.insert(
            source="rss",
            url=f"https://example.com/n{i}",
            published_at="2026-10-05T08:00:00+00:00",
            text=f"Headline {i} about AAPL",
            tickers_hint=["AAPL"],
            source_key="rss.cnbc",
        )
    registry = SourceRegistry.from_routines(shipped)
    docs = _load_docs(repo.list_unswept(limit=None), registry)
    selected, unselected, mix = select_docs(docs, registry, budget=120)
    assert {d.source for d in selected} == {"rss"}
    assert len(selected) == 5
    assert all(d.source != "earnings" for d in unselected)
    assert all("Earnings" not in label for label, *_ in mix)
    assert next_earnings(conn, ["AAPL"], _dt.date(2026, 10, 5))["AAPL"] == _dt.date(2026, 10, 10)


@pytest.mark.parametrize(
    ("job", "feed", "ok"),
    [
        ("earnings", "scout", True),  # schedule 06:00/18:00
        ("earnings", "sweep", False),  # no intraday every
        ("rss", "sweep", True),  # every 15m
        ("rss", "scout", False),  # intraday <= 60m
        ("rss", "fast", False),
    ],
)
def test_feed_validated_against_cadence(job: str, feed: str, ok: bool) -> None:
    import yaml

    from arc.routines.config import DEFAULT_ROUTINES_PATH

    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    raw["sources"][job]["feed"] = feed
    if ok:
        RoutinesConfig.model_validate(raw)
    else:
        with pytest.raises(ValueError, match="feed"):
            RoutinesConfig.model_validate(raw)
