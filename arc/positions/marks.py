"""Stored marks of an open structure (E18.1, D78): the profit lock's peak P&L.

``positions.evaluate`` writes one ``position_review`` context entry per open
structure every 30 minutes (subject = the open structure id). Those entries are
the only per-structure mark history in the store, so the profit lock's peak is
derived from them; no migration, no new table. Superseded and expired entries
count: a mark stays a mark after its context entry is replaced.

Per-share P&L of a stored mark = ``current_value − entry_net`` (both per share at
mid, as written by :func:`arc.positions.evaluate.review_position`).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import TYPE_CHECKING

from arc.context.ttl import to_db
from arc.exits.policy import peak_pnl

if TYPE_CHECKING:
    import sqlite3
    from decimal import Decimal

__all__ = ["stored_mark_pnls", "stored_peak_pnl"]


def _since(opened_at: str | None) -> str | None:
    """``opened_at`` in the ``created_at`` text form (``…Z``) for a string compare."""
    if not opened_at:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(opened_at))
    except ValueError:
        return None
    return to_db(ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts)


def stored_mark_pnls(
    conn: sqlite3.Connection,
    structure_id: str,
    *,
    opened_at: str | None = None,
    before: _dt.datetime | None = None,
) -> list[tuple[str, float]]:
    """``(created_at, per-share P&L)`` of every stored mark of *structure_id*, oldest first.

    Only marks at or after *opened_at* (the open fill) and, when given, strictly
    before *before* count. Rows whose payload lacks a finite ``current_value`` /
    ``entry_net`` are skipped.
    """
    sql = [
        "SELECT created_at, payload FROM context_entries",
        "WHERE kind = 'position_review' AND subject = ?",
    ]
    params: list[object] = [structure_id]
    since = _since(opened_at)
    if since is not None:
        sql.append("AND created_at >= ?")
        params.append(since)
    if before is not None:
        sql.append("AND created_at < ?")
        params.append(to_db(before))
    sql.append("ORDER BY created_at, rowid")
    out: list[tuple[str, float]] = []
    for created_at, payload in conn.execute(" ".join(sql), params).fetchall():
        try:
            data = json.loads(payload)
            pnl = float(data["current_value"]) - float(data["entry_net"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(pnl):
            out.append((str(created_at), round(pnl, 6)))
    return out


def stored_peak_pnl(
    conn: sqlite3.Connection,
    structure_id: str,
    *,
    opened_at: str | None = None,
    before: _dt.datetime | None = None,
) -> Decimal | None:
    """Peak per-share P&L over the stored marks (``None`` = no stored mark)."""
    marks = stored_mark_pnls(conn, structure_id, opened_at=opened_at, before=before)
    return peak_pnl(p for _, p in marks)
