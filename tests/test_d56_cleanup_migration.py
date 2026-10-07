"""E13.15 (D56 cutover): migration 027 drops the dead trending/put_call/UOA cursors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.store.db import connect
from arc.store.migrate import MIGRATIONS_DIR, migrate

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_DEAD = ("cursor:universe.trending", "cursor:put_call", "cursor:unusual_options")


def _store_at_026(path: Path) -> sqlite3.Connection:
    c = connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(sql.stem.split("_", 1)[0])
        if v >= 27:
            break
        c.executescript(sql.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    for key in (*_DEAD, "cursor:scalp", "cursor:universe.momentum"):
        c.execute(
            "INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, "2026-10-06T09:00:00-04:00", "2026-10-06T13:00:00.000000Z"),
        )
    c.commit()
    return c


def test_migration_027_drops_only_dead_cursors(tmp_path: Path) -> None:
    c = _store_at_026(tmp_path / "arc.db")
    assert migrate(c) == [27]
    keys = {r[0] for r in c.execute("SELECT key FROM routine_state")}
    assert keys.isdisjoint(_DEAD)
    assert {"cursor:scalp", "cursor:universe.momentum"} <= keys


def test_migration_027_on_fresh_store_leaves_no_dead_cursor(tmp_path: Path) -> None:
    c = connect(tmp_path / "fresh.db")
    migrate(c)
    assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 27
    keys = {r[0] for r in c.execute("SELECT key FROM routine_state")}
    assert keys.isdisjoint(_DEAD)
