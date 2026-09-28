"""Audit tables of the pipeline runner (E5.2): persona calls + per-day proposal lookups."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from arc.context.ttl import to_db
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

__all__ = ["PersonaCallRepo", "proposals_for_day"]


class PersonaCallRepo:
    """``persona_calls``: one row per Director/Quant/Risk LLM call (verbatim reply kept)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        run_id: str,
        persona: str,
        model: str,
        snapshot_id: str | None,
        prompt: str,
        raw_response: str | None,
        status: str,
        error: str | None = None,
        dropped: dict[str, int] | None = None,
        at: _dt.datetime | None = None,
    ) -> str:
        row_id = f"pc-{uuid.uuid4().hex[:16]}"
        with self.conn:
            self.conn.execute(
                """INSERT INTO persona_calls
                   (id, run_id, persona, model, snapshot_id, prompt_sha256, raw_response,
                    status, error, dropped, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    run_id,
                    persona,
                    model,
                    snapshot_id,
                    hashlib.sha256(prompt.encode()).hexdigest(),
                    raw_response,
                    status,
                    error,
                    json.dumps(dropped or {}, sort_keys=True),
                    to_db(at or now_et()),
                ),
            )
        return row_id

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM persona_calls WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def proposals_for_day(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    """Proposals keyed to session *day* (YYYY-MM-DD, ET), each with its latest gate decision."""
    rows = conn.execute(
        """SELECT p.*, g.passed AS gate_passed, g.violations_json AS gate_violations,
                  g.token IS NOT NULL AS gate_token
           FROM proposals p
           LEFT JOIN gate_decisions g ON g.id = (
               SELECT id FROM gate_decisions WHERE proposal_hash = p.proposal_hash
               ORDER BY decided_at DESC, rowid DESC LIMIT 1)
           WHERE p.day = ? ORDER BY p.created_at, p.rowid""",
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]
