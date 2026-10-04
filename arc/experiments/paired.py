"""Read-time union of the control store and its experiment arm stores (E10.2, D44).

Each runner arm keeps its own store (budget, halts, reconcile, positions), so the
arm's rows carry no ``arm_id``: the store is the tag (``arm_identity``). Readers
that compare arms (the E10.3 evaluator) open the control store through
:func:`paired_view`, which ATTACHes every arm store read-only and creates TEMP
views named like the base tables. SQLite resolves an unqualified name in ``temp``
first, so ``SELECT ... FROM pnl_snapshots WHERE arm_id = 'XP-1:treatment'`` sees
control's rows (``arm_id`` as stored, NULL for control) plus each arm's rows with
``arm_id`` projected from the arm's identity. No row is ever copied into the
control store: mirrored executions would count against control's D32 order
budget and contaminate every direct reader of the control store (Tower, scorecard).

The view connection is read-only (``query_only``); callers write through their
own connection.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.experiments.arms import arm_stores, read_identity

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["PAIRED_TABLES", "attach_arms", "paired_view"]

log = structlog.get_logger(__name__)

# The tables the evaluator filters by ``arm_id`` (E10.3 evaluate.py).
PAIRED_TABLES: tuple[str, ...] = (
    "pnl_snapshots",
    "positions_snapshots",
    "decisions",
    "executions",
    "outcomes",
    "proposals",
    "run_manifests",
)


def _db_file(conn: sqlite3.Connection, schema: str = "main") -> Path | None:
    for r in conn.execute("PRAGMA database_list"):
        if r[1] == schema and r[2]:
            return Path(r[2]).resolve()
    return None


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA "{schema}".table_info("{table}")')]


def _arm_paths(conn: sqlite3.Connection) -> list[Path]:
    return [p for p in arm_stores(conn).values() if p.is_file()]


def attach_arms(conn: sqlite3.Connection, paths: list[Path] | None = None) -> dict[str, str]:
    """ATTACH each arm store on *conn* and shadow :data:`PAIRED_TABLES` with TEMP views.

    Returns ``{schema alias: arm_id}``. *paths* defaults to the arm stores the
    control store recorded at t0. A store without an ``arm_identity`` is skipped.
    """
    attached: dict[str, str] = {}
    for i, path in enumerate(paths if paths is not None else _arm_paths(conn)):
        alias = f"arm{i}"
        conn.execute("ATTACH DATABASE ? AS " + alias, (str(path.resolve()),))
        ident = conn.execute(f"SELECT arm_id FROM {alias}.arm_identity WHERE id = 1").fetchone()  # noqa: S608
        if ident is None:
            conn.execute(f"DETACH DATABASE {alias}")
            log.warning("experiments.attach_skipped", store=str(path), reason="no arm_identity")
            continue
        attached[alias] = str(ident[0])
    if not attached:
        return attached
    for table in PAIRED_TABLES:
        cols = _columns(conn, "main", table)
        if "arm_id" not in cols:
            continue
        # views have no rowid; the evaluator orders by it, so project it as a column
        # (rows are compared within one arm, i.e. within one store, so it stays ordered)
        parts = [f'SELECT rowid AS rowid, {", ".join(cols)} FROM main."{table}"']  # noqa: S608
        for alias, aid in attached.items():
            arm_cols = set(_columns(conn, alias, table))
            sel = ["rowid AS rowid"]
            for c in cols:
                if c == "arm_id":
                    sel.append(f"COALESCE(arm_id, {_lit(aid)}) AS arm_id")
                else:
                    sel.append(c if c in arm_cols else f"NULL AS {c}")
            parts.append(f'SELECT {", ".join(sel)} FROM {alias}."{table}"')  # noqa: S608
        conn.execute(f'CREATE TEMP VIEW "{table}" AS ' + " UNION ALL ".join(parts))
    log.info("experiments.arms_attached", arms=sorted(attached.values()))
    return attached


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@contextlib.contextmanager
def paired_view(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A read-only connection to *conn*'s store with every arm attached.

    Yields *conn* itself when the store is not a file (tests, ``:memory:``), is an
    arm store, or has no arm store recorded: there is nothing to union.
    """
    path = _db_file(conn)
    arms = _arm_paths(conn) if path is not None else []
    if path is None or not arms or read_identity(conn) is not None:
        yield conn
        return
    view = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        view.row_factory = sqlite3.Row
        attach_arms(view, arms)
        view.execute("PRAGMA query_only = ON")
        yield view
    finally:
        view.close()
