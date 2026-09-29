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
        channel_id: str | None = None,
        title: str | None = None,
        source_key: str | None = None,
    ) -> str | None:
        """Insert a raw doc, skipping duplicates. Returns id or None if duplicate.

        *source_key* is the E4.5 registry source (an RSS feed name). NULL = derived
        at read time by :meth:`arc.ingest.sources.SourceRegistry.key_for`.
        """
        h = hash_val or content_hash(source, url)
        if self.exists(h):
            log.debug("rawdoc.duplicate", source=source, url=url)
            return None

        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO raw_docs
               (id, source, url, published_at, text, tickers_hint,
                content_hash, ingested_at, run_id, channel_id, title, source_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                channel_id,
                title,
                source_key,
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

    # -- Scout bookkeeping (E4.2) --------------------------------------------

    def list_unscouted(self, *, limit: int | None = 200) -> list[dict[str, Any]]:
        """Docs the Scout has not closed yet (read or budget-skipped), oldest first."""
        sql = "SELECT * FROM raw_docs WHERE scouted_at IS NULL ORDER BY published_at ASC, id ASC"
        if limit is None:
            return [dict(r) for r in self.conn.execute(sql).fetchall()]
        rows = self.conn.execute(f"{sql} LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_scouted(self, doc_ids: list[str], *, run_id: str) -> None:
        """Mark docs as summarised so later runs skip them."""
        self._close(doc_ids, run_id=run_id, status="scouted")

    def mark_skipped_budget(self, doc_ids: list[str], *, run_id: str) -> None:
        """E4.5: never selected by the per-source budget before its context TTL ran out.

        Audited (status + run id), never silently dropped; later runs skip them.
        """
        self._close(doc_ids, run_id=run_id, status="skipped_budget")

    def _close(self, doc_ids: list[str], *, run_id: str, status: str) -> None:
        now = _now_iso()
        self.conn.executemany(
            """UPDATE raw_docs SET scouted_at = ?, scout_run_id = ?, scout_status = ?
               WHERE id = ?""",
            [(now, run_id, status, d) for d in doc_ids],
        )
        self.conn.commit()

    def source_keys_for_urls(self, urls: list[str]) -> dict[str, dict[str, Any]]:
        """``url -> row`` (source, source_key, url, channel_id) for stored docs."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(urls), 500):
            chunk = urls[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT url, source, source_key, channel_id FROM raw_docs WHERE url IN ({marks})",  # noqa: S608
                chunk,
            ).fetchall()
            for r in rows:
                out.setdefault(r["url"], dict(r))
        return out


# ---------------------------------------------------------------------------
# Scout batch audit repository (E4.2)
# ---------------------------------------------------------------------------


class ScoutBatchRepo:
    """Audit trail of every Scout LLM call.

    Unstructured persona output (the verbatim response, including each
    candidate's rationale) is stored here and nowhere else.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        run_id: str,
        model: str,
        doc_ids: list[str],
        prompt: str,
        raw_response: str | None,
        status: str,
        error: str | None = None,
        accepted: int = 0,
        rejected: dict[str, int] | None = None,
        stage: str = "scout",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> str:
        row_id = _uuid()
        self.conn.execute(
            """INSERT INTO scout_batches
               (id, run_id, model, doc_ids, prompt_sha256, raw_response, status,
                error, accepted, rejected, created_at, stage, input_tokens,
                output_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                run_id,
                model,
                json.dumps(doc_ids),
                hashlib.sha256(prompt.encode()).hexdigest(),
                raw_response,
                status,
                error,
                accepted,
                json.dumps(rejected or {}, sort_keys=True),
                _now_iso(),
                stage,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )
        self.conn.commit()
        return row_id

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM scout_batches WHERE run_id = ? ORDER BY created_at, id", (run_id,)
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
