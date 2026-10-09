"""Intraday reconcile after an unknown order state (E11.1, D71).

An execution that ends ``unconfirmed`` (the ladder could not prove its order is
gone: a submit error the client-id lookup could not resolve, or a cancel the
broker never confirmed) queues exactly one ``reconcile.intraday`` routine event
per proposal (:func:`queue_intraday_reconcile`). The dispatcher runs the
``reconcile.intraday`` job for it on its next tick
(:func:`arc.broker.reconcile_job.intraday_reconcile_step`), which calls
:func:`arc.reconcile.engine.reconcile` with ``scope="intraday"``: only the
unconfirmed executions and their orders, no positions, fills, snapshots or
settles, and never a halt from the engine itself. The job then decides: an order
still unresolved raises the ``arc:reconcile`` halt; a resolved one is journaled
``reconcile:resolved`` and trading carries on.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import structlog

from arc.context.ttl import to_db

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

__all__ = ["INTRADAY_EVENT", "queue_intraday_reconcile"]

log = structlog.get_logger(__name__)

INTRADAY_EVENT = "reconcile.intraday"


def queue_intraday_reconcile(
    conn: sqlite3.Connection, proposal_hash: str, *, reason: str, now: _dt.datetime
) -> str | None:
    """Queue one ``reconcile.intraday`` event for *proposal_hash*; the event id, or
    ``None`` when one was already queued for it (idempotent per proposal).

    Never raises: the ladder's outcome is already recorded, and the 16:30
    reconcile still covers the order if this write fails.
    """
    try:
        with conn:
            exists = conn.execute(
                """SELECT 1 FROM routine_events
                   WHERE name = ? AND json_extract(payload, '$.proposal_hash') = ? LIMIT 1""",
                (INTRADAY_EVENT, proposal_hash),
            ).fetchone()
            if exists:
                return None
            event_id = f"evt-{uuid.uuid4().hex[:16]}"
            conn.execute(
                "INSERT INTO routine_events (id, name, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    event_id,
                    INTRADAY_EVENT,
                    json.dumps({"proposal_hash": proposal_hash, "reason": reason[:500]}),
                    to_db(now),
                ),
            )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.error("reconcile.intraday_queue_failed", proposal_hash=proposal_hash, error=str(exc))
        return None
    log.warning("reconcile.intraday_queued", proposal_hash=proposal_hash, event_id=event_id)
    return event_id
