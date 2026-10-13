"""E10.2e: control's reference tables, synced into an arm store before it runs.

The pipeline, the gate and the arm's own jobs read some tables **directly** from
the store (not through a context kind), e.g. the β-cap reads ``betas``, the
scanner's IV rank reads ``iv_daily`` and the earnings guard reads the earnings
calendar's ``raw_docs``. Only control's sources/jobs write them, so an arm store
held none: its gate saw β = 1.0 (a looser β$Δ cap than control's), its scanner had
no IV rank and every stock's next earnings date was unknown.

Option (a) of the card: before the arm's first forked step (and before the arm's own
jobs on each arms tick), :func:`sync_reference_tables` makes each table in
:data:`ARM_REFERENCE_TABLES` match control **as of the chain time** with an
idempotent upsert keyed by the table's natural key. A table's rows are compared on
``(key, version)`` first, so a sync that has nothing to do reads two narrow
indexes, and only the changed rows are fetched in full. Control is only read
(a ``mode=ro`` connection works).

:data:`ARM_OWN_TABLES` lists the tables the same code reads that stay per arm (the
fork-sensitive book and its bookkeeping). ``tests/test_experiment_reference_sync.py``
fails when a new table read in the scanned packages is in neither list.

Audited and deliberately not synced (no reader in the arm's pipeline/gate/jobs):
``market_tape`` (the arm replays control's tape straight from control's store,
:mod:`arc.experiments.tape`), ``channel_briefs`` (the Scout reads the synced
``channel_brief`` context kind), ``options_volume_daily`` (no reader left in
``arc``).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

__all__ = [
    "ARM_OWN_TABLES",
    "ARM_REFERENCE_TABLES",
    "ReferenceTable",
    "sync_reference_tables",
]

log = structlog.get_logger(__name__)

_INGEST_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # raw_docs.ingested_at (arc.ingest.store)


@dataclass(frozen=True)
class ReferenceTable:
    """One control-owned table the arm reads directly.

    *key* is the natural key (the table's primary key), *version* a column every
    write rewrites (``created_at`` on the upserting stores), *scope* the SQL filter
    (with ``:day`` = the chain's ET date and ``:ingested`` = the chain time as
    ``raw_docs.ingested_at`` text) that limits the sync to rows known as of the chain.
    *insert_only* tables never update an existing arm row (``raw_docs``: the Scalp
    bookkeeping columns are per store).
    """

    name: str
    key: tuple[str, ...]
    version: str
    scope: str
    insert_only: bool = False
    reader: str = ""


ARM_REFERENCE_TABLES: tuple[ReferenceTable, ...] = (
    ReferenceTable(
        "betas",
        ("ticker", "day"),
        "created_at",
        "day <= :day",
        reader="arc.betas.store.betas_used (gate β$Δ cap, monitor)",
    ),
    ReferenceTable(
        "iv_daily",
        ("ticker", "day", "source"),
        "created_at",
        "day <= :day",
        reader="arc.iv.store.IvStore (scanner IV rank)",
    ),
    ReferenceTable(
        "iv_skips",
        ("ticker", "day", "source"),
        "created_at",
        "day <= :day",
        reader="arc.iv.store.IvStore.days (kept identical to control's IvStore)",
    ),
    ReferenceTable(
        "raw_docs",
        ("id",),
        "ingested_at",
        "source = 'earnings' AND ingested_at <= :ingested",
        insert_only=True,
        reader="arc.pipeline.market.next_earnings (gate earnings guard)",
    ),
)
"""Control's tables an arm must see as of the chain (see the module doc)."""

ARM_OWN_TABLES: frozenset[str] = frozenset(
    {
        # the arm's own book and its audit trail (fork-sensitive)
        "approval_requests",
        "arm_identity",
        "decisions",
        "executions",
        "fills",
        "gate_decisions",
        "market_contexts",
        "open_structures",
        "order_events",
        "orders",
        "proposals",
        "tax_lots",
        # the arm's own runs and state
        "context_entries",  # synced by kind (arc.experiments.runner.sync_shared_context)
        "context_snapshots",
        "persona_calls",
        "routine_events",
        "routine_runs",
        "routine_state",
        "run_manifests",
        "candidates",  # the rows behind synced ``candidate`` entries (sync_shared_context)
    }
)
"""Tables the scanned code reads that stay per arm (never synced from control)."""


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _bind(as_of: _dt.datetime) -> dict[str, Any]:
    return {
        "day": as_of.astimezone(ET).date().isoformat(),
        "ingested": as_of.astimezone(_dt.UTC).strftime(_INGEST_FMT),
    }


def _reset_scalp(row: dict[str, Any]) -> dict[str, Any]:
    """A copied doc arrives Scalp-unread, except an ingest-time ``filtered`` close (D55)."""
    from arc.ingest.store import FILTERED_STATUS

    if "scalp_status" not in row:
        return row
    filtered = row.get("scalp_status") == FILTERED_STATUS
    return {
        **row,
        "scalped_at": row.get("scalped_at") if filtered else None,
        "scalp_run_id": None,
        "scalp_status": FILTERED_STATUS if filtered else None,
    }


_TRANSFORMS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {"raw_docs": _reset_scalp}


def _sync_table(
    control: sqlite3.Connection, arm: sqlite3.Connection, spec: ReferenceTable, args: dict[str, Any]
) -> int:
    ctl_cols = _cols(control, spec.name)
    arm_cols = set(_cols(arm, spec.name))
    if not ctl_cols or not arm_cols:
        return 0  # an old store without the table: nothing to read or to write
    cols = [c for c in ctl_cols if c in arm_cols]
    probe = [*spec.key, spec.version]
    sel = ", ".join(probe)
    sql = f"SELECT {sel} FROM {spec.name} WHERE {spec.scope}"  # noqa: S608 - fixed specs
    n_key = len(spec.key)
    want = {tuple(r[:n_key]): r[n_key] for r in control.execute(sql, args)}
    have = {tuple(r[:n_key]): r[n_key] for r in arm.execute(sql, args)}
    if spec.insert_only:
        todo = [k for k in want if k not in have]
    else:
        todo = [k for k, v in want.items() if have.get(k, object()) != v]
    if not todo:
        return 0
    transform = _TRANSFORMS.get(spec.name)
    match = " AND ".join(f"{c} = ?" for c in spec.key)
    fetch = f"SELECT {', '.join(cols)} FROM {spec.name} WHERE {match}"  # noqa: S608
    marks = ", ".join("?" * len(cols))
    insert = f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({marks})"  # noqa: S608
    if spec.insert_only:
        insert = insert.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
    else:
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in spec.key)
        insert += f" ON CONFLICT ({', '.join(spec.key)}) DO UPDATE SET {updates}"
    n = 0
    with arm:
        for r in _changed_rows(control, spec, cols, todo, args, fetch):
            row = dict(zip(cols, tuple(r), strict=True))
            if transform is not None:
                row = transform(row)
            n += arm.execute(insert, tuple(row[c] for c in cols)).rowcount
    return n


