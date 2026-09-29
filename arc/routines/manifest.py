"""Run manifests (E5.6, D27): every routine run is reconstructable from the DB alone.

The dispatcher writes one append-only :class:`RunManifest` per run attempt, for
every status (``ok``, ``failed``, ``skipped``), after the run's row is final.
Handlers never write it, so no job can forget one. Handlers add only the
external data they used, through :meth:`JobContext.record_input`; everything
else is derived here from the run row, the context store, ``persona_calls`` /
``scout_batches``, and the journal/proposal/gate tables.

Rules:

- **No secret values.** The environment section holds names and flags only
  (``arc_env``, ``halted``, ``auto_approve``, git sha, config file hashes).
- **New run-level metadata goes here.** A later card that adds a fact (e.g. an
  E8.5 override id) adds a field and bumps :data:`MANIFEST_SCHEMA_VERSION`; it
  does not add a side column elsewhere.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic fields
import functools
import hashlib
import importlib.metadata
import json
import platform
import socket
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.context.kinds import KINDS
from arc.context.store import ContextStore
from arc.context.ttl import to_db
from arc.utils.calendar import ET, now_et, session_phase

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from arc.config import ArcSettings
    from arc.routines.config import JobKind, RoutinesConfig, StepSpec
    from arc.routines.runs import RoutineRun

MANIFEST_SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"
_ERROR_MAX = 2000


def _jsonable(obj: object) -> object:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [_jsonable(v) for v in obj]
        return sorted(items, key=repr) if isinstance(obj, (set, frozenset)) else items
    return obj


def digest(payload: object) -> str:
    """sha256 of *payload*'s canonical JSON (sorted keys, no whitespace)."""
    text = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class ExternalInput(BaseModel):
    """Market/broker/DB data a handler used (only its digest is kept)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str  # e.g. "chain:SPY", "account", "bars:SPY", "raw_docs"
    source: str  # e.g. "alpaca", "fixture", "db"
    as_of: _dt.datetime | None = None
    digest: str | None = None  # sha256 of the canonical JSON the handler used
    count: int | None = None


class RunManifest(BaseModel):
    """Everything attributable about one run attempt (D27)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = MANIFEST_SCHEMA_VERSION
    # identity + trigger
    run_id: str
    job: str
    job_kind: Literal["source", "persona"]
    chain_run_id: str | None
    step_index: int
    attempt: int
    reason: str  # schedule | event:<name> | manual | chain:<root> | ...
    trigger_event_id: str | None = None  # routine_events.id when event-driven
    parent_run_id: str | None = None  # run whose completion started this one
    # timing: logical (the tick's clock) and wall clock, tz-aware ET
    scheduled_for: _dt.datetime
    tick_now: _dt.datetime
    started_at: _dt.datetime
    finished_at: _dt.datetime
    duration_ms: int
    market_session: Literal["pre", "open", "post", "closed"]
    trading_day: _dt.date
    # outcome
    status: Literal["ok", "failed", "skipped"]
    summary: str | None = None
    error_class: str | None = None
    error: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    # environment: names and flags only, never secret values
    arc_env: str | None = None
    account_profile: str | None = None
    halted: bool | None = None
    auto_approve: bool | None = None
    arc_version: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    python: str
    host: str
    correlation: dict[str, str] = Field(default_factory=dict)  # tick/cron/kanban/session ids
    # config provenance
    config_hashes: dict[str, str]
    config_version: str | None = None  # E8.5 override version once that card lands
    effective_spec: dict[str, Any]
    # contract
    declared_reads: list[str] | None
    declared_writes: list[str] | None
    kind_schema_versions: dict[str, int]
    # inputs
    snapshot_ids: list[str]
    input_counts: dict[str, int]
    input_digest: str
    external_inputs: list[ExternalInput] = Field(default_factory=list)
    # outputs
    output_ids: dict[str, list[str]]
    dropped: dict[str, int] = Field(default_factory=dict)
    # LLM usage (derived from persona_calls / scout_batches of this run)
    persona_call_ids: list[str] = Field(default_factory=list)
    scout_batch_ids: list[str] = Field(default_factory=list)
    models_requested: list[str] = Field(default_factory=list)
    models_served: list[str] = Field(default_factory=list)
    prompt_sha256: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    llm_latency_ms: int | None = None
    cost_usd: float | None = None
    # downstream links
    decision_ids: list[str] = Field(default_factory=list)
    proposal_hashes: list[str] = Field(default_factory=list)
    gate_decision_ids: list[str] = Field(default_factory=list)
    notifications: list[str] = Field(default_factory=list)  # Slack ts of posts for this run


