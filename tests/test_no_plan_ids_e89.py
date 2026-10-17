"""E8.9 (D122): no plan (``D#``) or card (``E#.#``) ids in owner-facing config text.

Registry descriptions feed the Tower Config page (``/api/ops/config``) and Slack ``!arc
config``; ``routines.yaml`` ``label`` / ``about`` feed the Session Timeline. Provenance for
devs lives in :attr:`arc.control.registry.Tunable.ref`, which is never served.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.control.registry import REGISTRY, lookup
from arc.store.migrate import migrate
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent
IDS = re.compile(r"\bD\d{1,3}\b|\bE\d{1,2}\.\d+[a-z]?\b")

# Keys built on demand (routine / profile / sleeve patterns): one example each.
PATTERN_KEYS = (
    "routines.scalp.cadence",
    "routines.scalp.enabled",
    "profiles.cash_debit.dte_min",
    "profiles.cash_debit.sleeves.swing.max_positions",
    "profiles.cash_debit.sleeves.swing.dte_max",
)


def _ids(text: str) -> list[str]:
    return IDS.findall(text)


def test_id_pattern_matches_plan_and_card_ids_only() -> None:
    assert _ids("D116 base tier of the D18 cap (E24.2, E7.5a)") == ["D116", "D18", "E24.2", "E7.5a"]
    # Not ids: units, tickers, Greek labels, ordinary words.
    assert _ids("Σ Δ × spot, DTE 45, D-day, SPY, 2D, E-mini, ADV, IDE") == []


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_registry_description_has_no_plan_or_card_ids(key: str) -> None:
    t = REGISTRY[key]
    assert _ids(t.description) == [], f"{key}: {t.description!r}"
    assert _ids(t.unit) == []


@pytest.mark.parametrize("key", PATTERN_KEYS)
def test_pattern_key_description_has_no_ids(key: str) -> None:
    assert _ids(lookup(key).description) == []


def test_ref_keeps_provenance_for_devs() -> None:
    # The stripped ids moved to `ref` (never rendered), e.g. the per-underlying cap.
    assert REGISTRY["max_alloc_pct"].ref == "D116/D18"
    assert REGISTRY["max_alloc_pct"].description.startswith("Base tier of the per-underlying cap")
    assert sum(1 for t in REGISTRY.values() if t.ref) > 150


def _walk(node: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if k in ("label", "about") and isinstance(v, str):
                out.append((p, v))
            out.extend(_walk(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk(v, f"{path}[{i}]"))
    return out


def test_routines_yaml_labels_and_about_have_no_ids() -> None:
    raw = yaml.safe_load((REPO / "config" / "routines.yaml").read_text())
    found = _walk(raw)
    assert len(found) > 40  # the walk actually reached the jobs' label/about fields
    bad = [(p, v) for p, v in found if _ids(v)]
    assert bad == []


def test_config_api_rows_have_no_ids_and_never_serve_ref(tmp_path: Path) -> None:
    from arc.tower.data_ops import load_config

    conn = sqlite3.connect(tmp_path / "arc.db")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    resp = load_config(conn, ArcSettings(), now=dt.datetime(2026, 10, 16, 12, 0, tzinfo=ET))
    body = resp.model_dump(mode="json")
    assert body["keys"]
    for row in body["keys"]:
        assert "ref" not in row
        assert _ids(row["description"]) == [], row["key"]
    assert body["note"] is None or _ids(body["note"]) == []


def test_slack_config_cards_have_no_ids(tmp_path: Path) -> None:
    """`!arc config <group|key>` renders the same descriptions (Slack)."""
    from arc.control import cards
    from arc.control.registry import Group
    from arc.control.service import ControlService

    conn = sqlite3.connect(tmp_path / "arc.db")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    now = dt.datetime(2026, 10, 16, 12, 0, tzinfo=ET)
    svc = ControlService(conn, base=ArcSettings(), now=lambda: now, is_halted=lambda: False)
    texts = [cards.config_summary(svc.show(), version=svc.version()).text]
    texts += [cards.key_detail(svc.show(g.value)).text for g in Group]
    every_key = svc.keys()
    texts += [cards.key_detail(svc.show(k)).text for k in every_key]
    for text in texts:
        assert _ids(text) == [], text[:200]
    refused = cards.change_card(svc.set("gate_secret", "x", actor="U1", source="slack"))
    assert _ids(refused.text) == []
