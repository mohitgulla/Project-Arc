"""E19.1 (D80): ``arc store reset`` archives the store and builds a clean one."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.control.effective import effective_routines, effective_settings
from arc.control.service import ControlService
from arc.gate.halt import HaltSwitch
from arc.store.db import connect
from arc.store.identity import bind_store_env, read_store_env
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.store.reset import (
    DEFAULT_KEEP_PATH,
    EPOCH_KEY,
    ResetError,
    ResetKeepList,
    apply_reset,
    load_keep_list,
    plan_reset,
    table_hash,
)
from arc.utils.calendar import ET

OWNER = "U0OWNER001"
NOW = dt.datetime(2026, 10, 11, 18, 30, tzinfo=ET)
BUILT = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
KEEP = load_keep_list()
KEPT_KIND = "regime"
DROPPED_KIND = "portfolio_context"
# Seeded by hand (control/halt semantics), never by the generic filler.
_MANUAL = {
    "schema_version",
    "sqlite_sequence",
    "store_identity",
    "arm_identity",  # a populated arm_identity makes the store an arm store (E10.2)
    "halts",
    "context_entries",
    "routine_state",
    "config_changes",
    "config_pending",
    "experiments",
    "experiment_events",
}


def settings() -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None, owner_slack_user_id=OWNER, approver_slack_user_ids=[OWNER]
    )


@pytest.fixture(autouse=True)
def _no_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_DB_PATH", raising=False)


def _value(col_type: str, i: int, *, pk: bool = False, choices: list[str] | None = None) -> Any:
    if choices:
        return choices[(i - 1) % len(choices)]
    t = col_type.upper()
    if "INT" in t:
        return i if pk else 1  # 1 satisfies the 0/1 flag and > 0 checks
    if "REAL" in t or "FLOA" in t or "NUM" in t:
        return 0.5  # inside every (0, 1] / > 0 check
    if "BLOB" in t:
        return b"\x78\x9c" + bytes([i % 256])
    return f'"v{i}"'  # a JSON string: some triggers json_extract() text columns


def _choices(sql: str, col: str) -> list[str] | None:
    """The literals of a ``CHECK (<col> IN ('a', ...))`` on *col*, if any."""
    m = re.search(rf"CHECK\s*\(\s*\"?{re.escape(col)}\"?\s+IN\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if not m:
        return None
    lits = re.findall(r"'([^']*)'", m.group(1))
    return lits or None


def _fill(conn: sqlite3.Connection, table: str, n: int = 3) -> None:
    """*n* synthetic rows: NOT NULL columns get CHECK-valid values, nullable ones stay NULL."""
    cols = list(conn.execute(f'PRAGMA table_info("{table}")'))
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    for i in range(1, n + 1):
        errors = []
        for every in (True, False):  # every column first; then NOT NULL ones only
            names, vals = [], []
            for c in cols:
                _, name, ctype, notnull, _default, pk = tuple(c)
                if every or notnull or pk:
                    names.append(f'"{name}"')
                    choices = _choices(sql, name)
                    vals.append(_value(ctype or "TEXT", i, pk=bool(pk), choices=choices))
            try:
                conn.execute(
                    f'INSERT INTO "{table}" ({", ".join(names)}) '
                    f"VALUES ({', '.join('?' * len(vals))})",
                    vals,
                )
                break
            except sqlite3.IntegrityError as exc:
                errors.append(str(exc))
        else:
            raise AssertionError(f"{table}: {errors}")


def _ctx(conn: sqlite3.Connection, cid: str, kind: str, sup: str | None = None) -> None:
    conn.execute(
        """INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by,
               created_at, valid_from, supersedes_id, status)
           VALUES (?, ?, 'SPY', '{"a": 1}', 1, 'test', ?, ?, ?, 'active')""",
        (cid, kind, "2026-10-09T14:00:00.000000Z", "2026-10-09T14:00:00.000000Z", sup),
    )


def _state(conn: sqlite3.Connection, key: str, value: str = "2026-10-09T14:00:00.000000Z") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, "2026-10-09T14:00:00.000000Z"),
    )


def _experiment_event(conn: sqlite3.Connection, xid: str, status: str) -> None:
    conn.execute(
        """INSERT INTO experiment_events (experiment_id, status, spec_hash, actor, at)
           VALUES (?, ?, 'h', 'test', '2026-10-09T14:00:00.000000Z')""",
        (xid, status),
    )


def build_store(path: Path, *, halted: bool = True, experiment: str = "stopped") -> Path:
    """A migrated, stamped store with every table populated and real D26 overrides."""
    conn = connect(path)
    migrate(conn)
    bind_store_env(conn, "paper", now=BUILT)
    conn.execute("PRAGMA foreign_keys = OFF")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    with conn:
        for t in tables:
            if t not in _MANUAL:
                _fill(conn, t)
        _ctx(conn, "c-keep-1", KEPT_KIND)
        _ctx(conn, "c-keep-2", KEPT_KIND, sup="c-keep-1")
        _ctx(conn, "c-keep-3", "chain_snapshot")
        _ctx(conn, "c-drop-1", DROPPED_KIND)
        _ctx(conn, "c-drop-2", "proposal")
        _state(conn, "cursor:scalp")
        _state(conn, "cursor:rss")
        _state(conn, "finnhub:calls", '{"n": 3}')
        _state(conn, "alpaca_data:calls", '{"n": 4}')
        _state(conn, "rename:sweep_to_scalp")
        _state(conn, "dispatcher:last_tick")
        _state(conn, "day_thread:2026-10-09", "123.456")
        _state(conn, "loop_root:chain-abc", "{}")
        _state(conn, "close_quote_fail:os-1", "{}")
        _experiment_event(conn, "XP-1", "running")
        _experiment_event(conn, "XP-1", experiment)
    conn.execute("PRAGMA foreign_keys = ON")
    # Real overrides through the control service: riskier ones via confirm.
    svc = ControlService(conn, base=settings(), now=lambda: BUILT, is_halted=lambda: False)
    for key, raw in [
        ("max_alloc_pct", "7.5%"),
        ("loop.max_idle", "20"),  # D85: 15 is the shipped default now, so not an override
        ("personas.finnhub_context", "off"),  # D85: on is the shipped default now
        ("auto_approve.paper", "on"),
        ("max_alloc_pct", "7.5%"),  # unchanged: no row
    ]:
        r = svc.set(key, raw, actor=OWNER, source="cli")
        if r.outcome == "pending":
            assert r.pending is not None
            r = svc.confirm(r.pending.code, actor=OWNER, source="cli")
        assert r.outcome in {"applied", "unchanged"}, (key, r.message)
    # A key set and later reverted to its default is not carried.
    r = svc.set("portfolio_dollar_delta_cap_pct", "80%", actor=OWNER, source="cli")
    if r.outcome == "pending":
        assert r.pending is not None
        r = svc.confirm(r.pending.code, actor=OWNER, source="cli")
    assert r.outcome == "applied", r.message
    r = svc.revert("portfolio_dollar_delta_cap_pct", actor=OWNER, source="cli")
    if r.outcome == "pending":
        assert r.pending is not None
        r = svc.confirm(r.pending.code, actor=OWNER, source="cli")
    assert r.outcome == "reverted", r.message
    if halted:
        HaltSwitch(HaltRepo(conn)).halt(actor=OWNER, reason="pre-reset", now=BUILT)
    conn.close()
    return path


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Built once per module (migrations + control service take ~2 s), copied per test."""
    return build_store(tmp_path_factory.mktemp("tpl") / "arc.db")


