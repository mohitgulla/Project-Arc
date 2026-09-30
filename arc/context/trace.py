"""Run traces (E5.6 / E8.7d): one serializer for ``arc context trace`` and the tower.

:func:`trace_runs` turns a run id or chain id into one element per run (a chain in
step order): its I/O contract, the context entries it read and wrote, the approval
events it took part in, its persona calls and its latest D27 run manifest. The CLI
prints it (``arc context trace``) and ``GET /api/ops/runs/{run_id}`` serves it, so both
show the same facts.

Everything here is plain SQL over the store: no LLM, broker or network import, so the
read-only tower may use it (the ``arc.tower`` import-linter contract). The manifest is
the stored ``run_manifests.payload`` JSON as written (``RunManifest.model_dump_json``).

The contract check is deterministic: a kind in ``wrote`` that is not in the manifest's
``declared_writes`` (or a read kind outside ``declared_reads``) is listed under
``contract`` so a UI can highlight it. ``None`` declarations mean "undeclared job"
(no contract to check).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

__all__ = ["contract_check", "latest_manifest", "trace_runs"]


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def latest_manifest(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """The latest attempt's stored manifest JSON for *run_id*, or ``None``."""
    if not _has_table(conn, "run_manifests"):
        return None
    row = conn.execute(
        "SELECT payload FROM run_manifests WHERE run_id = ? ORDER BY attempt DESC, rowid DESC"
        " LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _loads(row[0], None)
    return payload if isinstance(payload, dict) else None


def contract_check(
    declared_reads: list[str] | None,
    declared_writes: list[str] | None,
    read_kinds: list[str],
    wrote_kinds: list[str],
) -> dict[str, list[str]]:
    """Kinds read/written outside the declared contract (sorted, de-duplicated)."""
    undeclared_reads = (
        sorted({k for k in read_kinds if k not in declared_reads})
        if declared_reads is not None
        else []
    )
    undeclared_writes = (
        sorted({k for k in wrote_kinds if k not in declared_writes})
        if declared_writes is not None
        else []
    )
    return {"undeclared_reads": undeclared_reads, "undeclared_writes": undeclared_writes}


def _entry(conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, kind, subject, produced_by FROM context_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    return row


def _read_entries(conn: sqlite3.Connection, snapshot_ids: list[str]) -> list[dict[str, Any]]:
    read: dict[str, dict[str, Any]] = {}
    for sid in snapshot_ids:
        snap = conn.execute(
            "SELECT entry_ids FROM context_snapshots WHERE id = ?", (sid,)
        ).fetchone()
        if snap is None:
            continue
        for eid in _loads(snap[0], []):
            if eid in read:
                continue
            e = _entry(conn, eid)
            if e is None:  # pragma: no cover - rows cannot be deleted (trigger)
                continue
            read[eid] = {
                "id": e["id"],
                "kind": e["kind"],
                "subject": e["subject"],
                "produced_by": e["produced_by"],
            }
    return list(read.values())


def _events(conn: sqlite3.Connection, run: sqlite3.Row, cols: set[str]) -> list[dict[str, Any]]:
    """E6.2d: the event this run ran for (created → dispatched → consumed), or the
    events it dispatched (execute)."""
    if not _has_table(conn, "routine_events"):
        return []
    event_id = run["event_id"] if "event_id" in cols else None
    ev_cols = _columns(conn, "routine_events")
    if "dispatched_by" in ev_cols:
        rows = conn.execute(
            "SELECT * FROM routine_events WHERE id = ? OR dispatched_by = ?"
            " ORDER BY created_at, rowid",
            (event_id or "", run["run_id"]),
        ).fetchall()
    else:  # pragma: no cover - pre-018 store
        rows = conn.execute(
            "SELECT * FROM routine_events WHERE id = ? ORDER BY created_at, rowid",
            (event_id or "",),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for e in rows:
        keys = e.keys()
        out.append(
            {
                "id": e["id"],
                "name": e["name"],
                "role": "ran_for" if e["id"] == event_id else "dispatched",
                "created_at": e["created_at"],
                "dispatched_at": e["dispatched_at"] if "dispatched_at" in keys else None,
                "dispatched_by": e["dispatched_by"] if "dispatched_by" in keys else None,
                "consumed_at": e["consumed_at"],
                "consumed_by": _loads(e["consumed_by"], []),
            }
        )
    return out


def _persona_calls(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    if not _has_table(conn, "persona_calls"):
        return []
    cols = _columns(conn, "persona_calls")
    extra = [c for c in ("input_tokens", "output_tokens", "latency_ms", "cost_usd") if c in cols]
    sel = ", ".join(["id", "persona", "model", "status", "prompt_sha256", "created_at", *extra])
    rows = conn.execute(
        f"SELECT {sel} FROM persona_calls WHERE run_id = ? ORDER BY created_at, rowid",  # noqa: S608 - fixed column list
        (run_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for c in rows:
        item = {
            "id": c["id"],
            "persona": c["persona"],
            "model": c["model"],
            "status": c["status"],
            "prompt_sha256": c["prompt_sha256"],
            "created_at": c["created_at"],
        }
        for k in ("input_tokens", "output_tokens", "latency_ms", "cost_usd"):
            item[k] = c[k] if k in extra else None
        out.append(item)
    return out


def trace_runs(conn: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
    """One element per run (a chain in step order): contract, entries read and written,
    events, persona calls and the full run manifest (latest attempt). Raises LookupError.
    """
    runs = conn.execute(
        "SELECT * FROM routine_runs WHERE run_id = ? OR chain_run_id = ?"
        " ORDER BY step_index, scheduled_for, rowid",
        (ref, ref),
    ).fetchall()
    if not runs:
        msg = f"no routine run or chain {ref!r}"
        raise LookupError(msg)
    cols = _columns(conn, "routine_runs")
    out: list[dict[str, Any]] = []
    for run in runs:
        m = latest_manifest(conn, run["run_id"])
        read = _read_entries(conn, _loads(run["inputs_snapshot"], []))
        wrote = []
        for eid in _loads(run["outputs"], []):
            e = _entry(conn, eid)
            if e is not None:
                wrote.append({"id": e["id"], "kind": e["kind"], "subject": e["subject"]})
        declared_reads = m.get("declared_reads") if m else None
        declared_writes = m.get("declared_writes") if m else None
        out.append(
            {
                "job": run["job"],
                "run_id": run["run_id"],
                "chain_run_id": run["chain_run_id"],
                "step_index": run["step_index"],
                "status": run["status"],
                "declared": {"reads": declared_reads, "writes": declared_writes},
                "read": read,
                "wrote": wrote,
                "contract": contract_check(
                    declared_reads,
                    declared_writes,
                    [e["kind"] for e in read],
                    [e["kind"] for e in wrote],
                ),
                "events": _events(conn, run, cols),
                "persona_calls": _persona_calls(conn, run["run_id"]),
                "manifest": m,
            }
        )
    return out
