"""``arc store reset`` (E19.1, D80): archive the audit store, build a clean one.

The append-only triggers (decisions, outcomes, heartbeats, config_changes, ...)
forbid deleting history in place, so a reset never edits the live store. It:

1. **Refuses** unless trading is halted (scope ``all``), no routine/arms tick
   holds a lock under ``data/locks/`` and no experiment is ``running``.
2. **Archives** the store and every ``arc-exp-*.db`` arm store with SQLite's
   online backup into ``<out>/<YYYYMMDD-HHMM>-pre-d80/`` (journal mode DELETE,
   chmod 0444) plus ``MANIFEST.json`` (row counts per table, sha256, git sha).
3. **Builds** ``<db>.new`` at head migration (stamped with a fresh
   ``store_identity``), copies the keep list of ``config/store_reset.yaml``
   (market/reference tables, context kinds, ``routine_state`` prefixes) from the
   archive, re-asserts the halt, re-applies every ``applied`` runtime override
   through :class:`arc.control.service.ControlService` and writes the
   ``store:epoch`` provenance marker.
4. **Verifies** kept tables match the archive (row count + content hash) and the
   effective config is unchanged, then swaps the new file in with ``os.replace``.

:func:`plan_reset` is the read-only dry run; :func:`apply_reset` does it.
Everything else (orders, fills, positions, P&L, proposals, decisions, outcomes,
runs, heartbeats, halts, approvals, experiments, arm tables) starts empty.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import to_db

if TYPE_CHECKING:
    import datetime as _dt

    from arc.config import ArcSettings

__all__ = [
    "DEFAULT_KEEP_PATH",
    "EPOCH_KEY",
    "CarryOver",
    "Precheck",
    "ResetError",
    "ResetKeepList",
    "ResetPlan",
    "ResetResult",
    "TablePlan",
    "apply_reset",
    "load_keep_list",
    "plan_reset",
    "table_hash",
]

log = structlog.get_logger(__name__)

DEFAULT_KEEP_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "store_reset.yaml"
EPOCH_KEY = "store:epoch"
ARCHIVE_SUFFIX = "pre-d80"
ARM_STORE_GLOB = "arc-exp-*.db"
# Tables the new store writes itself (never copied, never "dropped" in the report).
_META_TABLES = frozenset({"schema_version", "sqlite_sequence", "store_identity"})
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class ResetError(RuntimeError):
    """The reset refused to run or failed a verification (nothing was swapped)."""


class ResetKeepList(BaseModel):
    """``config/store_reset.yaml``: what survives a reset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tables: list[str] = Field(min_length=1)
    context_kinds: list[str] = Field(default_factory=list)
    routine_state_prefixes: list[str] = Field(default_factory=list)


def load_keep_list(path: Path | str | None = None) -> ResetKeepList:
    p = Path(path) if path is not None else DEFAULT_KEEP_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return ResetKeepList.model_validate(data)


Mode = Literal["copy", "context", "state", "carry", "drop", "meta"]


class TablePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    mode: Mode
    rows: int  # rows in the old store
    kept: int  # rows the new store starts with (copied / filtered)