@pytest.fixture
def store(tmp_path: Path, template: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(template, data / "arc.db")
    arm = connect(data / "arc-exp-XP-10.db")
    migrate(arm)
    arm.close()
    return data / "arc.db"


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _counts(path: Path) -> dict[str, int]:
    c = _ro(path)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in names}
    finally:
        c.close()


def _apply(store: Path, tmp_path: Path, **kw: Any) -> Any:
    return apply_reset(
        store,
        out=tmp_path / "archive",
        actor=OWNER,
        settings=settings(),
        now=NOW,
        lock_dir=tmp_path / "locks",
        account_last4="AB12",
        starting_equity=25_000.0,
        **kw,
    )


# ---------------------------------------------------------------------------
# keep list
# ---------------------------------------------------------------------------


def test_keep_list_shipped_config() -> None:
    k = load_keep_list(DEFAULT_KEEP_PATH)
    assert "raw_docs" in k.tables and "market_tape" in k.tables
    assert "scalp_batches" not in k.tables
    for dropped in ("portfolio_context", "proposal", "candidate", "structures", "note", "journal"):
        assert dropped not in k.context_kinds
    assert {"regime", "chain_snapshot", "story", "scout_read"} <= set(k.context_kinds)
    assert "cursor:" in k.routine_state_prefixes


