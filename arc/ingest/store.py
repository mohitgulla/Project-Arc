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

# D54: the stored status of a doc the Sweep read keeps its pre-rename value (``scouted``)
# so status counts stay comparable across the cutover; only identifiers were renamed.
SWEPT_STATUS = "scouted"
# D55 (E4.11): an RSS entry matched by its feed's title filter. Stored (audited, counted
# on the Tower Sources page) but closed at insert, so the Sweep never reads it.
FILTERED_STATUS = "filtered"


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
        closed_status: str | None = None,
    ) -> str | None:
        """Insert a raw doc, skipping duplicates. Returns id or None if duplicate.

        *source_key* is the E4.5 registry source (an RSS feed name). NULL = derived
        at read time by :meth:`arc.ingest.sources.SourceRegistry.key_for`.
        *closed_status* (D55: ``filtered``) stores the doc already closed out of the
        Sweep queue in the same statement (``swept_at`` set, ``sweep_run_id`` NULL until
        the next Sweep run claims it for its count: :meth:`claim_filtered`).
        """
        h = hash_val or content_hash(source, url)
        if self.exists(h):
            log.debug("rawdoc.duplicate", source=source, url=url)
            return None

        row_id = id or _uuid()
        now = _now_iso()
        closed = closed_status is not None
        self.conn.execute(
            """INSERT INTO raw_docs
               (id, source, url, published_at, text, tickers_hint,
                content_hash, ingested_at, run_id, channel_id, title, source_key,
                swept_at, sweep_run_id, sweep_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                source,
                url,
                published_at,
                text,
                json.dumps(tickers_hint or []),
                h,
                now,
                run_id,
                channel_id,
                title,
                source_key,
                now if closed else None,
                None,
                closed_status,
            ),
        )
        self.conn.commit()
        log.info("rawdoc.stored", id=row_id, source=source, url=url, closed_status=closed_status)
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

    # -- Sweep bookkeeping (E4.2) --------------------------------------------

    def list_unswept(self, *, limit: int | None = 200) -> list[dict[str, Any]]:
        """Docs the Sweep has not closed yet (read or budget-skipped), oldest first."""
        sql = "SELECT * FROM raw_docs WHERE swept_at IS NULL ORDER BY published_at ASC, id ASC"
        if limit is None:
            return [dict(r) for r in self.conn.execute(sql).fetchall()]
        rows = self.conn.execute(f"{sql} LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_swept(self, doc_ids: list[str], *, run_id: str) -> None:
        """Mark docs as summarised so later runs skip them."""
        self._close(doc_ids, run_id=run_id, status=SWEPT_STATUS)

    def mark_skipped_budget(self, doc_ids: list[str], *, run_id: str) -> None:
        """E4.5: never selected by the per-source budget before its context TTL ran out.

        Audited (status + run id), never silently dropped; later runs skip them.
        """
        self._close(doc_ids, run_id=run_id, status="skipped_budget")

    def mark_brief_only(self, doc_ids: list[str], *, run_id: str) -> None:
        """D45 (E4.6): a video doc; it reaches trading only through the daily briefs."""
        self._close(doc_ids, run_id=run_id, status="brief_only")

    def mark_skipped_stale(self, doc_ids: list[str], *, run_id: str) -> None:
        """D47 (E4.7): older than its category's ``max_age`` at run time; never read."""
        self._close(doc_ids, run_id=run_id, status="skipped_stale")

    def mark_slow_feed(self, doc_ids: list[str], *, run_id: str) -> None:
        """D54: a ``feed: scout`` source's doc (earnings calendar); never Sweep-read.

        The row stays in ``raw_docs`` (``next_earnings()`` reads it); only the Sweep's
        queue is closed, so it never draws on ``sweep_doc_budget``.
        """
        self._close(doc_ids, run_id=run_id, status="slow_feed")

    def claim_filtered(self, *, run_id: str) -> dict[str, int]:
        """D55: stamp unclaimed ``filtered`` docs with this Sweep run; ``{source_key: n}``.

        Each filtered doc is counted by exactly one Sweep run (the first after it was
        stored), so the card's ``filtered`` count is "since the last Sweep".
        """
        rows = self.conn.execute(
            """SELECT COALESCE(source_key, source) AS k, COUNT(*) AS n FROM raw_docs
               WHERE sweep_status = ? AND sweep_run_id IS NULL GROUP BY k""",
            (FILTERED_STATUS,),
        ).fetchall()
        if rows:
            self.conn.execute(
                """UPDATE raw_docs SET sweep_run_id = ?
                   WHERE sweep_status = ? AND sweep_run_id IS NULL""",
                (run_id, FILTERED_STATUS),
            )
            self.conn.commit()
        return {str(r["k"]): int(r["n"]) for r in rows}

    def _close(self, doc_ids: list[str], *, run_id: str, status: str) -> None:
        now = _now_iso()
        self.conn.executemany(
            """UPDATE raw_docs SET swept_at = ?, sweep_run_id = ?, sweep_status = ?
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
# Sweep batch audit repository (E4.2)
# ---------------------------------------------------------------------------


class SweepBatchRepo:
    """Audit trail of every Sweep LLM call.

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
        stage: str = "sweep",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> str:
        row_id = _uuid()
        self.conn.execute(
            """INSERT INTO sweep_batches
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
            "SELECT * FROM sweep_batches WHERE run_id = ? ORDER BY created_at, id", (run_id,)
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