# ---------------------------------------------------------------------------
# Environment / config provenance (cached per process: a tick is one process)
# ---------------------------------------------------------------------------


@functools.cache
def _git() -> tuple[str | None, bool | None]:
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],  # noqa: S607 - git on PATH
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout

    sha = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=no")
    return (sha.strip() or None) if sha else None, (
        bool(status.strip()) if status is not None else None
    )


@functools.cache
def _arc_version() -> str:
    try:
        return importlib.metadata.version("arc")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - always installed
        return "unknown"


def config_hashes(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    """sha256 of every file under ``config/`` (relative path -> hex digest)."""
    out: dict[str, str] = {}
    if not config_dir.is_dir():  # pragma: no cover - repo always ships config/
        return out
    for p in sorted(config_dir.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            out[p.relative_to(config_dir).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Derivation from the DB
# ---------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, *args: object) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, args).fetchall())
    except Exception:  # noqa: BLE001 - a missing optional table must not sink the manifest
        return []


def _sum(values: Iterable[int | float | None]) -> Any:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _models_requested(personas: Iterable[str], settings: ArcSettings | None) -> list[str]:
    from arc.llm_routing import resolve

    out: list[str] = []
    for p in sorted(set(personas)):
        try:
            out.append(resolve(p, settings).model)
        except Exception:  # noqa: BLE001, S112 - unknown persona or routing file: skip it
            continue
    return out


def build_manifest(
    conn: sqlite3.Connection,
    run: RoutineRun,
    *,
    kind: JobKind,
    spec: StepSpec,
    routines: RoutinesConfig,
    tick_now: _dt.datetime,
    started_at: _dt.datetime,
    finished_at: _dt.datetime,
    duration_ms: int,
    exc: BaseException | None = None,
    metrics: dict[str, Any] | None = None,
    external_inputs: Iterable[ExternalInput] = (),
    event_id: str | None = None,
    parent_run_id: str | None = None,
    notifications: Iterable[str] = (),
    settings: ArcSettings | None = None,
    halted: bool | None = None,
    correlation: dict[str, str] | None = None,
) -> RunManifest:
    """Assemble the manifest for *run* (its row must already be final)."""
    store = ContextStore(conn)
    # inputs
    input_counts: Counter[str] = Counter()
    input_ids: set[str] = set()
    for sid in run.inputs_snapshot:
        try:
            snap = store.load_snapshot(sid)
        except KeyError:
            continue
        for e in snap.entries:
            if e.id not in input_ids:
                input_ids.add(e.id)
                input_counts[e.kind] += 1
    input_digest = hashlib.sha256("\n".join(sorted(input_ids)).encode()).hexdigest()
    # outputs
    output_ids: dict[str, list[str]] = {}
    for eid in run.outputs:
        entry = store.get(eid)
        if entry is not None:
            output_ids.setdefault(entry.kind, []).append(eid)
    # contract
    contract_kinds = set(spec.reads if spec.reads is not None else KINDS) | set(spec.writes or [])
    contract_kinds |= set(input_counts) | set(output_ids)
    kind_versions = {k: KINDS[k].schema_version for k in sorted(contract_kinds) if k in KINDS}
    # LLM usage
    calls = _rows(conn, "SELECT * FROM persona_calls WHERE run_id = ? ORDER BY rowid", run.run_id)
    batches = _rows(conn, "SELECT * FROM scout_batches WHERE run_id = ? ORDER BY rowid", run.run_id)
    dropped: Counter[str] = Counter()
    for c in calls:
        dropped.update(json.loads(c["dropped"] or "{}"))
    for b in batches:
        dropped.update(json.loads(b["rejected"] or "{}"))
    keys = set(calls[0].keys()) if calls else set()

    def col(name: str) -> list[Any]:
        return [c[name] for c in calls] if name in keys else []

    personas = [c["persona"] for c in calls] + (["scout"] if batches else [])
    served = sorted({*(c["model"] for c in calls), *(b["model"] for b in batches)})
    cost = _sum(col("cost_usd"))
    # downstream links
    decisions = _rows(conn, "SELECT id FROM decisions WHERE run_id = ? ORDER BY rowid", run.run_id)
    proposals = _rows(
        conn, "SELECT proposal_hash FROM proposals WHERE run_id = ? ORDER BY rowid", run.run_id
    )
    gates = _rows(conn, "SELECT id FROM gate_decisions WHERE run_id = ? ORDER BY rowid", run.run_id)
    git_sha, git_dirty = _git()
    error = run.error
    status = run.status.value
    return RunManifest(
        run_id=run.run_id,
        job=run.job,
        job_kind=kind.value,  # type: ignore[arg-type]
        chain_run_id=run.chain_run_id,
        step_index=run.step_index,
        attempt=run.attempts,
        reason=run.reason,
        trigger_event_id=event_id,
        parent_run_id=parent_run_id,
        scheduled_for=run.scheduled_for.astimezone(ET),
        tick_now=tick_now.astimezone(ET),
        started_at=started_at.astimezone(ET),
        finished_at=finished_at.astimezone(ET),
        duration_ms=max(0, duration_ms),
        market_session=session_phase(tick_now),  # type: ignore[arg-type]
        trading_day=tick_now.astimezone(ET).date(),
        status=status,  # type: ignore[arg-type]
        summary=run.summary,
        error_class=type(exc).__name__ if exc is not None and status == "failed" else None,
        error=error[:_ERROR_MAX] if error else None,
        metrics=json.loads(json.dumps(metrics or {}, default=str)),
        arc_env=str(settings.env.value) if settings is not None else None,
        account_profile=_opt_str(settings, "account_profile"),
        halted=halted,
        auto_approve=bool(settings.auto_approve) if settings is not None else None,
        arc_version=_arc_version(),
        git_sha=git_sha,
        git_dirty=git_dirty,
        python=platform.python_version(),
        host=socket.gethostname(),
        correlation=dict(correlation or {}),
        config_hashes=config_hashes(),
        config_version=_opt_str(settings, "config_version"),
        effective_spec=spec.model_dump(mode="json", exclude_none=True),
        declared_reads=list(spec.reads) if spec.reads is not None else None,
        declared_writes=list(spec.writes) if spec.writes is not None else None,
        kind_schema_versions=kind_versions,
        snapshot_ids=list(run.inputs_snapshot),
        input_counts=dict(sorted(input_counts.items())),
        input_digest=input_digest,
        external_inputs=list(external_inputs),
        output_ids=output_ids,
        dropped=dict(sorted(dropped.items())),
        persona_call_ids=[c["id"] for c in calls],
        scout_batch_ids=[b["id"] for b in batches],
        models_requested=_models_requested(personas, settings),
        models_served=served,
        prompt_sha256=[
            *(c["prompt_sha256"] for c in calls),
            *(b["prompt_sha256"] for b in batches),
        ],
        input_tokens=_sum(col("input_tokens")),
        output_tokens=_sum(col("output_tokens")),
        llm_latency_ms=_sum(col("latency_ms")),
        cost_usd=float(cost) if cost is not None else None,
        decision_ids=[r["id"] for r in decisions],
        proposal_hashes=[r["proposal_hash"] for r in proposals],
        gate_decision_ids=[r["id"] for r in gates],
        notifications=[n for n in notifications if n],
    )


def _opt_str(settings: ArcSettings | None, name: str) -> str | None:
    value = getattr(settings, name, None) if settings is not None else None
    if value is None:
        return None
    return str(getattr(value, "value", value))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ManifestRepo:
    """``run_manifests``: append-only, one row per run attempt."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, manifest: RunManifest, *, now: _dt.datetime | None = None) -> str:
        row_id = f"rm-{uuid.uuid4().hex[:16]}"
        with self.conn:
            self.conn.execute(
                """INSERT INTO run_manifests
                   (id, run_id, attempt, job, chain_run_id, status, schema_version, payload,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    manifest.run_id,
                    manifest.attempt,
                    manifest.job,
                    manifest.chain_run_id,
                    manifest.status,
                    manifest.schema_version,
                    manifest.model_dump_json(),
                    to_db(now or now_et()),
                ),
            )
        return row_id

    def for_run(self, run_id: str) -> list[RunManifest]:
        """Every attempt's manifest for *run_id*, oldest first."""
        rows = self.conn.execute(
            "SELECT payload FROM run_manifests WHERE run_id = ? ORDER BY attempt, rowid", (run_id,)
        ).fetchall()
        return [RunManifest.model_validate_json(r["payload"]) for r in rows]

    def latest(self, run_id: str) -> RunManifest | None:
        found = self.for_run(run_id)
        return found[-1] if found else None