def test_keep_list_rejects_unknown_fields(tmp_path: Path) -> None:
    p = tmp_path / "k.yaml"
    p.write_text(yaml.safe_dump({"tables": ["raw_docs"], "bogus": 1}))
    with pytest.raises(ValueError, match="bogus"):
        load_keep_list(p)


def test_new_kind_added_by_config_only(store: Path, tmp_path: Path) -> None:
    """A kind added to the YAML is kept, with no code change."""
    data = yaml.safe_load(DEFAULT_KEEP_PATH.read_text())
    data["context_kinds"].append(DROPPED_KIND)
    p = tmp_path / "keep.yaml"
    p.write_text(yaml.safe_dump(data))
    keep = load_keep_list(p)
    plan = plan_reset(store, keep=keep, lock_dir=tmp_path / "locks")
    ctx = next(t for t in plan.tables if t.table == "context_entries")
    assert ctx.kept == 4
    _apply(store, tmp_path, keep=keep)
    c = _ro(store)
    kinds = {r[0] for r in c.execute("SELECT kind FROM context_entries")}
    assert DROPPED_KIND in kinds and "proposal" not in kinds


def test_keep_list_table_missing_from_schema(store: Path, tmp_path: Path) -> None:
    keep = ResetKeepList(tables=["raw_docs", "no_such_table"])
    with pytest.raises(ResetError, match="no_such_table"):
        plan_reset(store, keep=keep, lock_dir=tmp_path / "locks")


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_plan_writes_nothing(store: Path, tmp_path: Path) -> None:
    before = (store.stat().st_mtime_ns, _counts(store))
    plan = plan_reset(store, lock_dir=tmp_path / "locks")
    assert plan.ok, plan.lines()
    modes = {t.table: t for t in plan.tables}
    assert modes["raw_docs"].mode == "copy" and modes["raw_docs"].kept == 3
    assert modes["orders"].mode == "drop" and modes["orders"].rows == 3
    assert modes["orders"].kept == 0
    assert modes["context_entries"].mode == "context" and modes["context_entries"].kept == 3
    # 4 seeded + 3 rename:* markers (the migrations write them; the fixture overwrote one)
    assert modes["routine_state"].kept == 7
    assert modes["store_identity"].mode == "meta"
    assert [c.key for c in plan.carry] == [
        "max_alloc_pct",
        "loop.max_idle",
        "personas.finnhub_context",
        "auto_approve.paper",
    ]
    assert plan.arm_stores == [str(store.parent / "arc-exp-XP-10.db")]
    text = "\n".join(plan.lines())
    assert "max_alloc_pct = 0.075" in text and "[ok] halted" in text
    assert (store.stat().st_mtime_ns, _counts(store)) == before
    assert not (tmp_path / "archive").exists()


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_refuses_when_not_halted(tmp_path: Path) -> None:
    db = build_store(tmp_path / "arc.db", halted=False)
    plan = plan_reset(db, lock_dir=tmp_path / "locks")
    assert not plan.ok
    with pytest.raises(ResetError, match="not halted"):
        _apply(db, tmp_path)
    assert not (tmp_path / "archive").exists()


