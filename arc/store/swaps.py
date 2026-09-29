"""Close-to-reallocate swaps (E6.4, D19; table ``swaps``, migration 013)."""

from __future__ import annotations

import sqlite3  # noqa: TC003 - runtime type of self.conn
import uuid
from typing import TYPE_CHECKING, Any, Literal

from arc.context.ttl import to_db

if TYPE_CHECKING:
    import datetime as dt

__all__ = ["SwapRepo", "SwapStatus"]

SwapStatus = Literal["vetoed", "closing", "open_proposed", "cancelled"]
_CHURN = ("closing", "open_proposed", "cancelled")  # a close was attempted


class SwapRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        day: str,
        status: SwapStatus,
        close_structure_id: str,
        close_ticker: str,
        open_ticker: str,
        source_ref: str,
        suggestion_json: str,
        now: dt.datetime,
        run_id: str | None,
        detail: str = "",
        commit: bool = True,
    ) -> str:
        sid = f"swap-{uuid.uuid4().hex[:16]}"
        ts = to_db(now)
        self.conn.execute(
            """INSERT INTO swaps
               (id, day, status, detail, close_structure_id, close_ticker, open_ticker,
                source_ref, suggestion_json, run_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                day,
                status,
                detail,
                close_structure_id,
                close_ticker,
                open_ticker,
                source_ref,
                suggestion_json,
                run_id,
                ts,
                ts,
            ),
        )
        if commit:
            self.conn.commit()
        return sid

    def update(
        self,
        swap_id: str,
        *,
        status: SwapStatus,
        now: dt.datetime,
        detail: str | None = None,
        close_proposal_hash: str | None = None,
        open_proposal_hash: str | None = None,
        commit: bool = True,
    ) -> None:
        self.conn.execute(
            """UPDATE swaps SET status = ?, updated_at = ?,
                   detail = COALESCE(?, detail),
                   close_proposal_hash = COALESCE(?, close_proposal_hash),
                   open_proposal_hash = COALESCE(?, open_proposal_hash)
               WHERE id = ?""",
            (status, to_db(now), detail, close_proposal_hash, open_proposal_hash, swap_id),
        )
        if commit:
            self.conn.commit()

    def get(self, swap_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        return dict(row) if row else None

    def by_status(self, status: SwapStatus) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM swaps WHERE status = ? ORDER BY created_at, id", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def for_day(self, day: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM swaps WHERE day = ? ORDER BY created_at, id", (day,)
        ).fetchall()
        return [dict(r) for r in rows]

    def sources(self) -> set[str]:
        return {str(r[0]) for r in self.conn.execute("SELECT source_ref FROM swaps")}

    def churn(self, day: str) -> tuple[int, dict[str, int]]:
        """(swaps attempted on *day*, per-ticker count on either side)."""
        rows = [r for r in self.for_day(day) if r["status"] in _CHURN]
        per: dict[str, int] = {}
        for r in rows:
            for t in {r["close_ticker"], r["open_ticker"]}:
                per[t] = per.get(t, 0) + 1
        return len(rows), per
