"""E20.4 (PLAN D85): every universe setting in config/universe.yaml.

The active cap, tier sizes and floors (were ArcSettings), the D64 carry-over and the
momentum / trending job options (were config/routines.yaml) live in universe.yaml.
Registry keys are unchanged; old override / overlay paths still reach the setting.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from arc.config import MOVED_UNIVERSE_FIELDS, ArcSettings
from arc.control.effective import (
    apply_changes,
    overlay_overrides,
    raw_yaml,
    routines_for_overlay,
)
from arc.control.registry import REGISTRY, Target, lookup, read_raw
from arc.experiments.overlay import arm_config_data
from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.config import (
    DEFAULT_UNIVERSE_CONFIG,
    UniverseConfig,
    extract_legacy_universe,
    legacy_universe_path,
    load_universe_config,
    moved_universe_keys,
    split_universe_overrides,
    universe_config,
)
from arc.universe.tiers import Tier, active_max, tier_floor, tier_floors, tier_size, tier_sizes
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)

if TYPE_CHECKING:
    from pathlib import Path

#: registry key -> (universe.yaml path, shipped value). Pins that the values did not move.
MOVED: dict[str, tuple[tuple[str, ...], Any]] = {
    "universe_active_max": (("active_max",), 50),
    "universe_momentum_size": (("sizes", "momentum"), 20),
    "universe_discovery_size": (("sizes", "discovery"), 25),
    "universe_trending_size": (("sizes", "trending"), 25),
    "universe_floor_core": (("floors", "core"), 0.4),
    "universe_floor_momentum": (("floors", "momentum"), 0.5),
    "universe_floor_discovery": (("floors", "discovery"), 0.6),
    "universe_floor_trending": (("floors", "trending"), 0.6),
    "universe.carryover.enabled": (("carryover", "enabled"), True),
    "universe.carryover.window_h": (("carryover", "window_h"), 48),
    "universe.carryover.window_sessions": (("carryover", "window_sessions"), 1),  # D138
    "universe.carryover.w_today": (("carryover", "w_today"), 0.6),
    "universe.trending.scoring": (("trending", "scoring"), "rank_gain"),
}


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    c.row_factory = sqlite3.Row
    return c


# -- one file, same keys, same values -----------------------------------------------------


@pytest.mark.parametrize("key", sorted(MOVED))
def test_registry_key_targets_universe_yaml_with_the_shipped_value(key: str) -> None:
    path, value = MOVED[key]
    t = REGISTRY[key]
    assert t.target is Target.UNIVERSE and t.path == path and t.field is None
    assert read_raw(t, raw_yaml(Target.UNIVERSE)) == value


def test_shipped_universe_yaml_carries_every_setting() -> None:
    u = load_universe_config(DEFAULT_UNIVERSE_CONFIG)
    assert u.active_max == 50
    assert (u.sizes.momentum, u.sizes.discovery, u.sizes.trending) == (20, 25, 25)
    assert (u.floors.core, u.floors.momentum, u.floors.discovery, u.floors.trending) == (
        0.4,
        0.5,
        0.6,
        0.6,
    )
    assert (u.carryover.enabled, u.carryover.window_h, u.carryover.w_today) == (True, 48, 0.6)
    assert u.carryover.window_sessions == 1  # D138
    assert u.momentum.source_order == ["stockanalysis", "schwab"] and u.momentum.size == 25
    t = u.trending
    assert (t.pool, t.min_inputs_first, t.exclude_market_reference, t.scoring) == (
        40,
        2,
        True,
        "rank_gain",
    )
    assert (t.velocity.smoothing, t.velocity.min_mentions) == (5, 10)


def test_model_defaults_equal_the_shipped_file() -> None:
    """A test fixture with no universe.yaml section gets the same values as production."""
    shipped = load_universe_config(DEFAULT_UNIVERSE_CONFIG)
    bare = UniverseConfig()
    for name in ("active_max", "sizes", "floors", "carryover", "trending"):
        assert getattr(bare, name) == getattr(shipped, name), name
    assert bare.momentum.source_order == shipped.momentum.source_order
    assert bare.momentum.size == shipped.momentum.size


def test_routines_yaml_no_longer_carries_universe_knobs() -> None:
    data = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert moved_universe_keys(data) == []
    r = load_routines()
    assert not hasattr(r, "universe")
    for job, gone in (
        ("universe.trending", ("pool", "min_inputs_first", "scoring", "velocity")),
        ("universe.momentum", ("source_order", "size")),
    ):
        spec = r.jobs()[job][1]
        assert not set(gone) & set(spec.options), job


def test_routines_yaml_with_a_moved_block_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text(
        DEFAULT_ROUTINES_PATH.read_text() + "\nuniverse:\n  carryover:\n    enabled: false\n"
    )
    with pytest.raises(ValueError, match="moved to config/universe.yaml"):
        load_routines(p)


@pytest.mark.parametrize(
    ("job", "key", "value"),
    [
        ("universe.trending", "pool", 30),
        ("universe.trending", "scoring", "velocity"),
        ("universe.momentum", "source_order", ["schwab"]),
        ("universe.momentum", "size", 30),
    ],
)
def test_routines_job_option_that_moved_is_refused(
    tmp_path: Path, job: str, key: str, value: Any
) -> None:
    data = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    data["sources"][job][key] = value
    p = tmp_path / "routines.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ValueError, match=rf"sources\.{job}\.{key} moved"):
        load_routines(p)


@pytest.mark.parametrize("field", sorted(MOVED_UNIVERSE_FIELDS))
def test_arcsettings_refuses_a_moved_field(field: str) -> None:
    with pytest.raises(ValueError, match="moved to config/universe.yaml"):
        _settings(**{field: 1})


def test_universe_mode_stays_an_env_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """D9: `ARC_UNIVERSE_MODE=strict` is documented as an env switch; it did not move."""
    monkeypatch.setenv("ARC_UNIVERSE_MODE", "strict")
    assert _settings().universe_mode.value == "strict"
    assert REGISTRY["universe_mode"].target is Target.SETTINGS


# -- readers -------------------------------------------------------------------------------


def test_readers_follow_the_d26_overrides() -> None:
    s = _settings().with_yaml_overrides(
        "universe",
        {
            ("active_max",): 30,
            ("sizes", "trending"): 10,
            ("floors", "discovery"): 0.7,
        },
    )
    assert active_max(s) == 30
    assert tier_size(s, Tier.TRENDING) == 10 and tier_sizes(s)[Tier.MOMENTUM] == 20
    assert tier_floor(s, Tier.DISCOVERY) == 0.7 and tier_floor(s, None) is None
    assert tier_floors(_settings())[Tier.CORE] == 0.4


def test_slack_override_on_a_moved_key_reaches_universe_yaml() -> None:
    """A stored config_changes row on the unchanged key lands on the universe target."""
    from arc.control.store import ConfigChange

    def change(i: int, key: str, new: object) -> ConfigChange:
        return ConfigChange.model_validate(
            {
                "id": i,
                "key": key,
                "new": new,
                "status": "applied",
                "actor": "U1",
                "source": "cli",
                "direction": "n/a",
                "at": "2026-10-09T13:00:00Z",
            }
        )

    changes = {
        "universe_floor_trending": change(1, "universe_floor_trending", 0.7),
        "universe.carryover.enabled": change(2, "universe.carryover.enabled", False),
        "universe.trending.scoring": change(3, "universe.trending.scoring", "velocity"),
        "universe_active_max": change(4, "universe_active_max", 40),
    }
    s = apply_changes(_settings(), changes, version=4)
    u = universe_config(s)
    assert u.floors.trending == 0.7 and u.carryover.enabled is False
    assert u.trending.scoring == "velocity" and u.active_max == 40
    assert s.yaml_overrides("routines") == {}


# -- old paths (overrides, experiment overlays) ---------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (("universe", "carryover", "enabled"), ("carryover", "enabled")),
        (("sources", "universe.trending", "scoring"), ("trending", "scoring")),
        (
            ("sources", "universe.trending", "velocity", "smoothing"),
            ("trending", "velocity", "smoothing"),
        ),
        (("sources", "universe.momentum", "size"), ("momentum", "size")),
        (("sources", "universe.trending", "schedule"), None),  # job IO stays in routines
        (("sources", "universe.momentum", "writes"), None),
        (("loop", "max_idle"), None),
    ],
)
def test_legacy_universe_path(old: tuple[str, ...], new: tuple[str, ...] | None) -> None:
    assert legacy_universe_path(old) == new


def test_split_universe_overrides() -> None:
    r, u = split_universe_overrides(
        {("loop", "max_idle"): "5m", ("universe", "carryover", "w_today"): 0.7}
    )
    assert r == {("loop", "max_idle"): "5m"} and u == {("carryover", "w_today"): 0.7}
    assert split_universe_overrides(None) == ({}, {})


def test_old_routines_overlay_maps_to_the_universe_target() -> None:
    ov = overlay_overrides(
        {
            "routines": {
                "universe": {"carryover": {"enabled": False}},
                "sources": {"universe.trending": {"scoring": "velocity"}},
                "loop": {"max_idle": "5m"},
            },
            "universe": {"carryover": {"enabled": True}},  # explicit new path wins
        }
    )
    assert ov["routines"] == {("loop", "max_idle"): "5m"}
    assert ov["universe"] == {
        ("carryover", "enabled"): True,
        ("trending", "scoring"): "velocity",
    }
    only_old = overlay_overrides({"routines": {"universe": {"carryover": {"w_today": 0.8}}}})
    assert only_old == {"universe": {("carryover", "w_today"): 0.8}}


def test_old_overlay_reaches_the_arm_settings_and_routines(conn: sqlite3.Connection) -> None:
    overlay = {"routines": {"universe": {"carryover": {"enabled": False}}}}
    s = apply_changes(_settings(), {}, version=0, overlay=overlay)
    assert universe_config(s).carryover.enabled is False
    routines_for_overlay(conn, overlay)  # the routines side loads (no refused path)


def test_extract_legacy_universe() -> None:
    data = {
        "universe": {"carryover": {"enabled": False}},
        "sources": {
            "universe.trending": {"scoring": "velocity", "schedule": ["05:50"]},
            "universe.momentum": {"size": 30},
            "other": {"pool": 1},
        },
    }
    rest, moved = extract_legacy_universe(data)
    assert "universe" not in rest
    assert rest["sources"]["universe.trending"] == {"schedule": ["05:50"]}
    assert rest["sources"]["universe.momentum"] == {}
    assert rest["sources"]["other"] == {"pool": 1}
    assert moved == {
        ("carryover", "enabled"): False,
        ("trending", "scoring"): "velocity",
        ("momentum", "size"): 30,
    }
    assert data["sources"]["universe.trending"]["scoring"] == "velocity"  # input untouched


class _Arm:
    def __init__(self, overlay: dict[str, Any]) -> None:
        self.overlay = overlay


class _Spec:
    """The two attributes arm_config_data reads (spec.arms.arm(name).overlay)."""

    def __init__(self, overlay: dict[str, Any]) -> None:
        self.arms = self
        self._arm = _Arm(overlay)

    def arm(self, _name: str) -> _Arm:
        return self._arm


def _validate(overlay: dict[str, Any]) -> None:
    for target in overlay:
        arm_config_data(_Spec(overlay), "t1", target)  # type: ignore[arg-type]


def test_experiment_overlay_validation_checks_old_and_new_universe_leaves() -> None:
    _validate({"routines": {"universe": {"carryover": {"enabled": False}}}})
    _validate({"routines": {"sources": {"universe.trending": {"scoring": "velocity"}}}})
    _validate({"universe": {"trending": {"scoring": "velocity"}}})
    with pytest.raises(ValueError):
        _validate({"routines": {"universe": {"carryover": {"w_today": 0.1}}}})
    with pytest.raises(ValueError):
        _validate({"universe": {"sizes": {"trending": 99}}})


def test_lookup_aliases_unchanged() -> None:
    assert lookup("carryover.w_today").key == "universe.carryover.w_today"
    assert lookup("universe_momentum_size_d56").key == "universe_momentum_size"
    assert lookup("scalp_min_confidence").key == "universe_floor_core"


#: key -> (value set through Slack, check on the effective universe config)
SET_CASES: dict[str, tuple[str, Any]] = {
    "universe_active_max": ("40", lambda u: u.active_max == 40),
    "universe_momentum_size": ("15", lambda u: u.sizes.momentum == 15),
    "universe_discovery_size": ("20", lambda u: u.sizes.discovery == 20),
    "universe_trending_size": ("20", lambda u: u.sizes.trending == 20),
    "universe_floor_core": ("0.5", lambda u: u.floors.core == 0.5),
    "universe_floor_momentum": ("0.6", lambda u: u.floors.momentum == 0.6),
    "universe_floor_discovery": ("0.7", lambda u: u.floors.discovery == 0.7),
    "universe_floor_trending": ("0.7", lambda u: u.floors.trending == 0.7),
    "universe.carryover.enabled": ("off", lambda u: u.carryover.enabled is False),
    "universe.carryover.window_h": ("72", lambda u: u.carryover.window_h == 72),
    "universe.carryover.window_sessions": ("2", lambda u: u.carryover.window_sessions == 2),
    "universe.carryover.w_today": ("0.8", lambda u: u.carryover.w_today == 0.8),
    "universe.trending.scoring": ("velocity", lambda u: u.trending.scoring == "velocity"),
}


def test_set_cases_cover_every_moved_key() -> None:
    assert set(SET_CASES) == set(MOVED)


@pytest.mark.parametrize("key", sorted(SET_CASES))
def test_slack_set_on_a_moved_key_reaches_universe_yaml(conn: sqlite3.Connection, key: str) -> None:
    """Per key: a `!arc config set` stored in config_changes applies at the new path."""
    from arc.control.effective import effective_settings
    from arc.control.service import ControlService

    owner = "U0OWNER"
    base = _settings(approver_slack_user_ids=[owner])
    svc = ControlService(conn, base=base, now=lambda: NOW, is_halted=lambda: False)
    value, check = SET_CASES[key]
    before = svc.view(key).value
    r = svc.set(key, value, actor=owner, source="slack")
    if r.pending is not None:
        r = svc.confirm(r.pending.code, actor=owner, source="slack")
    assert r.outcome == "applied", (key, r)
    assert svc.view(key).value != before
    eff = effective_settings(conn, base=base)
    assert check(universe_config(eff)), key
    assert eff.yaml_overrides("routines") == {}
