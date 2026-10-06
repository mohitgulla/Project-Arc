"""Context store: append-only, TTL'd shared memory between agents (D16).

Agents never hand results to each other in memory. A producer writes a typed
entry; a consumer reads a :class:`ContextSnapshot` and records the snapshot id on
its ``routine_runs`` row, so every decision can be traced to exactly what it saw.

Rules enforced here (and by triggers in ``004_routines.sql``):

- Writes are inserts. Nothing is overwritten or deleted.
- Superseding inserts the new row and flips the old row's status to
  ``superseded``; that status flip is the only update the DB allows.
- A snapshot contains only ``active`` entries with ``valid_from <= as_of`` and
  ``expires_at`` unset or ``> as_of``.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 — used at runtime in pydantic models
import enum
import json
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.context.kinds import kind_spec, validate_payload
from arc.context.ttl import Ttl, from_db, require_aware, to_db
from arc.utils.calendar import ET, now_et

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

log = structlog.get_logger(__name__)


class Supersede(enum.StrEnum):
    """What a new entry does to earlier active entries of the same kind + subject."""

    LATEST = "latest"  # newest replaces the previous active entries (e.g. StockedUp)
    ACCUMULATE = "accumulate"  # entries stack until they expire (e.g. macro guidance)


class EntryStatus(enum.StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class ContextEntry(BaseModel):
    """One row of ``context_entries``. ``payload`` is the validated JSON dict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    schema_version: int
    produced_by: str
    run_id: str | None = None
    chain_run_id: str | None = None
    created_at: _dt.datetime
    valid_from: _dt.datetime
    expires_at: _dt.datetime | None = None
    supersedes_id: str | None = None
    status: EntryStatus = EntryStatus.ACTIVE

    def model(self) -> BaseModel:
        """The payload parsed into its kind's pydantic model."""
        return validate_payload(self.kind, self.payload)


