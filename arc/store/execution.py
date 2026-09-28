"""Execution and open-structure repositories (E6.2; migration ``011_execution.sql``).

``ExecutionRepo`` keeps one row per approved proposal: the D24 price-band ladder
and its outcome. ``OpenStructureRepo`` is Arc's local position model — one row
per structure it opened, so two structures on one underlying stay separate.
Timestamps are stored with :func:`arc.utils.calendar.to_db`.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from arc.context.ttl import to_db

if TYPE_CHECKING:
    import datetime as dt
    from decimal import Decimal

__all__ = ["ExecutionRepo", "OpenStructureRepo"]


def _dec(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


class ExecutionRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(
        self,
        *,
        proposal_hash: str,
        kind: str,
        token_version: str,
        band_lo: Decimal,
        band_hi: Decimal,
        max_steps: int,
        contracts: int,
        now: dt.datetime,
        structure_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        """Claim the execution of *proposal_hash*. False if it was already claimed (idempotent)."""
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO executions
                       (proposal_hash, kind, structure_id, status, token_version, band_lo,
                        band_hi, max_steps, contracts, started_at, run_id)
                       VALUES (?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proposal_hash,
                        kind,
                        structure_id,
                        token_version,
                        str(band_lo),
                        str(band_hi),
                        max_steps,
                        contracts,
                        to_db(now),
                        run_id,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def attempt(self, proposal_hash: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE executions SET attempts = attempts + 1 WHERE proposal_hash = ?",
                (proposal_hash,),
            )

    def finish(
        self,
        proposal_hash: str,
        *,
        status: str,
        now: dt.datetime,
        filled_qty: int = 0,
        fill_price: Decimal | None = None,
        steps_used: int | None = None,
        structure_id: str | None = None,
        detail: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE executions
                   SET status = ?, filled_qty = ?, fill_price = ?, steps_used = ?,
                       structure_id = COALESCE(?, structure_id), detail = ?, finished_at = ?
                   WHERE proposal_hash = ?""",
                (
                    status,
                    filled_qty,
                    _dec(fill_price),
                    steps_used,
                    structure_id,
                    detail[:2000],
                    to_db(now),
                    proposal_hash,
                ),
            )

    def get(self, proposal_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM executions WHERE proposal_hash = ?", (proposal_hash,)
        ).fetchone()
        return dict(row) if row else None


class OpenStructureRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def open(
        self,
        *,
        ticker: str,
        open_proposal_hash: str,
        candidate_id: str,
        structure_json: str,
        contracts: int,
        entry_net: Decimal,
        now: dt.datetime,
        commit: bool = True,
    ) -> str:
        sid = f"os-{uuid.uuid4().hex[:16]}"
        self.conn.execute(
            """INSERT INTO open_structures
               (id, ticker, open_proposal_hash, candidate_id, structure_json, contracts,
                entry_net, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                ticker,
                open_proposal_hash,
                candidate_id,
                structure_json,
                contracts,
                str(entry_net),
                to_db(now),
            ),
        )
        if commit:
            self.conn.commit()
        return sid

    def get(self, structure_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM open_structures WHERE id = ?", (structure_id,)
        ).fetchone()
        return dict(row) if row else None

    def by_exit(self, exit_proposal_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM open_structures WHERE exit_proposal_hash = ?", (exit_proposal_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_open(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM open_structures WHERE status = 'open' ORDER BY opened_at, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_exit(
        self, structure_id: str, *, proposal_hash: str, reason: str, day: str, commit: bool = True
    ) -> None:
        self.conn.execute(
            """UPDATE open_structures
               SET exit_proposal_hash = ?, exit_reason = ?, exit_day = ?
               WHERE id = ?""",
            (proposal_hash, reason, day, structure_id),
        )
        if commit:
            self.conn.commit()

    def reduce(
        self,
        structure_id: str,
        *,
        closed_qty: int,
        close_net: Decimal,
        now: dt.datetime,
        commit: bool = True,
    ) -> bool:
        """Close *closed_qty* contracts. Returns True when the structure is now fully closed.

        A partial close keeps the structure open with fewer contracts and clears the
        pending exit, so the next monitor run can propose the remainder.
        """
        row = self.get(structure_id)
        if row is None:
            msg = f"unknown open structure {structure_id}"
            raise LookupError(msg)
        left = int(row["contracts"]) - closed_qty
        if left <= 0:
            self.conn.execute(
                """UPDATE open_structures
                   SET status = 'closed', closed_at = ?, close_net = ?
                   WHERE id = ?""",
                (to_db(now), str(close_net), structure_id),
            )
        else:
            self.conn.execute(
                """UPDATE open_structures
                   SET contracts = ?, exit_proposal_hash = NULL
                   WHERE id = ?""",
                (left, structure_id),
            )
        if commit:
            self.conn.commit()
        return left <= 0
