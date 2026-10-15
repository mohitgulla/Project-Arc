"""Execution and open-structure repositories (E6.2; migration ``011_execution.sql``).

``ExecutionRepo`` keeps one row per approved proposal: the D24 price-band ladder
and its outcome. ``OpenStructureRepo`` is Arc's local position model — one row
per structure it opened, so two structures on one underlying stay separate.
Timestamps are stored with :func:`arc.utils.calendar.to_db`.
"""

from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import to_db

if TYPE_CHECKING:
    import datetime as dt

__all__ = ["ExecutionRepo", "OpenStructureRepo", "reserved_open_max_loss"]

log = structlog.get_logger(__name__)


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

    def regate(self, proposal_hash: str, *, max_steps: int) -> None:
        """D101: a re-gated band of *max_steps* steps follows the attempts already made.

        ``max_steps`` becomes ``attempts + max_steps`` so the D32 reservation
        (``max_steps + 1 - attempts``) covers exactly the new band's attempts.
        The row's ``band_lo`` / ``band_hi`` keep the original (approved) band;
        each re-gated band is journalled (``order:band_moved``).
        """
        with self.conn:
            self.conn.execute(
                "UPDATE executions SET max_steps = attempts + ? WHERE proposal_hash = ?",
                (max_steps, proposal_hash),
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
        """Record the pending exit. ``exit_attempts_day`` counts proposals on *day*
        (E11.4, D73): +1 when ``exit_day`` is already *day*, else reset to 1."""
        self.conn.execute(
            """UPDATE open_structures
               SET exit_proposal_hash = ?, exit_reason = ?,
                   exit_attempts_day = CASE WHEN exit_day = ? THEN exit_attempts_day + 1
                                            ELSE 1 END,
                   exit_day = ?
               WHERE id = ?""",
            (proposal_hash, reason, day, day, structure_id),
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


def _expired(expires_at: str | None, now: dt.datetime) -> bool:
    """``expires_at`` (ISO / to_db text) is at or before *now*; unreadable = expired."""
    import datetime as _dt

    if not expires_at:
        return True
    try:
        at = _dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    at = at if at.tzinfo is not None else at.replace(tzinfo=_dt.UTC)
    return at <= now


def reserved_open_max_loss(
    conn: sqlite3.Connection | None,
    *,
    now: dt.datetime | None = None,
    exclude: str | None = None,
) -> Decimal:
    """D116 (E3.7): max loss reserved by in-flight opens, dollars (read-only).

    An open is in flight from its approval request until its execution resolves:
    an open proposal whose ``approval_requests`` row is still ``pending`` or
    ``approved`` with no execution
    yet and whose proposal has not expired at *now* (an approval that lapsed without
    a Broker run stays ``approved``; without *now* none of these count), or an
    ``executions`` row still ``working`` / ``unconfirmed``. Each counts its
    unfilled contracts at the structure's max loss per unit; an execution counts it at
    its band's worst price (``band_hi``), as the gate checked it. *exclude* drops one
    proposal hash: the trade being (re-)gated is never part of its own pre-trade test.
    Unreadable rows are skipped (logged); the broker positions still count once filled.
    """
    if conn is None:
        return Decimal(0)
    rows = conn.execute(
        """SELECT p.proposal_hash, p.structure_json, p.sizing_json, p.expires_at,
                  e.status, e.band_hi, e.contracts, e.filled_qty
           FROM proposals p
           LEFT JOIN approval_requests r ON r.proposal_hash = p.proposal_hash
           LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash
           WHERE p.kind = 'open'
             AND (e.status IN ('working', 'unconfirmed')
                  OR (e.proposal_hash IS NULL AND r.status IN ('pending', 'approved')))"""
    ).fetchall()
    from arc.models import Sizing, Structure

    total = Decimal(0)
    for phash, st_json, sz_json, expires_at, exec_status, band_hi, exec_contracts, filled in rows:
        if exclude is not None and phash == exclude:
            continue
        try:
            if exec_status is None and (now is None or _expired(expires_at, now)):
                continue
            st = Structure.model_validate_json(st_json)
            if st.max_loss is None:
                continue
            per = Decimal(st.max_loss)
            n = Sizing.model_validate_json(sz_json).contracts
            if exec_status is not None:
                n = max(int(exec_contracts) - int(filled or 0), 0)
                # worst fill inside the band the gate checked (band_hi = most debit)
                worst = Decimal(str(band_hi))
                per = max(per + (worst - Decimal(st.net_debit_credit)) * 100, Decimal(0))
        except (ValueError, ArithmeticError, TypeError) as exc:
            log.warning("store.reserved_unreadable", proposal_hash=phash, error=str(exc))
            continue
        total += per * n
    return total
