"""SQLite-backed repositories for raw documents and ingest cursors."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger()


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def content_hash(source: str, url: str) -> str:
    """SHA-256 hex digest of ``source + url`` for deduplication."""
    return hashlib.sha256(f"{source}:{url}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# RawDoc repository
# ---------------------------------------------------------------------------


class RawDocRepo:
    """Persist and deduplicate raw ingested documents."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def exists(self, hash_val: str) -> bool:
        """Return True if a document with this content_hash already exists."""
        row = self.conn.execute(
            "SELECT 1 FROM raw_docs WHERE content_hash = ?", (hash_val,)
        ).fetchone()
        return row is not None

    def insert(
        self,
        *,
        source: str,
        url: str,
        published_at: str,
        text: str,
        tickers_hint: list[str] | None = None,
        hash_val: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str | None:
        """Insert a raw doc, skipping duplicates. Returns id or None if duplicate."""
        h = hash_val or content_hash(source, url)
        if self.exists(h):
            log.debug("rawdoc.duplicate", source=source, url=url)
            return None

        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO raw_docs
               (id, source, url, published_at, text, tickers_hint,
                content_hash, ingested_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                source,
                url,
                published_at,
                text,
                json.dumps(tickers_hint or []),
                h,
                _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        log.info("rawdoc.stored", id=row_id, source=source, url=url)
        return row_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM raw_docs WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def list_by_source(self, source: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM raw_docs WHERE source = ? ORDER BY published_at DESC LIMIT ?",
            (source, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Ingest cursor repository
# ---------------------------------------------------------------------------


class IngestCursorRepo:
    """Persist incremental cursors (last-seen position per connector)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, connector: str) -> str | None:
        """Return the saved cursor value, or None if never set."""
        row = self.conn.execute(
            "SELECT cursor_val FROM ingest_cursors WHERE connector = ?",
            (connector,),
        ).fetchone()
        return row[0] if row else None

    def set(self, connector: str, cursor_val: str) -> None:
        """Upsert the cursor for a connector."""
        self.conn.execute(
            """INSERT INTO ingest_cursors (connector, cursor_val, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(connector) DO UPDATE
               SET cursor_val = excluded.cursor_val,
                   updated_at = excluded.updated_at""",
            (connector, cursor_val, _now_iso()),
        )
        self.conn.commit()