def _changed_rows(
    control: sqlite3.Connection,
    spec: ReferenceTable,
    cols: list[str],
    todo: list[tuple[Any, ...]],
    args: dict[str, Any],
    fetch: str,
) -> list[tuple[Any, ...]]:
    """Control's full rows for *todo*: point reads for a few, one scoped scan for many
    (the first sync of a fresh arm copies the whole history)."""
    if len(todo) <= 200:
        rows = [control.execute(fetch, key).fetchone() for key in todo]
        out = [tuple(r) for r in rows if r is not None]
    else:
        wanted = set(todo)
        idx = [cols.index(c) for c in spec.key]
        scan = f"SELECT {', '.join(cols)} FROM {spec.name} WHERE {spec.scope}"  # noqa: S608
        out = [tuple(r) for r in control.execute(scan, args) if tuple(r[i] for i in idx) in wanted]
    idx = [cols.index(c) for c in spec.key]
    return sorted(out, key=lambda r: tuple(str(r[i]) for i in idx))


def sync_reference_tables(
    control: sqlite3.Connection, arm: sqlite3.Connection, *, as_of: _dt.datetime
) -> dict[str, int]:
    """Make each :data:`ARM_REFERENCE_TABLES` table in *arm* match *control* as of *as_of*.

    Returns rows written per table (``0`` everywhere on a repeat sync). Never writes
    *control*; rows the arm store holds beyond control's are left alone.
    """
    args = _bind(as_of)
    out = {spec.name: _sync_table(control, arm, spec, args) for spec in ARM_REFERENCE_TABLES}
    if any(out.values()):
        log.info("experiments.reference_synced", as_of=as_of.isoformat(), **out)
    return out
