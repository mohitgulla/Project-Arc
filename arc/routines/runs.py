"""Persistence for the dispatcher: ``routine_runs``, ``routine_events``, ``routine_state``."""

from __future__ import annotations

import datetime as _dt
import enum
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db
from arc.utils.calendar import ET, now_et

if TYPE_CHECKING:
    from collections.abc import Iterable


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class RoutineRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    job: str
    chain_run_id: str | None = None
    step_index: int = 0
    reason: str
    scheduled_for: _dt.datetime
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    status: RunStatus
    attempts: int = 1
    inputs_snapshot: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    summary: str | None = None
    error: str | None = None
    event_id: str | None = None  # E6.2d: the routine_events.id an event-triggered run is for


def _row(row: sqlite3.Row) -> RoutineRun:
    return RoutineRun(
        run_id=row["run_id"],
        job=row["job"],
        chain_run_id=row["chain_run_id"],
        step_index=row["step_index"],
        reason=row["reason"],
        scheduled_for=from_db(row["scheduled_for"]),
        started_at=from_db(row["started_at"]) if row["started_at"] else None,
        finished_at=from_db(row["finished_at"]) if row["finished_at"] else None,
        status=RunStatus(row["status"]),
        attempts=row["attempts"],
        inputs_snapshot=json.loads(row["inputs_snapshot"] or "[]"),
        outputs=json.loads(row["outputs"] or "[]"),
        summary=row["summary"],
        error=row["error"],
        event_id=row["event_id"],
    )