def test_refuses_on_opens_only_halt(tmp_path: Path) -> None:
    from arc.gate.halt import HaltScope

    db = build_store(tmp_path / "arc.db", halted=False)
    c = connect(db)
    HaltSwitch(HaltRepo(c)).halt(actor=OWNER, reason="x", now=BUILT, scope=HaltScope.OPENS)
    c.close()
    with pytest.raises(ResetError, match="opens-only"):
        _apply(db, tmp_path)


def test_refuses_when_experiment_running(tmp_path: Path) -> None:
    db = build_store(tmp_path / "arc.db", experiment="running")
    with pytest.raises(ResetError, match="running: XP-1"):
        _apply(db, tmp_path)


def test_refuses_when_lock_held(store: Path, tmp_path: Path) -> None:
    locks = tmp_path / "locks" / "arm-treatment"
    locks.mkdir(parents=True)
    (tmp_path / "locks" / "scalp.lock").touch()  # a stale, unheld lock file is fine
    p = locks / "experiment-arms.lock"
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ResetError, match="arm-treatment/experiment-arms.lock"):
            _apply(store, tmp_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert plan_reset(store, lock_dir=tmp_path / "locks").ok


def test_refuses_non_owner(store: Path, tmp_path: Path) -> None:
    with pytest.raises(ResetError, match="not the owner"):
        apply_reset(store, out=tmp_path / "a", actor="U0STRANGER", settings=settings(), now=NOW)


def test_refuses_stale_schema(tmp_path: Path) -> None:
    db = build_store(tmp_path / "arc.db")
    c = sqlite3.connect(db)
    c.execute(
        "DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)"
    )
    c.commit()
    c.close()
    with pytest.raises(ResetError, match="schema"):
        _apply(db, tmp_path)


def test_refuses_existing_archive_dir(store: Path, tmp_path: Path) -> None:
    (tmp_path / "archive" / "20261011-1830-pre-d80").mkdir(parents=True)
    with pytest.raises(ResetError, match="already exists"):
        _apply(store, tmp_path)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_resets_store(store: Path, tmp_path: Path) -> None:  # noqa: PLR0915
    old_counts = _counts(store)
    old = _ro(store)
    old_hashes = {t: table_hash(old, t) for t in KEEP.tables}
    old_ctx = table_hash(old, "context_entries", where="kind IN ('regime', 'chain_snapshot')")
    s_old = effective_settings(old, base=settings())
    r_old = effective_routines(old)
    old.close()

    res = _apply(store, tmp_path)

    # archive: read-only copies + manifest
    adir = tmp_path / "archive" / "20261011-1830-pre-d80"
    assert Path(res.archive_dir) == adir
    arch = adir / "arc.db"
    for p in (arch, adir / "arc-exp-XP-10.db", adir / "MANIFEST.json"):
        assert p.is_file() and not (p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    assert _counts(arch) == old_counts
    manifest = json.loads((adir / "MANIFEST.json").read_text())
    assert manifest["files"]["arc.db"]["rows"] == old_counts
    assert manifest["files"]["arc.db"]["sha256"] == res.archive_sha256
    assert "arc-exp-XP-10.db" in manifest["files"]
    assert not (store.parent / "arc-exp-XP-10.db").exists()
    assert not Path(f"{store}.new").exists()

    new = _ro(store)
    # kept tables identical (count + content hash)
    for t in KEEP.tables:
        assert table_hash(new, t) == old_hashes[t], t
    assert table_hash(new, "context_entries") == old_ctx
    assert {r[0] for r in new.execute("SELECT kind FROM context_entries")} == {
        KEPT_KIND,
        "chain_snapshot",
    }
    keys = {r[0] for r in new.execute("SELECT key FROM routine_state")}
    assert keys == {
        "cursor:scalp",
        "cursor:rss",
        "finnhub:calls",
        "alpaca_data:calls",
        "rename:sweep_to_scalp",
        "rename:scout_to_sweep",
        "rename:investor_to_broker",
        EPOCH_KEY,
    }
    # the archived cutover instant wins over the one the head migrations just wrote
    v = new.execute("SELECT value FROM routine_state WHERE key = 'rename:sweep_to_scalp'")
    assert v.fetchone()[0] == "2026-10-09T14:00:00.000000Z"
    # dropped tables empty
    counts = _counts(store)
    kept = {*KEEP.tables, "context_entries", "routine_state", "config_changes", "halts"}
    for t, n in counts.items():
        if t in kept or t in {"schema_version", "sqlite_sequence", "store_identity"}:
            continue
        assert n == 0, t
    for t in ("orders", "fills", "proposals", "decisions", "pnl_snapshots", "scalp_batches"):
        assert counts[t] == 0 and old_counts[t] > 0
    # triggers and schema at head
    trig = "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    old_trig = {r[0] for r in _ro(arch).execute(trig)}
    new_trig = {r[0] for r in new.execute(trig)}
    assert new_trig == old_trig and "decisions_no_delete" in new_trig
    with pytest.raises(sqlite3.DatabaseError):
        new.execute("DELETE FROM context_entries")
    assert counts["schema_version"] == old_counts["schema_version"]
    # one fresh store_identity
    assert counts["store_identity"] == 1
    ident = read_store_env(new)
    assert ident is not None and ident.env == "paper" and ident.created_at == NOW
    # halted (new halts table, re-asserted)
    assert HaltSwitch(HaltRepo(new)).is_halted()
    assert counts["halts"] == 1
    # overrides re-applied through the control service
    rows = new.execute(
        "SELECT key, actor, reason, status FROM config_changes ORDER BY id"
    ).fetchall()
    assert [r["key"] for r in rows] == [
        "max_alloc_pct",
        "loop.max_idle",
        "personas.finnhub_context",
        "auto_approve.paper",
    ]
    assert {r["actor"] for r in rows} == {OWNER}
    assert all(r["reason"] == f"D80 carry-over from {arch}" for r in rows)
    assert {r["status"] for r in rows} == {"applied"}
    assert res.settings_diff == {} and res.routines_diff == {}
    s_new = effective_settings(new, base=settings())
    assert s_new.max_alloc_pct == pytest.approx(0.075) == s_old.max_alloc_pct
    assert s_new.auto_approve is True
    assert effective_routines(new).model_dump() == r_old.model_dump()
    # provenance marker
    epoch = json.loads(
        new.execute("SELECT value FROM routine_state WHERE key = ?", (EPOCH_KEY,)).fetchone()[0]
    )
    assert epoch == {
        "account_last4": "AB12",
        "archive": str(arch.resolve()),
        "archive_sha256": res.archive_sha256,
        "archived_from": str(store.resolve()),
        "started_at": NOW.isoformat(),
        "starting_equity": 25_000.0,
    }
    assert new.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    new.close()
    assert "effective settings diff (must be empty): {}" in "\n".join(res.lines())


def test_apply_without_arm_stores(tmp_path: Path) -> None:
    db = build_store(tmp_path / "arc.db")
    res = _apply(db, tmp_path)
    assert res.arm_archives == []
    assert sorted(p.name for p in Path(res.archive_dir).iterdir()) == ["MANIFEST.json", "arc.db"]


def test_failed_build_leaves_live_store(store: Path, tmp_path: Path, monkeypatch) -> None:
    import arc.store.reset as reset_mod

    before = _counts(store)

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise ResetError("injected")

    monkeypatch.setattr(reset_mod, "_diff", boom)
    with pytest.raises(ResetError, match="injected"):
        _apply(store, tmp_path)
    assert _counts(store) == before
    assert not Path(f"{store}.new").exists()
    assert (store.parent / "arc-exp-XP-10.db").exists()


def test_carry_over_refusal_aborts(store: Path, tmp_path: Path) -> None:
    c = sqlite3.connect(store)
    c.execute("DROP TRIGGER config_changes_no_update")
    c.execute("UPDATE config_changes SET new = '7' WHERE key = 'max_alloc_pct'")  # 700%: invalid
    c.commit()
    c.close()
    with pytest.raises(ResetError, match="carry-over refused for max_alloc_pct"):
        _apply(store, tmp_path)
    assert not Path(f"{store}.new").exists()


# ---------------------------------------------------------------------------
# ControlService.carry_over
# ---------------------------------------------------------------------------


def test_carry_over_applies_riskier_without_confirm() -> None:
    c = connect(":memory:")
    migrate(c)
    svc = ControlService(c, base=settings(), now=lambda: NOW, is_halted=lambda: True)
    r = svc.carry_over("max_alloc_pct", 0.075, actor=OWNER, reason="D80")
    assert r.outcome == "applied" and r.direction == "riskier" and r.halted
    assert svc.settings().max_alloc_pct == pytest.approx(0.075)
    assert svc.carry_over("max_alloc_pct", 0.075, actor="U0X", reason="x").outcome == "refused"
    assert svc.carry_over("no.such.key", 1, actor=OWNER, reason="x").outcome == "refused"
    assert svc.carry_over("live.gate_met", True, actor=OWNER, reason="x").outcome == "refused"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    import arc.store.identity_cli as cli_mod
    import arc.utils.calendar as cal
    from arc.cli import main

    monkeypatch.setattr(cli_mod, "get_settings", settings)
    monkeypatch.setattr(cal, "now_et", lambda: NOW)
    return main(argv)


def test_cli_dry_run_and_apply(store: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    locks = str(tmp_path / "locks")
    base = ["store", "reset", "--db", str(store), "--lock-dir", locks]
    rc = _cli(base, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0 and "dry run: nothing written" in out and "orders" in out
    rc = _cli([*base, "--json"], monkeypatch)
    assert rc == 0 and len(json.loads(capsys.readouterr().out)["carry"]) == 4
    rc = _cli([*base, "--apply"], monkeypatch)
    assert rc == 2 and "--actor" in capsys.readouterr().out
    apply = [*base, "--apply", "--actor", OWNER, "--out", str(tmp_path / "arch")]
    rc = _cli([*apply, "--account-last4", "AB12", "--starting-equity", "25000"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0 and "store reset applied" in out and "(must be empty): {}" in out
    assert (tmp_path / "arch" / "20261011-1830-pre-d80" / "MANIFEST.json").is_file()
    # The new store is halted and clean, so it passes the prechecks; a second reset in
    # the same minute is refused on the existing archive dir.
    rc = _cli(apply, monkeypatch)
    assert rc == 1 and "already exists" in capsys.readouterr().out


def test_cli_refusal_exit_codes(tmp_path: Path, monkeypatch, capsys) -> None:
    db = build_store(tmp_path / "arc.db", halted=False)
    rc = _cli(["store", "reset", "--db", str(db), "--lock-dir", str(tmp_path / "l")], monkeypatch)
    assert rc == 1 and "[FAIL] halted" in capsys.readouterr().out
    rc = _cli(
        [
            "store",
            "reset",
            "--db",
            str(db),
            "--apply",
            "--actor",
            OWNER,
            "--lock-dir",
            str(tmp_path / "l"),
        ],
        monkeypatch,
    )
    assert rc == 1 and "REFUSED" in capsys.readouterr().out
    rc = _cli(["store", "reset", "--db", str(tmp_path / "missing.db")], monkeypatch)
    assert rc == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("tables: []\n")
    rc = _cli(["store", "reset", "--db", str(db), "--keep-list", str(bad)], monkeypatch)
    assert rc == 2 and "keep list" in capsys.readouterr().out


def test_cli_identity_still_works(store: Path, monkeypatch, capsys) -> None:
    assert _cli(["store", "identity", "--db", str(store)], monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["env"] == "paper"