class ContextSnapshot(BaseModel):
    """An immutable, recorded view of the context store at ``as_of``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    as_of: _dt.datetime
    kinds: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    entries: list[ContextEntry] = Field(default_factory=list)

    def of_kind(self, kind: str, subject: str | None = None) -> list[ContextEntry]:
        """Entries of *kind* (optionally for one *subject*), oldest first."""
        return [
            e for e in self.entries if e.kind == kind and (subject is None or e.subject == subject)
        ]

    def latest(self, kind: str, subject: str | None = None) -> ContextEntry | None:
        """Most recent entry of *kind* (optionally for one *subject*)."""
        matches = self.of_kind(kind, subject)
        return matches[-1] if matches else None

    def payloads(self, kind: str, subject: str | None = None) -> list[BaseModel]:
        """Typed payload models of *kind*, oldest first."""
        return [e.model() for e in self.of_kind(kind, subject)]

    @property
    def entry_ids(self) -> list[str]:
        return [e.id for e in self.entries]


def _row_to_entry(row: sqlite3.Row) -> ContextEntry:
    return ContextEntry(
        id=row["id"],
        kind=row["kind"],
        subject=row["subject"],
        payload=json.loads(row["payload"]),
        schema_version=row["schema_version"],
        produced_by=row["produced_by"],
        run_id=row["run_id"],
        chain_run_id=row["chain_run_id"],
        created_at=from_db(row["created_at"]),
        valid_from=from_db(row["valid_from"]),
        expires_at=from_db(row["expires_at"]) if row["expires_at"] else None,
        supersedes_id=row["supersedes_id"],
        status=EntryStatus(row["status"]),
    )


class ContextStore:
    """Read/write API over ``context_entries`` and ``context_snapshots``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- writes ------------------------------------------------------------

    def write(
        self,
        *,
        kind: str,
        subject: str,
        payload: BaseModel | Mapping[str, object],
        produced_by: str,
        ttl: Ttl | str | None = None,
        supersede: Supersede | str = Supersede.LATEST,
        run_id: str | None = None,
        chain_run_id: str | None = None,
        valid_from: _dt.datetime | None = None,
        now: _dt.datetime | None = None,
    ) -> ContextEntry:
        """Validate and append one entry; apply the producer's supersede policy.

        *ttl* ``None`` means the entry never expires on its own (it can still be
        superseded).
        """
        spec = kind_spec(kind)
        model = validate_payload(kind, payload)
        if not subject:
            msg = "context entry subject must be non-empty"
            raise ValueError(msg)
        now = require_aware(now or now_et(), "now").astimezone(ET)
        valid_from = require_aware(valid_from or now, "valid_from").astimezone(ET)
        ttl_obj = Ttl.model_validate(ttl) if ttl is not None else None
        expires_at = ttl_obj.expires_at(valid_from) if ttl_obj is not None else None
        policy = Supersede(supersede)

        entry_id = f"ctx-{uuid.uuid4().hex}"
        previous: list[str] = []
        if policy is Supersede.LATEST:
            previous = [
                r["id"]
                for r in self.conn.execute(
                    """SELECT id FROM context_entries
                       WHERE kind = ? AND subject = ? AND status = 'active'
                       ORDER BY valid_from, created_at, rowid""",
                    (kind, subject),
                ).fetchall()
            ]
        entry = ContextEntry(
            id=entry_id,
            kind=kind,
            subject=subject,
            payload=model.model_dump(mode="json"),
            schema_version=spec.schema_version,
            produced_by=produced_by,
            run_id=run_id,
            chain_run_id=chain_run_id,
            created_at=now,
            valid_from=valid_from,
            expires_at=expires_at,
            supersedes_id=previous[-1] if previous else None,
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO context_entries
                   (id, kind, subject, payload, schema_version, produced_by, run_id,
                    chain_run_id, created_at, valid_from, expires_at, supersedes_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (
                    entry.id,
                    entry.kind,
                    entry.subject,
                    json.dumps(entry.payload, sort_keys=True),
                    entry.schema_version,
                    entry.produced_by,
                    entry.run_id,
                    entry.chain_run_id,
                    to_db(entry.created_at),
                    to_db(entry.valid_from),
                    to_db(entry.expires_at) if entry.expires_at else None,
                    entry.supersedes_id,
                ),
            )
            for old_id in previous:
                self.conn.execute(
                    "UPDATE context_entries SET status = 'superseded' "
                    "WHERE id = ? AND status = 'active'",
                    (old_id,),
                )
        log.info(
            "context.write",
            id=entry.id,
            kind=kind,
            subject=subject,
            produced_by=produced_by,
            superseded=len(previous),
            expires_at=entry.expires_at.isoformat() if entry.expires_at else None,
        )
        return entry

    def expire_due(self, now: _dt.datetime | None = None) -> int:
        """Flip active entries whose ``expires_at`` has passed to ``expired``."""
        now = require_aware(now or now_et(), "now")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE context_entries SET status = 'expired' "
                "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
                (to_db(now),),
            )
        return cur.rowcount

    # -- reads -------------------------------------------------------------

    def get(self, entry_id: str) -> ContextEntry | None:
        row = self.conn.execute(
            "SELECT * FROM context_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    def query(
        self,
        *,
        as_of: _dt.datetime,
        kinds: Iterable[str] = (),
        subjects: Iterable[str] = (),
    ) -> list[ContextEntry]:
        """Active, unexpired entries visible at *as_of* (oldest first). Not recorded."""
        kinds = list(kinds)
        subjects = list(subjects)
        for k in kinds:
            kind_spec(k)
        at = to_db(require_aware(as_of, "as_of"))
        sql = [
            "SELECT * FROM context_entries WHERE status = 'active' AND valid_from <= ?",
            "AND (expires_at IS NULL OR expires_at > ?)",
        ]
        params: list[Any] = [at, at]
        if kinds:
            sql.append(f"AND kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if subjects:
            sql.append(f"AND subject IN ({','.join('?' * len(subjects))})")
            params.extend(subjects)
        sql.append("ORDER BY valid_from, created_at, rowid")
        return [_row_to_entry(r) for r in self.conn.execute(" ".join(sql), params).fetchall()]

    def snapshot(
        self,
        as_of: _dt.datetime | None = None,
        *,
        kinds: Iterable[str] = (),
        subjects: Iterable[str] = (),
        run_id: str | None = None,
    ) -> ContextSnapshot:
        """Read and **record** what is visible at *as_of*; returns the snapshot."""
        as_of = require_aware(as_of or now_et(), "as_of").astimezone(ET)
        kinds = list(kinds)
        subjects = list(subjects)
        entries = self.query(as_of=as_of, kinds=kinds, subjects=subjects)
        snap = ContextSnapshot(
            id=f"snap-{uuid.uuid4().hex}",
            as_of=as_of,
            kinds=kinds,
            subjects=subjects,
            entries=entries,
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO context_snapshots
                   (id, as_of, kinds, subjects, entry_ids, run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    snap.id,
                    to_db(as_of),
                    json.dumps(kinds),
                    json.dumps(subjects),
                    json.dumps(snap.entry_ids),
                    run_id,
                    to_db(now_et()),
                ),
            )
        return snap

    def load_snapshot(self, snapshot_id: str) -> ContextSnapshot:
        """Rebuild a recorded snapshot exactly (for audit and backtest replay)."""
        row = self.conn.execute(
            "SELECT * FROM context_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            msg = f"unknown snapshot {snapshot_id!r}"
            raise KeyError(msg)
        ids: list[str] = json.loads(row["entry_ids"])
        entries: list[ContextEntry] = []
        for eid in ids:
            entry = self.get(eid)
            if entry is None:  # pragma: no cover - rows cannot be deleted (trigger)
                msg = f"snapshot {snapshot_id} references missing entry {eid}"
                raise KeyError(msg)
            entries.append(entry)
        return ContextSnapshot(
            id=row["id"],
            as_of=from_db(row["as_of"]),
            kinds=json.loads(row["kinds"]),
            subjects=json.loads(row["subjects"]),
            entries=entries,
        )

    def for_run(self, run_id: str) -> list[ContextEntry]:
        """Everything a run wrote (any status)."""
        rows = self.conn.execute(
            "SELECT * FROM context_entries WHERE run_id = ? ORDER BY created_at, rowid",
            (run_id,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]
