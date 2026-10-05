"""E8.8e: ``/api/ops/config`` feeds the full-page Effective Config + Change Log (D48, D50).

Every registry key appears exactly once with its group, default and allowed values; the
change log carries registry-formatted text and list flags; actor display names come from
``tower.actor_names`` in routines.yaml.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.config import ArcSettings
from arc.control.registry import REGISTRY, Group, ValueType
from arc.routines.config import TowerSettings, load_routines
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_ops import GROUP_LABELS, load_config
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
ops_fixture = _load("tower_fixture_ops", REPO / "scripts" / "tower_fixture_ops.py")


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("cfg") / "arc.db", NOW, ops=True)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


def test_every_registry_key_once_with_group_default_and_allowed(conn) -> None:
    c = load_config(conn, ArcSettings(), now=NOW)
    keys = [k.key for k in c.keys]
    assert len(keys) == len(set(keys))  # exactly once
    by = {k.key: k for k in c.keys}
    for key, t in REGISTRY.items():
        row = by[key]
        assert row.group == t.group.value
        assert row.default_text  # a default is always shown
        assert row.value_type == t.type.value
        assert row.choices == list(t.choices)
        assert row.min == t.min and row.max == t.max
        assert row.bounds == t.bounds  # allowed values text
        if t.type is ValueType.CHOICE:
            assert row.choices and row.bounds == " | ".join(row.choices)
    assert by["universe"].is_list and by["universe"].value_type == "tickers"
    assert not by["account_profile"].is_list
    assert by["account_profile"].choices == ["cash_long_only", "cash_debit", "margin"]


def test_groups_follow_registry_order_with_counts(conn) -> None:
    c = load_config(conn, ArcSettings(), now=NOW)
    order = [g.value for g in Group]
    assert [g.key for g in c.groups] == [g for g in order if any(k.group == g for k in c.keys)]
    assert sum(g.keys for g in c.groups) == len(c.keys)
    for g in c.groups:
        assert g.label == GROUP_LABELS[g.key]
        assert g.label[0].isupper()


def test_universe_change_is_a_list_with_registry_text(conn) -> None:
    c = load_config(conn, ArcSettings(), now=NOW)
    ch = next(x for x in c.changes if x.key == "universe")
    assert ch.is_list and ch.group == "universe"
    assert len(ch.old) == 20 and len(ch.new) == 100
    assert {"DIA", "XLF"} <= set(ch.old) - set(ch.new)
    assert ch.new_text is not None and ch.new_text.startswith("SPY, QQQ")
    scalar = next(x for x in c.changes if x.key == "max_open_positions")
    assert not scalar.is_list and (scalar.old_text, scalar.new_text) == ("5", "4")
    by = {k.key: k for k in c.keys}
    assert len(by["universe"].value) == 100  # the override reaches the effective value


def test_actor_names_come_from_routines_yaml(fx_db) -> None:
    names = load_routines().tower.actor_names
    assert names.get("U0C5KUMH28G") == "Mohit"
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as client:
        body = client.get("/api/ops/config").json()
    assert body["actor_names"] == names
    actors = {ch["actor"] for ch in body["changes"]}
    assert "U0C5KUMH28G" in actors and "U0OWNER" in actors  # stored ids stay ids
    assert {g["key"] for g in body["groups"]} >= {"account", "universe", "risk"}


def test_actor_names_validation() -> None:
    assert TowerSettings().actor_names == {}
    with pytest.raises(ValueError, match="non-empty"):
        TowerSettings.model_validate({"actor_names": {"U1": " "}})
    with pytest.raises(ValueError, match="Extra inputs"):
        TowerSettings.model_validate({"actor_name": {}})


def test_unknown_change_key_is_tolerated() -> None:
    """A change-log row for a key no longer in the registry renders as plain text."""
    from arc.control.store import ConfigChange
    from arc.tower.data_ops import _change_row

    row = ConfigChange(
        id=9, key="retired_knob", old=1, new=2, actor="U9", at=NOW, source="cli",
        status="applied", direction="neutral",
    )  # fmt: skip
    ch = _change_row(row)
    assert ch.group is None and (ch.old_text, ch.new_text) == ("1", "2") and not ch.is_list
    lst = _change_row(
        row.model_copy(update={"key": "retired_list", "old": ["A"], "new": ["A", "B"]})
    )
    assert lst.is_list


def test_run_detail_carries_label_and_persona(fx_db) -> None:
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as client:
        rows = client.get("/api/ops/runs?job=director&size=1").json()["rows"]
        body = client.get(f"/api/ops/runs/{rows[0]['run_id']}").json()
    assert body["step"]["persona"] == "director"
    assert body["step"]["label"] and body["step"]["label"] != "director"
    assert all("label" in s for s in body["chain"])