class CarryOver(BaseModel):
    """One runtime override to re-apply (the latest ``applied`` row of its key)."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: Any
    change_id: int
    actor: str
    at: str


class Precheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal["halted", "locks", "experiments", "schema"]
    ok: bool
    detail: str


class ResetPlan(BaseModel):
    """The dry run: what a reset would keep, drop and carry (nothing written)."""

    db: str
    schema_version: int
    head_version: int
    tables: list[TablePlan]
    carry: list[CarryOver]
    checks: list[Precheck]
    arm_stores: list[str]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def lines(self) -> list[str]:
        out = [f"store reset plan: {self.db} (schema {self.schema_version}/{self.head_version})"]
        out.append(f"{'table':<32} {'mode':<8} {'rows':>9} {'kept':>9}")
        for t in self.tables:
            out.append(f"{t.table:<32} {t.mode:<8} {t.rows:>9} {t.kept:>9}")
        out.append(f"config carry-over ({len(self.carry)}):")
        for c in self.carry:
            out.append(f"  {c.key} = {json.dumps(c.value)}  (#{c.change_id} by {c.actor})")
        out.append("arm stores to archive: " + (", ".join(self.arm_stores) or "none"))
        out.append("prechecks:")
        for chk in self.checks:
            out.append(f"  [{'ok' if chk.ok else 'FAIL'}] {chk.name}: {chk.detail}")
        return out


class ResetResult(BaseModel):
    """What :func:`apply_reset` did."""

    db: str
    archive_dir: str
    archive_db: str
    archive_sha256: str
    manifest: str
    arm_archives: list[str]
    tables: list[TablePlan]
    carried: list[dict[str, Any]]
    settings_diff: dict[str, list[Any]]
    routines_diff: dict[str, list[Any]]
    epoch: dict[str, Any]

    def lines(self) -> list[str]:
        out = [
            f"store reset applied: {self.db}",
            f"archive: {self.archive_db} (sha256 {self.archive_sha256[:16]}…)",
            f"manifest: {self.manifest}",
        ]
        out += [f"arm store archived: {a}" for a in self.arm_archives]
        out.append(f"{'table':<32} {'mode':<8} {'rows':>9} {'kept':>9}")
        for t in self.tables:
            if t.mode != "drop" or t.rows:
                out.append(f"{t.table:<32} {t.mode:<8} {t.rows:>9} {t.kept:>9}")
        out.append("config carried over:")
        for c in self.carried:
            new = json.dumps(c["new"])
            out.append(f"  {c['key']}: {c['outcome']} #{c.get('change_id')} = {new}")
        out.append(f"effective settings diff (must be empty): {self.settings_diff}")
        out.append(f"effective routines diff (must be empty): {self.routines_diff}")
        out.append(f"{EPOCH_KEY}: {json.dumps(self.epoch)}")
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        msg = f"audit store not found: {path}"
        raise ResetError(msg)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tables(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' ORDER BY name"  # noqa: S608
        )
    ]


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _count(
    conn: sqlite3.Connection, table: str, where: str = "", args: tuple[Any, ...] = ()
) -> int:
    sql = f'SELECT COUNT(*) FROM "{table}"' + (f" WHERE {where}" if where else "")  # noqa: S608
    return int(conn.execute(sql, args).fetchone()[0])


def _in(values: list[str]) -> str:
    return ",".join("?" * len(values)) or "NULL"


def _context_where(keep: ResetKeepList) -> tuple[str, tuple[Any, ...]]:
    return f"kind IN ({_in(keep.context_kinds)})", tuple(keep.context_kinds)


def _state_where(keep: ResetKeepList) -> tuple[str, tuple[Any, ...]]:
    if not keep.routine_state_prefixes:
        return "0", ()
    parts = " OR ".join("substr(key, 1, ?) = ?" for _ in keep.routine_state_prefixes)
    args: list[Any] = []
    for p in keep.routine_state_prefixes:
        args += [len(p), p]
    return f"({parts})", tuple(args)


def table_hash(
    conn: sqlite3.Connection,
    table: str,
    *,
    columns: list[str] | None = None,
    where: str = "",
    args: tuple[Any, ...] = (),
    schema: str = "main",
) -> str:
    """sha256 over every row of *table* (selected *columns*, ``rowid`` order)."""
    cols = columns or _columns(conn, table, schema)
    sel = ", ".join(f'"{c}"' for c in cols)
    sql = f'SELECT {sel} FROM {schema}."{table}"' + (f" WHERE {where}" if where else "")  # noqa: S608
    h = hashlib.sha256()
    for row in conn.execute(sql + " ORDER BY rowid", args):
        h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _head_version() -> int:
    from arc.store.migrate import MIGRATIONS_DIR

    versions = []
    for f in MIGRATIONS_DIR.glob("*.sql"):
        with contextlib.suppress(ValueError):
            versions.append(int(f.stem.split("_", 1)[0]))
    return max(versions, default=0)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _held_locks(lock_dir: Path) -> list[str]:
    """Lock files under *lock_dir* another process holds right now (``flock`` probe)."""
    if not lock_dir.is_dir():
        return []
    held: list[str] = []
    for p in sorted(lock_dir.rglob("*.lock")):
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(str(p.relative_to(lock_dir)))
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held


def _carry_list(conn: sqlite3.Connection) -> list[CarryOver]:
    """The latest row per key, when it is an ``applied`` override (not a default)."""
    try:
        rows = conn.execute(
            """SELECT c.* FROM config_changes c
               JOIN (SELECT key, MAX(id) AS id FROM config_changes GROUP BY key) m
                 ON m.id = c.id
               WHERE c.status = 'applied' AND c.is_default = 0
               ORDER BY c.id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        CarryOver(
            key=r["key"],
            value=json.loads(r["new"]) if r["new"] is not None else None,
            change_id=int(r["id"]),
            actor=r["actor"],
            at=r["at"],
        )
        for r in rows
    ]


