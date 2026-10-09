"""E11.3 / D70: per-env store identity (paper and live never share a store)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from arc.config import ArcSettings
from arc.store.db import DEFAULT_DB_PATH, connect
from arc.store.identity import (
    DEFAULT_LIVE_DB_PATH,
    StoreEnvMismatchError,
    bind_store_env,
    check_store_env,
    default_db_path,
    open_store,
    read_store_env,
    store_path,
    write_store_env,
)
from arc.store.migrate import migrate
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
_FAKE_LIVE = Path(__file__)  # any existing file satisfies the live-env guard


def paper() -> ArcSettings:
    return ArcSettings(_env_file=None)  # type: ignore[call-arg]


def live(**kw: object) -> ArcSettings:
    with mock.patch("arc.config._LIVE_ENV_PATH", _FAKE_LIVE):
        return ArcSettings(_env_file=None, env="live", **kw)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _no_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_DB_PATH", raising=False)


def _migrated() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def test_first_open_stamps_running_env(tmp_path: Path) -> None:
    p = open_store(tmp_path / "p.db", settings=paper(), now=NOW)
    assert (ident := read_store_env(p)) is not None and ident.env == "paper"
    lv = open_store(tmp_path / "l.db", settings=live(), now=NOW)
    assert (ident := read_store_env(lv)) is not None and ident.env == "live"
    assert ident.created_at == NOW


def test_reopen_same_env_keeps_stamp(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    open_store(db, settings=paper(), now=NOW).close()
    c = open_store(db, settings=paper(), now=NOW + dt.timedelta(days=1))
    ident = read_store_env(c)
    assert ident is not None and ident.created_at == NOW


def test_live_process_refuses_paper_store(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    open_store(db, settings=paper(), now=NOW).close()
    built: list[str] = []

    def broker_factory() -> None:  # never reached: the store check comes first
        built.append("broker")

    with pytest.raises(StoreEnvMismatchError, match="paper store; ARC_ENV=live"):
        open_store(db, settings=live(), now=NOW)
        broker_factory()
    assert built == []


def test_paper_process_refuses_live_store(tmp_path: Path) -> None:
    db = tmp_path / "arc-live.db"
    open_store(db, settings=live(), now=NOW).close()
    with pytest.raises(StoreEnvMismatchError) as exc:
        open_store(db, settings=paper(), now=NOW)
    assert exc.value.store_env == "live" and exc.value.running_env == "paper"


def test_memory_store_is_stamped_too() -> None:
    c = open_store(":memory:", settings=live(), now=NOW)
    assert (ident := read_store_env(c)) is not None and ident.env == "live"


def test_identity_is_append_only() -> None:
    c = _migrated()
    write_store_env(c, "paper", NOW)
    with pytest.raises(sqlite3.IntegrityError), c:
        c.execute("UPDATE store_identity SET env = 'live'")
    with pytest.raises(sqlite3.IntegrityError), c:
        c.execute("DELETE FROM store_identity")
    with pytest.raises(sqlite3.IntegrityError):
        write_store_env(c, "live", NOW)  # one row only (id = 1)
    assert (ident := read_store_env(c)) is not None and ident.env == "paper"


def test_env_check_constraint() -> None:
    c = _migrated()
    with pytest.raises(sqlite3.IntegrityError):
        write_store_env(c, "staging", NOW)


def test_unmigrated_or_unstamped_reads_none() -> None:
    assert read_store_env(sqlite3.connect(":memory:")) is None
    c = _migrated()
    assert read_store_env(c) is None
    assert check_store_env(c, "live") is None  # read-only check passes an unstamped store


def test_bind_is_idempotent() -> None:
    c = _migrated()
    a = bind_store_env(c, "paper", now=NOW)
    b = bind_store_env(c, "paper", now=NOW + dt.timedelta(hours=1))
    assert a == b


def test_default_db_path_depends_on_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_db_path("paper") == DEFAULT_DB_PATH
    assert default_db_path("live") == DEFAULT_LIVE_DB_PATH
    assert DEFAULT_LIVE_DB_PATH.name == "arc-live.db"
    assert paper().db_path is None and store_path(paper()) == DEFAULT_DB_PATH
    assert live().db_path == DEFAULT_LIVE_DB_PATH
    assert store_path(live()) == DEFAULT_LIVE_DB_PATH
    monkeypatch.setenv("ARC_DB_PATH", "/tmp/override.db")  # noqa: S108
    assert store_path(paper()) == Path("/tmp/override.db")  # noqa: S108
    assert store_path(live()) == Path("/tmp/override.db")  # noqa: S108
    assert store_path(live(), "x.db") == "x.db"  # --db beats both


def test_cli_open_paths_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control-store helper every CLI uses refuses the other env's store."""
    from arc.control.effective import effective_from_path
    from arc.control.effective import open_store as control_open

    db = tmp_path / "arc.db"
    control_open(db, settings=paper()).close()
    with pytest.raises(StoreEnvMismatchError):
        control_open(db, settings=live())
    with pytest.raises(StoreEnvMismatchError):  # the tower's read-only path too
        effective_from_path(db, base=live())
    s, _ = effective_from_path(db, base=paper())
    assert s.env.value == "paper"


def test_journal_read_only_refuses_other_env(tmp_path: Path) -> None:
    from arc.journal.cli import _connect_ro

    db = tmp_path / "arc.db"
    open_store(db, settings=paper(), now=NOW).close()
    _connect_ro(str(db), paper()).close()
    with pytest.raises(StoreEnvMismatchError):
        _connect_ro(str(db), live())


def test_pipeline_open_db_copy_binds(tmp_path: Path) -> None:
    from arc.pipeline.runner import open_db

    db = tmp_path / "arc.db"
    open_store(db, settings=paper(), now=NOW).close()
    c = open_db(db, copy=True, settings=paper())
    assert (ident := read_store_env(c)) is not None and ident.env == "paper"
    with pytest.raises(StoreEnvMismatchError):
        open_db(db, copy=True, settings=live())
    with pytest.raises(StoreEnvMismatchError):
        open_db(db, copy=False, settings=live())


def test_store_identity_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from arc.cli import main

    db = tmp_path / "arc.db"
    open_store(db, settings=paper(), now=NOW).close()
    assert main(["store", "identity", "--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["env"] == "paper" and out["path"] == str(db) and out["running_env"] == "paper"
    with mock.patch("arc.store.identity_cli.get_settings", return_value=live()):
        assert main(["store", "identity", "--db", str(db)]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["env"] == "paper" and out["running_env"] == "live" and out["match"] is False


def test_tower_refuses_other_env_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from arc.tower.api import create_app

    monkeypatch.setattr("arc.config._LIVE_ENV_PATH", _FAKE_LIVE)
    db = tmp_path / "arc.db"
    open_store(db, settings=paper(), now=NOW).close()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    ok = TestClient(create_app(db, paper(), static_dir=static, clock=lambda: NOW))
    assert ok.get("/api/snapshot").status_code == 200
    bad = TestClient(create_app(db, live(), static_dir=static, clock=lambda: NOW))
    r = bad.get("/api/snapshot")
    assert r.status_code == 503
    assert "store_env_mismatch" in r.text or "StoreEnvMismatchError" in r.text
