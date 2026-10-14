"""E15.7: the ``--arms`` Tower fixture (``scripts/tower_fixture_arms.py``).

The Playwright arm-switcher spec (``web/e2e/arms.spec.ts``) runs on this store; these tests
pin what it relies on through the real API: ``/api/meta`` lists the three running arms, an
arm-scoped read serves that arm's virtual account (not Production's book), and an unknown
arm is a 404.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.tower.api import create_app
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 30, 13, 42, tzinfo=ET)


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("arms") / "arc.db", NOW, ops=True, arms=True)


@pytest.fixture(scope="module")
def client(fx_db: Path, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    static = tmp_path_factory.mktemp("static")
    (static / "index.html").write_text("<html></html>")
    return TestClient(create_app(fx_db, static_dir=static, clock=lambda: NOW))


def test_arm_stores_sit_next_to_control(fx_db: Path) -> None:
    for t in ("t1", "t2", "t3"):
        assert (fx_db.parent / f"arc-exp-XP-12-{t}.db").exists()
    conn = sqlite3.connect(f"file:{fx_db}?mode=ro", uri=True)
    rows = conn.execute("select key from routine_state where key like 'experiment_arm:%'")
    keys = {r[0] for r in rows}
    assert keys == {f"experiment_arm:XP-12:{t}" for t in ("t1", "t2", "t3")}


def test_meta_lists_the_running_arms(client: TestClient) -> None:
    arms = client.get("/api/meta").json()["arms"]
    viewable = [a for a in arms if a["viewable"]]
    assert [a["id"] for a in viewable] == ["XP-12:t1", "XP-12:t2", "XP-12:t3"]
    assert [a["label"] for a in viewable] == ["XP-12 · T1", "XP-12 · T2", "XP-12 · T3"]


def test_arm_view_reads_the_arm_account(client: TestClient) -> None:
    control = client.get("/api/overview")
    arm = client.get("/api/overview", params={"arm": "XP-12:t1"})
    assert control.status_code == arm.status_code == 200
    assert arm.json() != control.json()
    assert "24,8" not in control.text
    snap = client.get("/api/snapshot", params={"arm": "XP-12:t2"})
    assert snap.status_code == 200


def test_unknown_arm_is_404(client: TestClient) -> None:
    assert client.get("/api/overview", params={"arm": "XP-99:t1"}).status_code == 404
