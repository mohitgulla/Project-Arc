"""SQLite-backed repositories for every audit entity.

Each repository operates on a ``sqlite3.Connection`` passed at
construction time.  Writes are explicit (no implicit commits).

Order-related writes go through ``OrderRepository``, which enforces
the state machine and writes event rows atomically.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from arc.models import OrderState
from arc.store.order_state import validate_transition

log = structlog.get_logger()


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Candidate repository
# ---------------------------------------------------------------------------


class CandidateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        ticker: str,
        stance: str,
        catalyst_type: str,
        catalyst_date: str | None = None,
        confidence: float,
        sources: list[str] | None = None,
        created_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO candidates
               (id, ticker, stance, catalyst_type, catalyst_date,
                confidence, sources, created_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                ticker,
                stance,
                catalyst_type,
                catalyst_date,
                confidence,
                json.dumps(sources or []),
                created_at or _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        return row_id

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Proposal repository
# ---------------------------------------------------------------------------


class ProposalRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        candidate_id: str,
        proposal_hash: str,
        structure_json: str,
        thesis: str,
        quant_json: str,
        risk_narrative: str = "",
        sizing_json: str,
        expires_at: str,
        created_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO proposals
               (id, candidate_id, proposal_hash, structure_json, thesis,
                quant_json, risk_narrative, sizing_json, expires_at, created_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                candidate_id,
                proposal_hash,
                structure_json,
                thesis,
                quant_json,
                risk_narrative,
                sizing_json,
                expires_at,
                created_at or _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        return row_id

    def get_by_hash(self, proposal_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_hash = ?", (proposal_hash,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Gate decision repository
# ---------------------------------------------------------------------------


class GateDecisionRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        proposal_hash: str,
        passed: bool,
        violations: list[str] | None = None,
        token: str | None = None,
        account_snapshot: dict[str, Any] | None = None,
        decided_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO gate_decisions
               (id, proposal_hash, passed, violations_json, token,
                account_snapshot, decided_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                proposal_hash,
                1 if passed else 0,
                json.dumps(violations or []),
                token,
                json.dumps(account_snapshot or {}),
                decided_at or _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        return row_id


# ---------------------------------------------------------------------------
# Approval repository
# ---------------------------------------------------------------------------


class ApprovalRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        proposal_hash: str,
        slack_user: str,
        slack_ts: str,
        decision: str,
        decided_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO approvals
               (id, proposal_hash, slack_user, slack_ts, decision, decided_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                proposal_hash,
                slack_user,
                slack_ts,
                decision,
                decided_at or _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        return row_id


# ---------------------------------------------------------------------------
# Order repository (event-sourced state machine)
# ---------------------------------------------------------------------------


class OrderRepo:
    """Event-sourced order repository.

    Every ``transition()`` validates the state machine, writes an event
    row, and updates the order's ``state`` + ``updated_at`` atomically.

    Idempotency: ``client_order_id`` is UNIQUE.  ``create()`` with a
    duplicate is a no-op returning the existing order id.

    Event replay: ``transition()`` with an identical
    ``(order_id, from_state, to_state, event_at)`` tuple is a no-op
    (UNIQUE constraint on ``order_events``).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        proposal_hash: str,
        client_order_id: str,
        run_id: str | None = None,
        id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        """Create a new order in ``proposed`` state.

        Returns the order id.  If ``client_order_id`` already exists,
        returns the existing id (idempotent).
        """
        existing = self.conn.execute(
            "SELECT id FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if existing:
            log.info("order.duplicate_client_id", client_order_id=client_order_id)
            return existing[0]

        row_id = id or _uuid()
        now = created_at or _now_iso()
        self.conn.execute(
            """INSERT INTO orders
               (id, proposal_hash, client_order_id, state, broker_order_id,
                created_at, updated_at, run_id)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
            (row_id, proposal_hash, client_order_id, OrderState.PROPOSED.value, now, now, run_id),
        )
        self.conn.commit()
        return row_id

    def get(self, order_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def get_events(self, order_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def transition(
        self,
        *,
        order_id: str,
        to_state: OrderState,
        actor: str = "",
        detail: str = "",
        event_at: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Transition an order to a new state.

        Raises ``IllegalTransitionError`` on invalid moves.
        Idempotent: replaying the exact same event (same from, to, event_at)
        is a silent no-op.
        """
        order = self.get(order_id)
        if order is None:
            msg = f"Order not found: {order_id}"
            raise ValueError(msg)

        from_state = OrderState(order["state"])
        now = event_at or _now_iso()

        # Idempotency: if this exact event already exists, it's a replay — no-op.
        existing = self.conn.execute(
            """SELECT 1 FROM order_events
               WHERE order_id = ? AND to_state = ? AND event_at = ?""",
            (order_id, to_state.value, now),
        ).fetchone()
        if existing:
            log.info(
                "order.event_replay_noop",
                order_id=order_id,
                to_state=to_state.value,
            )
            return

        validate_transition(from_state, to_state)

        try:
            self.conn.execute(
                """INSERT INTO order_events
                   (order_id, from_state, to_state, actor, detail, event_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_id, from_state.value, to_state.value, actor, detail, now, run_id),
            )
        except sqlite3.IntegrityError:
            # Belt-and-suspenders: concurrent replay.
            log.info(
                "order.event_replay_noop",
                order_id=order_id,
                to_state=to_state.value,
            )
            return

        self.conn.execute(
            "UPDATE orders SET state = ?, updated_at = ? WHERE id = ?",
            (to_state.value, now, order_id),
        )
        self.conn.commit()
        log.info(
            "order.transitioned",
            order_id=order_id,
            from_state=from_state.value,
            to_state=to_state.value,
            actor=actor,
        )

    def set_broker_order_id(self, order_id: str, broker_order_id: str) -> None:
        self.conn.execute(
            "UPDATE orders SET broker_order_id = ? WHERE id = ?",
            (broker_order_id, order_id),
        )
        self.conn.commit()


# ---------------------------------------------------------------------------
# Fill repository
# ---------------------------------------------------------------------------


class FillRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        order_id: str,
        qty: int,
        price: str,
        filled_at: str | None = None,
        broker_fill_id: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO fills
               (id, order_id, broker_fill_id, qty, price, filled_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row_id, order_id, broker_fill_id, qty, price, filled_at or _now_iso(), run_id),
        )
        self.conn.commit()
        return row_id


# ---------------------------------------------------------------------------
# Snapshot repositories (positions, PnL)
# ---------------------------------------------------------------------------


class PositionsSnapshotRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        positions_json: str,
        snapshot_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO positions_snapshots (id, snapshot_at, positions_json, run_id)
               VALUES (?, ?, ?, ?)""",
            (row_id, snapshot_at or _now_iso(), positions_json, run_id),
        )
        self.conn.commit()
        return row_id


class PnlSnapshotRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(
        self,
        *,
        realized: str,
        unrealized: str,
        total: str,
        details_json: str = "{}",
        snapshot_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO pnl_snapshots
               (id, snapshot_at, realized, unrealized, total, details_json, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row_id, snapshot_at or _now_iso(), realized, unrealized, total, details_json, run_id),
        )
        self.conn.commit()
        return row_id


# ---------------------------------------------------------------------------
# Halt repository
# ---------------------------------------------------------------------------


class HaltRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def halt(
        self,
        *,
        reason: str = "",
        actor: str = "",
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO halts (id, halted_at, reason, actor, run_id)
               VALUES (?, ?, ?, ?, ?)""",
            (row_id, _now_iso(), reason, actor, run_id),
        )
        self.conn.commit()
        return row_id

    def resume(self, halt_id: str, *, actor: str = "") -> None:
        self.conn.execute(
            "UPDATE halts SET resumed_at = ? WHERE id = ? AND resumed_at IS NULL",
            (_now_iso(), halt_id),
        )
        self.conn.commit()

    def is_halted(self) -> bool:
        """True if there is an active (un-resumed) halt."""
        row = self.conn.execute("SELECT 1 FROM halts WHERE resumed_at IS NULL LIMIT 1").fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Tax lot repository
# ---------------------------------------------------------------------------


class TaxLotRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def open_lot(
        self,
        *,
        order_id: str,
        ticker: str,
        occ_symbol: str,
        side: str,
        qty: int,
        open_price: str,
        opened_at: str | None = None,
        run_id: str | None = None,
        id: str | None = None,
    ) -> str:
        row_id = id or _uuid()
        self.conn.execute(
            """INSERT INTO tax_lots
               (id, order_id, ticker, occ_symbol, side, qty, open_price,
                opened_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                order_id,
                ticker,
                occ_symbol,
                side,
                qty,
                open_price,
                opened_at or _now_iso(),
                run_id,
            ),
        )
        self.conn.commit()
        return row_id

    def close_lot(
        self,
        lot_id: str,
        *,
        close_price: str,
        realized_pnl: str,
        closed_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE tax_lots
               SET close_price = ?, realized_pnl = ?, closed_at = ?
               WHERE id = ? AND closed_at IS NULL""",
            (close_price, realized_pnl, closed_at or _now_iso(), lot_id),
        )
        self.conn.commit()

    def open_lots_for_ticker(self, ticker: str, *, days: int = 30) -> list[dict[str, Any]]:
        """Return lots closed within ``days`` for wash-sale checking."""
        rows = self.conn.execute(
            """SELECT * FROM tax_lots
               WHERE ticker = ?
                 AND closed_at IS NOT NULL
                 AND julianday('now') - julianday(closed_at) <= ?""",
            (ticker, days),
        ).fetchall()
        return [dict(r) for r in rows]
