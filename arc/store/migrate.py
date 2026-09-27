"""Simple versioned SQL migration runner.

Reads numbered ``.sql`` files from ``arc/store/migrations/`` and applies
them in order, skipping any that have already been applied (tracked in
the ``schema_version`` table).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import structlog

log = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs for unapplied migrations, sorted."""
    cur = current_version(conn)
    result: list[tuple[int, Path]] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        # Filename must start with a zero-padded number, e.g. 001_initial.sql
        try:
            version = int(sql_file.stem.split("_", 1)[0])
        except ValueError:
            continue
        if version > cur:
            result.append((version, sql_file))
    return result


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations inside a transaction.

    Returns the list of versions that were applied.
    """
    _ensure_version_table(conn)
    applied: list[int] = []
    for version, path in pending_migrations(conn):
        sql = path.read_text()
        log.info("migrate.applying", version=version, file=path.name)
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
        applied.append(version)
        log.info("migrate.applied", version=version)
    return applied
