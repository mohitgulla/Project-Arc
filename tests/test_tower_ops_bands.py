"""E8.8d: Session Timeline bands + persona chips are config-driven (routines.yaml)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest
import yaml

from arc.context.categories import CATEGORY_ORDER, SourceCategory
from arc.routines.config import (
    ABOUT_MAX_CHARS,
    DEFAULT_ROUTINES_PATH,
    TIMELINE_GROUPS,
    TIMELINE_PERSONAS,
    load_routines,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.data import connect_ro
from arc.tower.data_ops import load_session
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 9, 30, 13, 42, tzinfo=ET)
TODAY = NOW.date()


@pytest.fixture
def empty_db(tmp_path: Path):
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    c.close()
    ro = connect_ro(db)
    yield ro
    ro.close()


def _yaml() -> dict:
    return yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "routines.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_live_config_bands_in_owner_order(empty_db) -> None:
    s = load_session(empty_db, load_routines(), now=NOW, day=TODAY)
    keys = [b.key for b in s.bands]
    groups = [g for g, _ in TIMELINE_GROUPS]
    # bands follow TIMELINE_GROUPS, the sources sub-bands follow D47 CATEGORY_ORDER
    assert [b.group for b in s.bands] == sorted((b.group for b in s.bands), key=groups.index)
    # D56: reference data gets its own band after the six categories
    assert keys[: keys.index("scalp")] == [
        f"sources.{c.value}" for c in CATEGORY_ORDER if f"sources.{c.value}" in keys
    ] + ["sources.reference"]
    labels = {b.key: b.label for b in s.bands}
    routines = load_routines()
    for c in CATEGORY_ORDER:  # labels come from categories.<c>.label (D47/D49 rename-safe)
        if f"sources.{c.value}" in labels:
            assert labels[f"sources.{c.value}"] == routines.category_spec(c).label
    assert labels["sources.reference"] == "Reference data"
    assert labels["trading_loop"] == "Trading loop"
    # the loop row is in its band, labelled with its chain
    assert s.loop is not None and s.loop.band == "trading_loop"
    assert s.loop.label == "Research → Quant → Risk → Propose → Execute"
    assert s.loop.persona == "research"
    rows = {r.job: r for r in [*s.rows, s.loop]}
    assert rows["positions.evaluate"].label == "Investor exits"
    assert rows["positions.evaluate"].band == "position_management"
    assert rows["auditor"].band == "post_market" and rows["auditor"].persona == "auditor"
    assert rows["rss"].band == f"sources.{CATEGORY_ORDER[0].value}" and rows["rss"].persona is None
    assert rows["rss"].categories[0] == SourceCategory.MARKET_NEWS.value
    assert rows["youtube.briefs"].band.startswith("sources.") and rows["youtube.briefs"].llm
    assert rows["scalp"].band == "scalp" and rows["scalp"].llm
    for job in ("earnings", "macro_calendar", "ex_dividend", "iv.record"):
        assert rows[job].band == "sources.reference", job
    # every band's jobs are exactly the rows tagged with it
    for b in s.bands:
        assert b.jobs == [j for j in rows if rows[j].band == b.key] or set(b.jobs) == {
            j for j in rows if rows[j].band == b.key
        }


def test_every_scheduled_live_job_has_label_and_about(empty_db) -> None:
    """Acceptance: every current job is filled in (no job falls back to its key)."""
    s = load_session(empty_db, load_routines(), now=NOW, day=TODAY)
    for r in [*s.rows, s.loop]:
        assert r is not None
        assert r.label != r.job or r.job in {"scalp", "research"}, r.job
        assert r.about, r.job
        # universe maintenance (symbols, E12.2 momentum, E12.3 trending) sits in "other"
        assert r.band != "other" or r.job == "symbols" or r.job.startswith("universe."), r.job


def test_new_job_in_temp_yaml_lands_in_its_band(tmp_path, empty_db) -> None:
    """No code change: a new source joins its D47 category band, a new persona its group."""
    data = _yaml()
    data["sources"]["cboe.skew"] = {
        "schedule": ["09:05"],
        "days": "daily",
        "category": "options_slow",
        "label": "Cboe SKEW",
        "about": "Daily SKEW index close",
        "writes": [],
    }
    data["personas"]["risk.review"] = {
        "schedule": ["12:00"],
        "days": "daily",
        "group": "position_management",
        "persona": "risk",
        "label": "Midday risk review",
        "llm": False,
        "handler": "arc.routines.handlers:noop",
    }
    data["personas"]["mystery"] = {"schedule": ["13:00"], "days": "daily", "llm": False}
    cfg = load_routines(_write(tmp_path, data))
    s = load_session(empty_db, cfg, now=NOW, day=TODAY)
    rows = {r.job: r for r in s.rows}
    assert rows["cboe.skew"].band == "sources.options_slow"
    assert rows["cboe.skew"].label == "Cboe SKEW" and rows["cboe.skew"].persona is None
    assert rows["risk.review"].band == "position_management"
    assert rows["risk.review"].persona == "risk"
    assert rows["mystery"].band == "other" and rows["mystery"].label == "mystery"
    assert rows["mystery"].persona is None
    assert s.bands[-1].key == "other" and "mystery" in s.bands[-1].jobs
    band = next(b for b in s.bands if b.key == "sources.options_slow")
    assert "cboe.skew" in band.jobs


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("group", "sourcez", "unknown group"),
        ("persona", "quant_guru", "unknown persona"),
        ("about", "x" * (ABOUT_MAX_CHARS + 1), "one line"),
        ("label", "", "non-empty"),
    ],
)
def test_display_keys_are_validated(tmp_path, key, value, match) -> None:
    data = _yaml()
    data["personas"]["auditor"][key] = value
    with pytest.raises(ValueError, match=match):
        load_routines(_write(tmp_path, data))


def test_persona_choices_cover_the_chips() -> None:
    assert set(TIMELINE_PERSONAS) == {"scalp", "research", "investor", "risk", "auditor", "monitor"}