def _prechecks(conn: sqlite3.Connection, lock_dir: Path, head: int, version: int) -> list[Precheck]:
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    checks: list[Precheck] = []
    state = HaltSwitch(HaltRepo(conn)).state()
    if state.error:
        checks.append(Precheck(name="halted", ok=False, detail=f"halts unreadable: {state.error}"))
    else:
        checks.append(
            Precheck(
                name="halted",
                ok=state.halted,
                detail=(
                    f"halted ({len(state.active)} active halt(s))"
                    if state.halted
                    else "trading is not halted (run `arc halt` first; an opens-only halt "
                    "is not enough)"
                ),
            )
        )
    held = _held_locks(lock_dir)
    checks.append(
        Precheck(
            name="locks",
            ok=not held,
            detail=(
                f"held under {lock_dir}: {', '.join(held)}" if held else f"none held ({lock_dir})"
            ),
        )
    )
    running: list[str] = []
    try:
        running = [
            r[0]
            for r in conn.execute(
                """SELECT e.experiment_id FROM experiment_events e
                   JOIN (SELECT experiment_id, MAX(id) AS id FROM experiment_events
                         GROUP BY experiment_id) m ON m.id = e.id
                   WHERE e.status = 'running' ORDER BY e.experiment_id"""
            )
        ]
    except sqlite3.OperationalError:
        running = []
    checks.append(
        Precheck(
            name="experiments",
            ok=not running,
            detail=f"running: {', '.join(running)}" if running else "none running",
        )
    )
    checks.append(
        Precheck(
            name="schema",
            ok=version == head,
            detail=(
                f"at head migration {head}"
                if version == head
                else f"store at {version}, code at {head}: open it once with this code first"
            ),
        )
    )
    return checks


def _table_plans(
    conn: sqlite3.Connection, keep: ResetKeepList, new_tables: set[str]
) -> list[TablePlan]:
    plans: list[TablePlan] = []
    cw, ca = _context_where(keep)
    sw, sa = _state_where(keep)
    old_tables = set(_tables(conn))
    for t in sorted(old_tables | set(keep.tables)):
        rows = _count(conn, t) if t in old_tables else 0
        if t in _META_TABLES:
            plans.append(TablePlan(table=t, mode="meta", rows=rows, kept=0))
        elif t in keep.tables:
            plans.append(TablePlan(table=t, mode="copy", rows=rows, kept=rows))
        elif t == "context_entries":
            kept = _count(conn, t, cw, ca)
            plans.append(TablePlan(table=t, mode="context", rows=rows, kept=kept))
        elif t == "routine_state":
            kept = _count(conn, t, sw, sa)
            plans.append(TablePlan(table=t, mode="state", rows=rows, kept=kept))
        elif t == "config_changes":
            plans.append(TablePlan(table=t, mode="carry", rows=rows, kept=len(_carry_list(conn))))
        else:
            plans.append(TablePlan(table=t, mode="drop", rows=rows, kept=0))
    missing = [t for t in keep.tables if t not in new_tables]
    if missing:
        msg = f"keep list names tables the schema does not have: {', '.join(missing)}"
        raise ResetError(msg)
    return plans


def _schema_tables() -> set[str]:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(":memory:")
    try:
        migrate(c)
        return set(_tables(c))
    finally:
        c.close()


def _version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


