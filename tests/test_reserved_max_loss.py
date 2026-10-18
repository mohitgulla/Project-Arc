"""D116 (E3.7): max loss reserved by in-flight opens (``arc.store.execution``).

Structure: SPY bull put, $415 max loss per unit at a -0.85 credit (tests/test_gate.py).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.context.ttl import to_db
from arc.models import Sizing
from arc.store.db import connect
from arc.store.execution import reserved_open_max_loss, reserved_opens
from arc.store.migrate import migrate
from tests.test_gate import NOW, bull_put

if TYPE_CHECKING:
    import sqlite3

EXPIRES = NOW + dt.timedelta(minutes=10)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _proposal(
    c: sqlite3.Connection,
    h: str,
    *,
    contracts: int = 2,
    kind: str = "open",
    request: str | None = "approved",
    expires: dt.datetime = EXPIRES,
    structure_json: str | None = None,
) -> None:
    sj = structure_json if structure_json is not None else bull_put().model_dump_json()
    sz = Sizing(contracts=contracts, notional=D(415 * contracts), pct_equity=0.01)
    c.execute(
        """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)
           VALUES (?, 'SPY', 'bullish', 'momentum', 0.5, ?)""",
        (h, to_db(NOW)),
    )
    c.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, kind)
           VALUES (?, ?, ?, ?, 't', '{}', ?, ?, ?, ?)""",
        (h, h, h, sj, sz.model_dump_json(), to_db(expires), to_db(NOW), kind),
    )
    if request is not None:
        c.execute(
            """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
                   channel, expires_at, created_at)
               VALUES (?, 'SPY', '2026-10-09', '{}', ?, 'log', ?, ?)""",
            (h, request, to_db(expires), to_db(NOW)),
        )


def _execution(
    c: sqlite3.Connection, h: str, status: str, *, contracts: int = 2, filled: int = 0
) -> None:
    c.execute(
        """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo, band_hi,
               max_steps, contracts, filled_qty, started_at)
           VALUES (?, 'open', ?, 'arc2', '-0.85', '-0.80', 4, ?, ?, ?)""",
        (h, status, contracts, filled, to_db(NOW)),
    )


def test_none_conn_and_empty_store_reserve_nothing(conn: sqlite3.Connection) -> None:
    assert reserved_open_max_loss(None, now=NOW) == 0
    assert reserved_open_max_loss(conn, now=NOW) == 0


def test_pending_and_approved_requests_count_full_size(conn: sqlite3.Connection) -> None:
    _proposal(conn, "a", request="pending")
    _proposal(conn, "b", request="approved", contracts=3)
    assert reserved_open_max_loss(conn, now=NOW) == D(415 * 5)
    assert reserved_open_max_loss(conn, now=NOW, exclude="b") == D(830)


def test_requests_need_now_and_an_unexpired_proposal(conn: sqlite3.Connection) -> None:
    _proposal(conn, "a")
    assert reserved_open_max_loss(conn) == 0  # no clock: approvals are not counted
    assert reserved_open_max_loss(conn, now=EXPIRES) == 0  # lapsed approval
    assert reserved_open_max_loss(conn, now=EXPIRES - dt.timedelta(seconds=1)) == D(830)


def test_decided_requests_closes_and_no_request_do_not_count(conn: sqlite3.Connection) -> None:
    for h, req in (("r", "rejected"), ("x", "expired"), ("n", "not_actionable"), ("o", None)):
        _proposal(conn, h, request=req)
    _proposal(conn, "c", kind="close")
    assert reserved_open_max_loss(conn, now=NOW) == 0


def test_working_execution_counts_unfilled_at_the_band_worst(conn: sqlite3.Connection) -> None:
    # band_hi -0.80 vs proposal -0.85: 5c worse -> $420/unit; 3 of 5 unfilled
    _proposal(conn, "w", contracts=5)
    _execution(conn, "w", "working", contracts=5, filled=2)
    _proposal(conn, "u", request=None, expires=NOW - dt.timedelta(hours=1))
    _execution(conn, "u", "unconfirmed")  # no request row, past expiry: still in flight
    assert reserved_open_max_loss(conn, now=NOW) == D(420 * 3) + D(420 * 2)


@pytest.mark.parametrize("status", ["filled", "partially_filled", "cancelled", "rejected"])
def test_resolved_executions_release_the_reserve(conn: sqlite3.Connection, status: str) -> None:
    _proposal(conn, "f")
    _execution(conn, "f", status)
    assert reserved_open_max_loss(conn, now=NOW) == 0


def test_unreadable_or_unbounded_rows_are_skipped(conn: sqlite3.Connection) -> None:
    _proposal(conn, "bad", structure_json="{not json")
    naked = bull_put().model_copy(update={"max_loss": None}).model_dump_json()
    _proposal(conn, "naked", structure_json=naked)
    _proposal(conn, "ok")
    conn.execute("UPDATE proposals SET expires_at = 'garbage' WHERE proposal_hash = 'ok'")
    _proposal(conn, "good")
    assert reserved_open_max_loss(conn, now=NOW) == D(830)


def test_reserved_opens_lists_underlying_and_amount(conn: sqlite3.Connection) -> None:
    """D131 (E28.5): the per-open rows the sum and the cluster cap both read."""
    assert reserved_opens(None, now=NOW) == []
    _proposal(conn, "a", request="pending")
    _proposal(conn, "w", contracts=5)
    _execution(conn, "w", "working", contracts=5, filled=5)  # nothing left to fill
    assert reserved_opens(conn, now=NOW) == [("SPY", D(830))]
    assert reserved_opens(conn, now=NOW, exclude="a") == []
    nolegs = bull_put().model_copy(update={"legs": []}).model_dump_json()
    _proposal(conn, "z", structure_json=nolegs)
    assert sorted(reserved_opens(conn, now=NOW)) == [("", D(830)), ("SPY", D(830))]
