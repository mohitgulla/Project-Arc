"""Tests for the audit store: migrations, state machine, repositories, idempotency, crash-replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3

from arc.models import OrderState
from arc.store.db import connect
from arc.store.migrate import current_version, migrate, pending_migrations
from arc.store.order_state import (
    VALID_TRANSITIONS,
    IllegalTransitionError,
    is_terminal,
    validate_transition,
)
from arc.store.repos import (
    ApprovalRepo,
    CandidateRepo,
    FillRepo,
    GateDecisionRepo,
    HaltRepo,
    OrderRepo,
    PnlSnapshotRepo,
    PositionsSnapshotRepo,
    ProposalRepo,
    TaxLotRepo,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """In-memory DB with migrations applied."""
    c = connect(":memory:")
    migrate(c)
    return c


def _seed_candidate(conn: sqlite3.Connection) -> str:
    repo = CandidateRepo(conn)
    return repo.insert(
        ticker="AAPL",
        stance="bullish",
        catalyst_type="earnings",
        confidence=0.85,
        sources=["reuters"],
    )


def _seed_proposal(conn: sqlite3.Connection, candidate_id: str) -> str:
    repo = ProposalRepo(conn)
    return repo.insert(
        candidate_id=candidate_id,
        proposal_hash="abc123",
        structure_json="{}",
        thesis="test thesis",
        quant_json="{}",
        sizing_json="{}",
        expires_at="2026-12-31T00:00:00Z",
    )


def _seed_order(conn: sqlite3.Connection, proposal_hash: str = "abc123") -> str:
    repo = OrderRepo(conn)
    return repo.create(
        proposal_hash=proposal_hash,
        client_order_id="coid-001",
        run_id="run-1",
    )


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigrations:
    def test_migrate_creates_tables(self, conn: sqlite3.Connection):
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        expected = {
            "schema_version",
            "candidates",
            "proposals",
            "gate_decisions",
            "approvals",
            "orders",
            "order_events",
            "fills",
            "positions_snapshots",
            "pnl_snapshots",
            "halts",
            "tax_lots",
        }
        assert expected.issubset(tables), f"Missing: {expected - tables}"

    def test_current_version_after_migrate(self, conn: sqlite3.Connection):
        assert current_version(conn) == 34

    def test_no_pending_after_migrate(self, conn: sqlite3.Connection):
        assert pending_migrations(conn) == []

    def test_migrate_is_idempotent(self, conn: sqlite3.Connection):
        applied = migrate(conn)
        assert applied == []  # nothing new to apply

    def test_foreign_keys_enabled(self, conn: sqlite3.Connection):
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------------
# Order state machine tests (table-driven)
# ---------------------------------------------------------------------------


# Every (from, to) pair — legal ones should pass, illegal should raise.
_ALL_STATES = list(OrderState)

# Build the full expected-legal set from the transition table.
_LEGAL_PAIRS = {
    (from_st, to_st) for from_st, targets in VALID_TRANSITIONS.items() for to_st in targets
}


class TestOrderStateMachine:
    @pytest.mark.parametrize(
        "from_state,to_state",
        sorted(_LEGAL_PAIRS, key=lambda p: (p[0].value, p[1].value)),
        ids=[
            f"{f.value}->{t.value}"
            for f, t in sorted(_LEGAL_PAIRS, key=lambda p: (p[0].value, p[1].value))
        ],
    )
    def test_legal_transition(self, from_state: OrderState, to_state: OrderState):
        # Should not raise
        validate_transition(from_state, to_state)

    @pytest.mark.parametrize(
        "from_state,to_state",
        [(f, t) for f in _ALL_STATES for t in _ALL_STATES if (f, t) not in _LEGAL_PAIRS][
            :30
        ],  # sample — full cartesian would be noisy
        ids=lambda pair: (
            f"{pair[0].value}->{pair[1].value}" if isinstance(pair, tuple) else str(pair)
        ),
    )
    def test_illegal_transition(self, from_state: OrderState, to_state: OrderState):
        with pytest.raises(IllegalTransitionError):
            validate_transition(from_state, to_state)

    def test_terminal_states_have_no_outgoing(self):
        terminals = {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
        for state in terminals:
            assert is_terminal(state), f"{state.value} should be terminal"

    def test_proposed_is_not_terminal(self):
        assert not is_terminal(OrderState.PROPOSED)


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


class TestCandidateRepo:
    def test_insert_and_get(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        row = CandidateRepo(conn).get(cid)
        assert row is not None
        assert row["ticker"] == "AAPL"
        assert row["stance"] == "bullish"

    def test_get_missing(self, conn: sqlite3.Connection):
        assert CandidateRepo(conn).get("nonexistent") is None


class TestProposalRepo:
    def test_insert_and_get_by_hash(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        row = ProposalRepo(conn).get_by_hash("abc123")
        assert row is not None
        assert row["thesis"] == "test thesis"


class TestGateDecisionRepo:
    def test_insert(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        gid = GateDecisionRepo(conn).insert(
            proposal_hash="abc123",
            passed=True,
            violations=[],
            token="hmac-token",
        )
        assert gid


class TestApprovalRepo:
    def test_insert(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        aid = ApprovalRepo(conn).insert(
            proposal_hash="abc123",
            slack_user="U123",
            slack_ts="1234567890.123456",
            decision="approved",
        )
        assert aid


class TestOrderRepo:
    def test_create_and_get(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        order = OrderRepo(conn).get(oid)
        assert order is not None
        assert order["state"] == "proposed"
        assert order["client_order_id"] == "coid-001"

    def test_idempotent_create(self, conn: sqlite3.Connection):
        """Duplicate client_order_id returns existing order id."""
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid1 = _seed_order(conn)
        oid2 = OrderRepo(conn).create(
            proposal_hash="abc123",
            client_order_id="coid-001",
            run_id="run-2",
        )
        assert oid1 == oid2

    def test_happy_path_transitions(self, conn: sqlite3.Connection):
        """Walk the full happy path: proposed → gated → approved → submitted → filled."""
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = OrderRepo(conn)

        for to_state in [
            OrderState.GATED,
            OrderState.APPROVED,
            OrderState.SUBMITTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        ]:
            repo.transition(order_id=oid, to_state=to_state, actor="test")

        order = repo.get(oid)
        assert order["state"] == "filled"

        events = repo.get_events(oid)
        assert len(events) == 5

    def test_illegal_transition_raises(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = OrderRepo(conn)

        with pytest.raises(IllegalTransitionError):
            repo.transition(order_id=oid, to_state=OrderState.FILLED, actor="test")

    def test_transition_on_missing_order_raises(self, conn: sqlite3.Connection):
        repo = OrderRepo(conn)
        with pytest.raises(ValueError, match="Order not found"):
            repo.transition(order_id="ghost", to_state=OrderState.GATED, actor="test")

    def test_event_replay_is_noop(self, conn: sqlite3.Connection):
        """Replaying the exact same event (from, to, event_at) is idempotent."""
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = OrderRepo(conn)

        ts = "2026-10-01T12:00:00.000000Z"
        repo.transition(
            order_id=oid,
            to_state=OrderState.GATED,
            actor="test",
            event_at=ts,
        )
        # Replay — should not raise or create duplicate
        repo.transition(
            order_id=oid,
            to_state=OrderState.GATED,
            actor="test",
            event_at=ts,
        )
        events = repo.get_events(oid)
        assert len(events) == 1

    def test_set_broker_order_id(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = OrderRepo(conn)
        repo.set_broker_order_id(oid, "broker-abc")
        order = repo.get(oid)
        assert order["broker_order_id"] == "broker-abc"


class TestCrashReplay:
    """Simulate a crash mid-pipeline and replay events."""

    def test_crash_and_replay(self, conn: sqlite3.Connection):
        """Create order, transition to gated, 'crash', then replay same + continue."""
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = OrderRepo(conn)

        ts1 = "2026-10-01T12:00:00.000000Z"
        repo.transition(order_id=oid, to_state=OrderState.GATED, actor="bot", event_at=ts1)

        # --- simulate crash: open a new connection to the same DB ---
        # (in-memory DBs are per-connection, so we just re-use conn to
        # simulate reading committed state)

        # Replay the same event — should be a no-op
        repo.transition(order_id=oid, to_state=OrderState.GATED, actor="bot", event_at=ts1)

        # Continue from where we left off
        repo.transition(order_id=oid, to_state=OrderState.APPROVED, actor="human")
        repo.transition(order_id=oid, to_state=OrderState.SUBMITTED, actor="exec")

        order = repo.get(oid)
        assert order["state"] == "submitted"
        assert len(repo.get_events(oid)) == 3


class TestFillRepo:
    def test_insert(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        fid = FillRepo(conn).insert(order_id=oid, qty=10, price="3.45")
        assert fid


class TestHaltRepo:
    def test_halt_and_resume(self, conn: sqlite3.Connection):
        repo = HaltRepo(conn)
        assert not repo.is_halted()
        hid = repo.halt(reason="manual", actor="owner")
        assert repo.is_halted()
        repo.resume(hid)
        assert not repo.is_halted()


class TestTaxLotRepo:
    def test_open_and_close(self, conn: sqlite3.Connection):
        cid = _seed_candidate(conn)
        _seed_proposal(conn, cid)
        oid = _seed_order(conn)
        repo = TaxLotRepo(conn)
        lid = repo.open_lot(
            order_id=oid,
            ticker="AAPL",
            occ_symbol="AAPL  261218C00200000",
            side="long",
            qty=1,
            open_price="5.20",
        )
        repo.close_lot(lid, close_price="7.00", realized_pnl="1.80")
        lots = repo.open_lots_for_ticker("AAPL", days=30)
        assert len(lots) == 1
        assert lots[0]["realized_pnl"] == "1.80"


class TestSnapshotRepos:
    def test_positions_snapshot(self, conn: sqlite3.Connection):
        pid = PositionsSnapshotRepo(conn).insert(positions_json="[]")
        assert pid

    def test_pnl_snapshot(self, conn: sqlite3.Connection):
        pid = PnlSnapshotRepo(conn).insert(realized="100.00", unrealized="-20.00", total="80.00")
        assert pid