class RoutineRunRepo:
    """``routine_runs``: one row per (job, scheduled slot), or per (job, event) (E6.2d)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def claim(
        self,
        *,
        job: str,
        scheduled_for: _dt.datetime,
        reason: str,
        chain_run_id: str | None = None,
        step_index: int = 0,
        status: RunStatus = RunStatus.RUNNING,
        summary: str | None = None,
        now: _dt.datetime | None = None,
        event_id: str | None = None,
    ) -> RoutineRun | None:
        """Insert the row for ``(job, scheduled_for)``; ``None`` if it already exists.

        The unique key is what makes a duplicate tick a no-op. An event-triggered
        run (``event_id`` set) is unique per ``(job, event_id)`` instead (migration
        018), so two events created in the same second each get their own run.
        """
        now = now or now_et()
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        finished = to_db(now) if status is not RunStatus.RUNNING else None
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO routine_runs
                       (run_id, job, chain_run_id, step_index, reason, scheduled_for,
                        started_at, finished_at, status, summary, event_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        job,
                        chain_run_id,
                        step_index,
                        reason,
                        to_db(scheduled_for),
                        to_db(now),
                        finished,
                        status.value,
                        summary,
                        event_id,
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return self.get(run_id)

    def restart(self, run_id: str, *, now: _dt.datetime | None = None) -> RoutineRun:
        """Re-open a failed row for a resume attempt (same slot, attempts + 1)."""
        now = now or now_et()
        with self.conn:
            cur = self.conn.execute(
                """UPDATE routine_runs
                   SET status = 'running', attempts = attempts + 1, started_at = ?,
                       finished_at = NULL, error = NULL
                   WHERE run_id = ? AND status = 'failed'""",
                (to_db(now), run_id),
            )
        if cur.rowcount != 1:
            msg = f"run {run_id} is not in a failed state"
            raise ValueError(msg)
        run = self.get(run_id)
        assert run is not None
        return run

    def set_config_version(self, run_id: str, version: int | None) -> None:
        """D26: record the ``config_changes`` version a run executes under."""
        if version is None:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE routine_runs SET config_version = ? WHERE run_id = ?", (version, run_id)
            )

    def set_inputs(self, run_id: str, snapshot_ids: Iterable[str]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE routine_runs SET inputs_snapshot = ? WHERE run_id = ?",
                (json.dumps(list(snapshot_ids)), run_id),
            )

    def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        outputs: Iterable[str] = (),
        summary: str | None = None,
        error: str | None = None,
        now: _dt.datetime | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE routine_runs
                   SET status = ?, finished_at = ?, outputs = ?, summary = ?, error = ?
                   WHERE run_id = ?""",
                (
                    status.value,
                    to_db(now or now_et()),
                    json.dumps(list(outputs)),
                    summary,
                    error,
                    run_id,
                ),
            )

    def get(self, run_id: str) -> RoutineRun | None:
        row = self.conn.execute("SELECT * FROM routine_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row(row) if row else None

    def find(self, job: str, scheduled_for: _dt.datetime) -> RoutineRun | None:
        """The scheduled/manual run of *job* for one slot (event-triggered runs excluded)."""
        row = self.conn.execute(
            "SELECT * FROM routine_runs WHERE job = ? AND scheduled_for = ? AND event_id IS NULL",
            (job, to_db(scheduled_for)),
        ).fetchone()
        return _row(row) if row else None

    def for_event(self, job: str, event_id: str) -> RoutineRun | None:
        """The run of *job* for one event (unique since migration 018), if any."""
        row = self.conn.execute(
            "SELECT * FROM routine_runs WHERE job = ? AND event_id = ?", (job, event_id)
        ).fetchone()
        return _row(row) if row else None

    def chain(self, chain_run_id: str) -> list[RoutineRun]:
        rows = self.conn.execute(
            "SELECT * FROM routine_runs WHERE chain_run_id = ? ORDER BY step_index",
            (chain_run_id,),
        ).fetchall()
        return [_row(r) for r in rows]

    def latest_failed_chain(self, root_job: str, day: _dt.date) -> str | None:
        """chain_run_id of *root_job*'s most recent chain on ET *day* with a failed step."""
        start = _dt.datetime.combine(day, _dt.time.min, tzinfo=ET)
        end = start + _dt.timedelta(days=1)
        row = self.conn.execute(
            """SELECT r.chain_run_id FROM routine_runs r
               WHERE r.job = ? AND r.step_index = 0 AND r.chain_run_id IS NOT NULL
                 AND r.scheduled_for >= ? AND r.scheduled_for < ?
                 AND EXISTS (SELECT 1 FROM routine_runs s
                             WHERE s.chain_run_id = r.chain_run_id AND s.status = 'failed')
               ORDER BY r.scheduled_for DESC LIMIT 1""",
            (root_job, to_db(start), to_db(end)),
        ).fetchone()
        return row["chain_run_id"] if row else None

    def history(self, *, job: str | None = None, limit: int = 50) -> list[RoutineRun]:
        if job:
            rows = self.conn.execute(
                """SELECT * FROM routine_runs WHERE job = ?
                   ORDER BY scheduled_for DESC, step_index DESC LIMIT ?""",
                (job, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM routine_runs ORDER BY scheduled_for DESC, step_index DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]


class RoutineEvent(BaseModel):
    """One ``routine_events`` row.

    Lifecycle (E6.2d): ``created`` → optionally ``dispatched`` (the D34 ``execute``
    step claimed it for an Investor subprocess, so the tick's drain never fires
    it) → ``consumed`` (by exactly one run, or by a journaled refusal).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: _dt.datetime
    dispatched_at: _dt.datetime | None = None
    dispatched_by: str | None = None
    consumed_at: _dt.datetime | None = None
    consumed_by: list[str] = Field(default_factory=list)


def _event(r: sqlite3.Row) -> RoutineEvent:
    return RoutineEvent(
        id=r["id"],
        name=r["name"],
        payload=json.loads(r["payload"]),
        created_at=from_db(r["created_at"]),
        dispatched_at=from_db(r["dispatched_at"]) if r["dispatched_at"] else None,
        dispatched_by=r["dispatched_by"],
        consumed_at=from_db(r["consumed_at"]) if r["consumed_at"] else None,
        consumed_by=json.loads(r["consumed_by"] or "[]"),
    )


class RoutineEventRepo:
    """External events (``approval``, ``halt``, ...) queued for the next tick."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def emit(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        now: _dt.datetime | None = None,
    ) -> RoutineEvent:
        ev = RoutineEvent(
            id=f"evt-{uuid.uuid4().hex[:16]}",
            name=name,
            payload=payload or {},
            created_at=now or now_et(),
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO routine_events (id, name, payload, created_at) VALUES (?, ?, ?, ?)",
                (ev.id, ev.name, json.dumps(ev.payload), to_db(ev.created_at)),
            )
        return ev

    def get(self, event_id: str) -> RoutineEvent | None:
        """One event by id, consumed or not (D34: a spawned Investor reads its own)."""
        r = self.conn.execute("SELECT * FROM routine_events WHERE id = ?", (event_id,)).fetchone()
        return _event(r) if r is not None else None

    def pending(self, *, until: _dt.datetime) -> list[RoutineEvent]:
        """Unconsumed events the tick's drain may fire; never a dispatched one (E6.2d)."""
        rows = self.conn.execute(
            """SELECT * FROM routine_events
               WHERE consumed_at IS NULL AND dispatched_at IS NULL AND created_at <= ?
               ORDER BY created_at, rowid""",
            (to_db(until),),
        ).fetchall()
        return [_event(r) for r in rows]

    def dispatch(self, event_id: str, *, by: str, now: _dt.datetime) -> bool:
        """Claim a pending event for one out-of-band run (D34 ``execute``).

        Atomic: ``True`` only for the caller that moved it from pending to
        dispatched. A dispatched event is invisible to :meth:`pending`.
        """
        with self.conn:
            cur = self.conn.execute(
                """UPDATE routine_events SET dispatched_at = ?, dispatched_by = ?
                   WHERE id = ? AND dispatched_at IS NULL AND consumed_at IS NULL""",
                (to_db(now), by, event_id),
            )
        return cur.rowcount == 1

    def release(self, event_id: str) -> None:
        """Undo :meth:`dispatch` (the spawn failed): back to the tick's drain."""
        with self.conn:
            self.conn.execute(
                """UPDATE routine_events SET dispatched_at = NULL, dispatched_by = NULL
                   WHERE id = ? AND consumed_at IS NULL""",
                (event_id,),
            )

    def consume(self, event_id: str, run_ids: Iterable[str], *, now: _dt.datetime) -> bool:
        """Mark consumed once; ``False`` if it already was (the first consumer wins)."""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE routine_events SET consumed_at = ?, consumed_by = ?
                   WHERE id = ? AND consumed_at IS NULL""",
                (to_db(now), json.dumps(list(run_ids)), event_id),
            )
        return cur.rowcount == 1


class RoutineStateRepo:
    """Small key/value store for dispatcher cursors and heartbeat state."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM routine_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str, *, now: _dt.datetime | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (key, value, to_db(now or now_et())),
            )

    def delete(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM routine_state WHERE key = ?", (key,))

    def get_time(self, key: str) -> _dt.datetime | None:
        val = self.get(key)
        return from_db(val) if val else None

    def set_time(self, key: str, value: _dt.datetime) -> None:
        self.set(key, to_db(value))
