"""``heartbeats`` and ``ops_alerts`` (migration 010, E8.2)."""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations at runtime
import json
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db

if TYPE_CHECKING:
    import sqlite3

HeartbeatStatus = Literal["ok", "degraded", "failed"]


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    component: str
    status: HeartbeatStatus
    at: _dt.datetime
    correlation: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)


class OpsAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    key: str
    kind: str
    message: str
    opened_at: _dt.datetime
    resolved_at: _dt.datetime | None = None
    posted_ts: str | None = None
    correlation: dict[str, Any] = Field(default_factory=dict)


def _hb(row: sqlite3.Row) -> Heartbeat:
    return Heartbeat(
        id=row["id"],
        component=row["component"],
        status=row["status"],
        at=from_db(row["at"]),
        correlation=json.loads(row["correlation"]),
        detail=json.loads(row["detail"]),
    )


def _alert(row: sqlite3.Row) -> OpsAlert:
    return OpsAlert(
        id=row["id"],
        key=row["key"],
        kind=row["kind"],
        message=row["message"],
        opened_at=from_db(row["opened_at"]),
        resolved_at=from_db(row["resolved_at"]) if row["resolved_at"] else None,
        posted_ts=row["posted_ts"],
        correlation=json.loads(row["correlation"]),
    )


class HeartbeatRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        component: str,
        status: HeartbeatStatus,
        *,
        at: _dt.datetime,
        correlation: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Heartbeat:
        hb = Heartbeat(
            id=f"hb-{uuid.uuid4().hex[:16]}",
            component=component,
            status=status,
            at=at,
            correlation=correlation or {},
            detail=detail or {},
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO heartbeats (id, component, status, at, correlation, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    hb.id,
                    component,
                    status,
                    to_db(at),
                    json.dumps(hb.correlation, sort_keys=True, default=str),
                    json.dumps(hb.detail, sort_keys=True, default=str),
                ),
            )
        return hb

    def latest(self, component: str) -> Heartbeat | None:
        row = self.conn.execute(
            "SELECT * FROM heartbeats WHERE component = ? ORDER BY at DESC, rowid DESC LIMIT 1",
            (component,),
        ).fetchone()
        return _hb(row) if row else None

    def first(self, component: str) -> Heartbeat | None:
        row = self.conn.execute(
            "SELECT * FROM heartbeats WHERE component = ? ORDER BY at, rowid LIMIT 1",
            (component,),
        ).fetchone()
        return _hb(row) if row else None

    def recent(self, *, component: str | None = None, limit: int = 20) -> list[Heartbeat]:
        if component:
            rows = self.conn.execute(
                "SELECT * FROM heartbeats WHERE component = ? ORDER BY at DESC LIMIT ?",
                (component, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM heartbeats ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_hb(r) for r in rows]

    def find(self, needle: str, *, limit: int = 50) -> list[Heartbeat]:
        """Heartbeats whose correlation or detail mentions *needle* (an id)."""
        like = f"%{needle}%"
        rows = self.conn.execute(
            """SELECT * FROM heartbeats WHERE id = ? OR correlation LIKE ? OR detail LIKE ?
               ORDER BY at DESC LIMIT ?""",
            (needle, like, like, limit),
        ).fetchall()
        return [_hb(r) for r in rows]


class AlertRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def seen(self, key: str) -> bool:
        """True if any alert (open or resolved) was ever recorded for *key*."""
        row = self.conn.execute("SELECT 1 FROM ops_alerts WHERE key = ? LIMIT 1", (key,)).fetchone()
        return row is not None

    def open_for(self, key: str) -> OpsAlert | None:
        row = self.conn.execute(
            "SELECT * FROM ops_alerts WHERE key = ? AND resolved_at IS NULL", (key,)
        ).fetchone()
        return _alert(row) if row else None

    def open_alerts(self) -> list[OpsAlert]:
        rows = self.conn.execute(
            "SELECT * FROM ops_alerts WHERE resolved_at IS NULL ORDER BY opened_at"
        ).fetchall()
        return [_alert(r) for r in rows]

    def open(
        self,
        key: str,
        kind: str,
        message: str,
        *,
        at: _dt.datetime,
        correlation: dict[str, Any] | None = None,
        resolved: bool = False,
    ) -> OpsAlert:
        """Record an alert. ``resolved=True`` records a one-off event (e.g. a missed slot)."""
        alert = OpsAlert(
            id=f"alert-{uuid.uuid4().hex[:12]}",
            key=key,
            kind=kind,
            message=message,
            opened_at=at,
            resolved_at=at if resolved else None,
            correlation=correlation or {},
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO ops_alerts
                   (id, key, kind, message, opened_at, resolved_at, correlation)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert.id,
                    key,
                    kind,
                    message,
                    to_db(at),
                    to_db(at) if resolved else None,
                    json.dumps(alert.correlation, sort_keys=True, default=str),
                ),
            )
        return alert

    def resolve(self, key: str, *, at: _dt.datetime) -> OpsAlert | None:
        current = self.open_for(key)
        if current is None:
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE ops_alerts SET resolved_at = ? WHERE id = ?", (to_db(at), current.id)
            )
        return current

    def set_posted(self, alert_ids: list[str], ts: str) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE ops_alerts SET posted_ts = ? WHERE id = ?", [(ts, i) for i in alert_ids]
            )

    def find(self, needle: str, *, limit: int = 50) -> list[OpsAlert]:
        like = f"%{needle}%"
        rows = self.conn.execute(
            """SELECT * FROM ops_alerts
               WHERE id = ? OR key LIKE ? OR message LIKE ? OR correlation LIKE ?
                  OR posted_ts = ?
               ORDER BY opened_at DESC LIMIT ?""",
            (needle, like, like, like, needle, limit),
        ).fetchall()
        return [_alert(r) for r in rows]
