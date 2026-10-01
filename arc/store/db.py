"""Database connection manager for the Arc audit store.

Provides a thin wrapper around sqlite3 that:
- Enforces WAL mode, foreign keys and a busy timeout on every connection.
- Centralises the default path (data/arc.db relative to project root).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import structlog

log = structlog.get_logger()

DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "arc.db"
BUSY_TIMEOUT_MS = 30_000  # E5.10: tick + background children share the store


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return a configured SQLite connection.

    Args:
        db_path: Explicit path; defaults to ``data/arc.db`` (gitignored).
                 Use ``:memory:`` for tests.
    """
    path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)

    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # E5.10: the tick and its background children write concurrently; a writer
    # waits for the other's commit instead of failing with "database is locked".
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    log.debug("db.connected", path=path)
    return conn


def connect_ro(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open an existing store read-only (``mode=ro`` + ``query_only``); never creates it.

    Raises ``FileNotFoundError`` when the file is missing, so a read-only view
    pointed at a wrong path cannot leave an empty ``arc.db`` behind.
    """
    path = Path(db_path if db_path is not None else DEFAULT_DB_PATH).expanduser().resolve()
    if not path.is_file():
        msg = f"audit store not found: {path} (read-only views never create one)"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    log.debug("db.connected", path=str(path), mode="ro")
    return conn
