"""Persistence for the D26 control panel: ``config_changes`` and ``config_pending``."""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
import json
import secrets
import sqlite3
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from arc.context.ttl import from_db, to_db

__all__ = [
    "ConfigChange",
    "ConfigChangeRepo",
    "PendingChange",
    "PendingRepo",
    "Source",
]

Source = Literal["slack", "cli"]


def _dump(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class ConfigChange(BaseModel):
    """One ``config_changes`` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    key: str
    old: Any = None
    new: Any = None
    is_default: bool = False  # the key went back to its file/env default
    actor: str
    reason: str | None = None
    at: _dt.datetime
    source: Source
    supersedes_id: int | None = None
    status: Literal["applied", "reverted"]
    direction: str
    halted: bool = False
    pending_id: str | None = None


def _change(row: sqlite3.Row) -> ConfigChange:
    return ConfigChange(
        id=row["id"],
        key=row["key"],
        old=_load(row["old"]),
        new=_load(row["new"]),
        is_default=bool(row["is_default"]),
        actor=row["actor"],
        reason=row["reason"],
        at=from_db(row["at"]),
        source=row["source"],
        supersedes_id=row["supersedes_id"],
        status=row["status"],
        direction=row["direction"],
        halted=bool(row["halted"]),
        pending_id=row["pending_id"],
    )


class ConfigChangeRepo:
    """Append-only override log. The effective override of a key is its latest row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(
        self,
        *,
        key: str,
        old: Any,
        new: Any,
        is_default: bool,
        actor: str,
        reason: str | None,
        at: _dt.datetime,
        source: Source,
        status: Literal["applied", "reverted"],
        direction: str,
        supersedes_id: int | None = None,
        halted: bool = False,
        pending_id: str | None = None,
    ) -> ConfigChange:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO config_changes
                   (key, old, new, is_default, actor, reason, at, source, supersedes_id,
                    status, direction, halted, pending_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    _dump(old),
                    None if is_default else _dump(new),
                    int(is_default),
                    actor,
                    reason,
                    to_db(at),
                    source,
                    supersedes_id,
                    status,
                    direction,
                    int(halted),
                    pending_id,
                ),
            )
        got = self.get(int(cur.lastrowid or 0))
        assert got is not None
        return got

    def get(self, change_id: int) -> ConfigChange | None:
        row = self.conn.execute(
            "SELECT * FROM config_changes WHERE id = ?", (change_id,)
        ).fetchone()
        return _change(row) if row else None

    def version(self) -> int:
        """``config_version``: the latest change id (0 = no override ever)."""
        row = self.conn.execute("SELECT MAX(id) FROM config_changes").fetchone()
        return int(row[0] or 0)

    def latest(self, key: str) -> ConfigChange | None:
        row = self.conn.execute(
            "SELECT * FROM config_changes WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()
        return _change(row) if row else None

    def active(self) -> dict[str, ConfigChange]:
        """Latest row per key whose value is an override (not back at the default)."""
        rows = self.conn.execute(
            """SELECT c.* FROM config_changes c
               JOIN (SELECT key, MAX(id) AS id FROM config_changes GROUP BY key) m
                 ON m.id = c.id
               WHERE c.is_default = 0
               ORDER BY c.key"""
        ).fetchall()
        return {r["key"]: _change(r) for r in rows}

    def history(self, key: str | None = None, *, limit: int = 20) -> list[ConfigChange]:
        """Newest first."""
        if key is None:
            rows = self.conn.execute(
                "SELECT * FROM config_changes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM config_changes WHERE key = ? ORDER BY id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [_change(r) for r in rows]


class PendingChange(BaseModel):
    """A riskier-direction change waiting for the owner's confirm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    code: str
    key: str
    old: Any = None
    new: Any = None
    is_default: bool = False
    kind: Literal["set", "revert"]
    supersedes_id: int | None = None
    actor: str
    reason: str | None = None
    source: Source
    created_at: _dt.datetime
    expires_at: _dt.datetime
    base_version: int
    resolved_at: _dt.datetime | None = None
    outcome: Literal["confirmed", "cancelled", "expired"] | None = None
    resolved_by: str | None = None


def _pending(row: sqlite3.Row) -> PendingChange:
    return PendingChange(
        id=row["id"],
        code=row["code"],
        key=row["key"],
        old=_load(row["old"]),
        new=_load(row["new"]),
        is_default=bool(row["is_default"]),
        kind=row["kind"],
        supersedes_id=row["supersedes_id"],
        actor=row["actor"],
        reason=row["reason"],
        source=row["source"],
        created_at=from_db(row["created_at"]),
        expires_at=from_db(row["expires_at"]),
        base_version=row["base_version"],
        resolved_at=from_db(row["resolved_at"]) if row["resolved_at"] else None,
        outcome=row["outcome"],
        resolved_by=row["resolved_by"],
    )


_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


def new_code(n: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


class PendingRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        key: str,
        old: Any,
        new: Any,
        is_default: bool,
        kind: Literal["set", "revert"],
        actor: str,
        reason: str | None,
        source: Source,
        now: _dt.datetime,
        ttl: _dt.timedelta,
        base_version: int,
        supersedes_id: int | None = None,
    ) -> PendingChange:
        pid = f"cfgp-{uuid.uuid4().hex[:12]}"
        for _ in range(5):
            code = new_code()
            try:
                with self.conn:
                    self.conn.execute(
                        """INSERT INTO config_pending
                           (id, code, key, old, new, is_default, kind, supersedes_id, actor,
                            reason, source, created_at, expires_at, base_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            pid,
                            code,
                            key,
                            _dump(old),
                            None if is_default else _dump(new),
                            int(is_default),
                            kind,
                            supersedes_id,
                            actor,
                            reason,
                            source,
                            to_db(now),
                            to_db(now + ttl),
                            base_version,
                        ),
                    )
                break
            except sqlite3.IntegrityError:  # pragma: no cover - code collision, retry
                continue
        got = self.get(pid)
        assert got is not None
        return got

    def get(self, pid: str) -> PendingChange | None:
        row = self.conn.execute("SELECT * FROM config_pending WHERE id = ?", (pid,)).fetchone()
        return _pending(row) if row else None

    def by_code(self, code: str) -> PendingChange | None:
        row = self.conn.execute(
            "SELECT * FROM config_pending WHERE code = ?", (code.strip().upper(),)
        ).fetchone()
        return _pending(row) if row else None

    def open(self) -> list[PendingChange]:
        rows = self.conn.execute(
            "SELECT * FROM config_pending WHERE resolved_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [_pending(r) for r in rows]

    def resolve(
        self,
        pid: str,
        *,
        outcome: Literal["confirmed", "cancelled", "expired"],
        by: str,
        now: _dt.datetime,
    ) -> bool:
        """Resolve once; ``False`` if it was already resolved."""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE config_pending SET resolved_at = ?, outcome = ?, resolved_by = ?
                   WHERE id = ? AND resolved_at IS NULL""",
                (to_db(now), outcome, by, pid),
            )
        return cur.rowcount == 1

    def expire_due(self, now: _dt.datetime) -> list[str]:
        due = [p.id for p in self.open() if p.expires_at <= now]
        return [pid for pid in due if self.resolve(pid, outcome="expired", by="ttl", now=now)]


def set_run_config_version(conn: sqlite3.Connection, run_id: str, version: int | None) -> None:
    """Record the ``config_version`` a routine run executed under."""
    with conn:
        conn.execute(
            "UPDATE routine_runs SET config_version = ? WHERE run_id = ?", (version, run_id)
        )