def _arm_stores(db: Path) -> list[Path]:
    return sorted(db.parent.glob(ARM_STORE_GLOB))


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def plan_reset(
    db: Path | str,
    *,
    keep: ResetKeepList | None = None,
    lock_dir: Path | str | None = None,
) -> ResetPlan:
    """Read-only: what :func:`apply_reset` would keep, drop and carry, plus prechecks."""
    db = Path(db)
    keep = keep or load_keep_list()
    lock = Path(lock_dir) if lock_dir is not None else db.parent / "locks"
    conn = _connect_ro(db)
    try:
        head, version = _head_version(), _version(conn)
        return ResetPlan(
            db=str(db),
            schema_version=version,
            head_version=head,
            tables=_table_plans(conn, keep, _schema_tables()),
            carry=_carry_list(conn),
            checks=_prechecks(conn, lock, head, version),
            arm_stores=[str(p) for p in _arm_stores(db)],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _backup(src: Path, dst: Path) -> None:
    """Online ``.backup`` of *src* into *dst* (rollback journal, so it opens 0444 ``mode=ro``)."""
    s = _connect_ro(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode = DELETE")
        d.commit()
        if d.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            msg = f"archive {dst} failed quick_check"
            raise ResetError(msg)
    finally:
        d.close()
        s.close()


def _row_counts(path: Path) -> dict[str, int]:
    c = _connect_ro(path)
    try:
        return {t: _count(c, t) for t in _tables(c)}
    finally:
        c.close()


def _remove_db(path: Path) -> None:
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def _copy(
    new: sqlite3.Connection,
    table: str,
    where: str = "",
    args: tuple[Any, ...] = (),
    *,
    replace: bool = False,
) -> int:
    """Copy rows of *table* from the attached ``old`` store into ``main``.

    *replace*: the old row wins over a row the head migrations seeded (the
    ``rename:*`` cutover markers in ``routine_state`` carry the original instant).
    """
    cols = _columns(new, table)
    old_cols = set(_columns(new, table, "old"))
    if not old_cols:
        return 0  # a keep-list table the old store never had (empty)
    missing = [c for c in cols if c not in old_cols]
    if missing:
        msg = f"{table}: old store lacks columns {missing} (migrate it first)"
        raise ResetError(msg)
    sel = ", ".join(f'"{c}"' for c in cols)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    sql = (
        f'{verb} INTO main."{table}" ({sel}) SELECT {sel} FROM old."{table}"'  # noqa: S608
        + (f" WHERE {where}" if where else "")
        + " ORDER BY rowid"
    )
    cur = new.execute(sql, args)
    return int(cur.rowcount)


def _dump(model: BaseModel, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    data = model.model_dump(mode="json")
    for k in drop:
        data.pop(k, None)
    return data


def _diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, list[Any]]:
    return {k: [a.get(k), b.get(k)] for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def _effective(
    conn: sqlite3.Connection, base: ArcSettings, routines_path: Path | str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    from arc.control.effective import effective_routines, effective_settings

    s = _dump(effective_settings(conn, base=base), drop=("config_version",))
    r = _dump(effective_routines(conn, routines_path))
    return s, r


def apply_reset(  # noqa: PLR0913, PLR0915 - one deterministic sequence, kept in one place
    db: Path | str,
    *,
    out: Path | str,
    actor: str,
    settings: ArcSettings,
    now: _dt.datetime,
    keep: ResetKeepList | None = None,
    lock_dir: Path | str | None = None,
    account_last4: str | None = None,
    starting_equity: float | None = None,
    routines_path: Path | str | None = None,
    reason: str = "D80 fresh start",
) -> ResetResult:
    """Archive *db* (+ arm stores) under *out* and swap in a clean store. See module doc."""
    from arc.control.service import ControlService
    from arc.gate.halt import HaltSwitch
    from arc.store.db import connect
    from arc.store.identity import bind_store_env
    from arc.store.migrate import migrate
    from arc.store.repos import HaltRepo

    db = Path(db)
    keep = keep or load_keep_list()
    if actor != settings.owner_slack_user_id:
        msg = f"{actor!r} is not the owner; only {settings.owner_slack_user_id} may reset the store"
        raise ResetError(msg)
    plan = plan_reset(db, keep=keep, lock_dir=lock_dir)
    if not plan.ok:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in plan.checks if not c.ok)
        msg = f"refusing to reset {db}: {failed}"
        raise ResetError(msg)

    # 1. archive (store + arm stores), read-only, with a manifest
    stamp = now.strftime("%Y%m%d-%H%M")
    adir = Path(out) / f"{stamp}-{ARCHIVE_SUFFIX}"
    if adir.exists():
        msg = f"archive dir already exists: {adir}"
        raise ResetError(msg)
    adir.mkdir(parents=True)
    archive_db = adir / db.name
    _backup(db, archive_db)
    arms = _arm_stores(db)
    arm_archives: list[Path] = []
    for arm in arms:
        dst = adir / arm.name
        _backup(arm, dst)
        arm_archives.append(dst)
    files: dict[str, Any] = {}
    for p in [archive_db, *arm_archives]:
        files[p.name] = {
            "sha256": _sha256_file(p),
            "bytes": p.stat().st_size,
            "rows": _row_counts(p),
        }
    archive_sha = files[archive_db.name]["sha256"]
    manifest = adir / "MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": now.isoformat(),
                "source": str(db.resolve()),
                "arm_sources": [str(a.resolve()) for a in arms],
                "git_sha": _git_sha(),
                "actor": actor,
                "reason": reason,
                "schema_version": plan.schema_version,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    log.info("store.reset.archived", archive=str(adir), sha256=archive_sha)

    # 2. build the new store from the archive (never from the live file)
    new_path = db.with_name(db.name + ".new")
    _remove_db(new_path)
    conn = connect(new_path)
    try:
        migrate(conn)
        bind_store_env(conn, settings.env, now=now, path=str(db))
        conn.execute("ATTACH DATABASE ? AS old", (str(archive_db),))
        conn.execute("PRAGMA foreign_keys = OFF")
        cw, ca = _context_where(keep)
        sw, sa = _state_where(keep)
        with conn:
            for t in keep.tables:
                _copy(conn, t)
            _copy(conn, "context_entries", cw, ca)
            _copy(conn, "routine_state", sw, sa, replace=True)
        bad = conn.execute("PRAGMA main.foreign_key_check").fetchall()
        if bad:
            msg = f"new store has dangling references: {[tuple(r) for r in bad[:5]]}"
            raise ResetError(msg)
        conn.execute("PRAGMA foreign_keys = ON")

        # 3. verify kept data == archive
        for t in keep.tables:
            if not _columns(conn, t, "old"):
                continue
            cols = _columns(conn, t)
            if table_hash(conn, t, columns=cols) != table_hash(conn, t, columns=cols, schema="old"):
                msg = f"{t}: copied rows differ from the archive"
                raise ResetError(msg)
        cols = _columns(conn, "context_entries")
        if table_hash(conn, "context_entries", columns=cols) != table_hash(
            conn, "context_entries", columns=cols, where=cw, args=ca, schema="old"
        ):
            msg = "context_entries: copied rows differ from the archive"
            raise ResetError(msg)
        conn.execute("DETACH DATABASE old")

        # 4. halt stays on until the cutover is verified (the halts table is new)
        HaltSwitch(HaltRepo(conn)).halt(
            actor=actor, reason=f"{reason}: store reset, halted until cutover is verified", now=now
        )

        # 5. runtime config carry-over through the control service (a real change log)
        svc = ControlService(conn, base=settings, now=lambda: now, is_halted=lambda: True)
        note = f"D80 carry-over from {archive_db}"
        carried: list[dict[str, Any]] = []
        for c in plan.carry:
            res = svc.carry_over(c.key, c.value, actor=actor, reason=note)
            if res.outcome != "applied":
                msg = f"config carry-over refused for {c.key}: {res.message}"
                raise ResetError(msg)
            carried.append(
                {"key": c.key, "outcome": res.outcome, "change_id": res.change_id, "new": res.new}
            )
        old_ro = _connect_ro(archive_db)
        try:
            s_old, r_old = _effective(old_ro, settings, routines_path)
        finally:
            old_ro.close()
        s_new, r_new = _effective(conn, settings, routines_path)
        settings_diff, routines_diff = _diff(s_old, s_new), _diff(r_old, r_new)
        if settings_diff or routines_diff:
            msg = f"effective config changed: settings {settings_diff}, routines {routines_diff}"
            raise ResetError(msg)

        # 6. provenance marker
        epoch = {
            "started_at": now.isoformat(),
            "archived_from": str(db.resolve()),
            "archive": str(archive_db.resolve()),
            "archive_sha256": archive_sha,
            "account_last4": account_last4,
            "starting_equity": starting_equity,
        }
        with conn:
            conn.execute(
                "INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
                (EPOCH_KEY, json.dumps(epoch, sort_keys=True), to_db(now)),
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            msg = "new store failed integrity_check"
            raise ResetError(msg)
        new_tables = set(_tables(conn))
        kept_counts = {t: _count(conn, t) for t in new_tables}
    except BaseException:
        conn.close()
        _remove_db(new_path)
        raise
    conn.close()

    # 7. freeze the archive, swap the live path atomically
    for p in [archive_db, *arm_archives, manifest]:
        p.chmod(0o444)
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(f"{db}{suffix}").unlink()
    os.replace(new_path, db)
    _remove_db(new_path)  # stray -wal/-shm of the build connection
    for arm in arms:
        _remove_db(arm)
    log.info("store.reset.swapped", db=str(db), archive=str(archive_db))

    tables = [
        TablePlan(table=t.table, mode=t.mode, rows=t.rows, kept=kept_counts.get(t.table, 0))
        for t in plan.tables
    ]
    return ResetResult(
        db=str(db),
        archive_dir=str(adir),
        archive_db=str(archive_db),
        archive_sha256=archive_sha,
        manifest=str(manifest),
        arm_archives=[str(a) for a in arm_archives],
        tables=tables,
        carried=carried,
        settings_diff=settings_diff,
        routines_diff=routines_diff,
        epoch=epoch,
    )
