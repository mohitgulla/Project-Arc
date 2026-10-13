"""E20.3 (PLAN D85): config/strategy.yaml in the strategy lane, switch + settings in one block.

Covers: the moved blocks load from strategy.yaml and nowhere else; a routines.yaml that
still carries one is refused; every registry key still resolves and every moved key's
Slack override reaches the consuming config; pre-E20.3 override / overlay paths map to
the new location; the registered XP-1 spec hash is unchanged; the lane lists the file.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.context.ttl import to_db
from arc.control.effective import (
    YAML_PATHS,
    effective_routines,
    effective_settings,
    overlay_overrides,
    raw_yaml,
    routines_for_overlay,
)
from arc.control.registry import REGISTRY, Target, lookup, read_raw
from arc.control.service import ControlService
from arc.experiments.models import OVERLAY_TARGETS, spec_hash
from arc.experiments.overlay import LIVE_SPECS_DIR, arm_config_data, load_spec, target_path
from arc.experiments.runner import STEP_TARGETS, arm_owned_personas
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.routines.strategy import (
    DEFAULT_STRATEGY_PATH,
    MOVED_BLOCKS,
    StrategyConfig,
    extract_legacy,
    legacy_path,
    moved_keys,
    split_overrides,
    strategy_path_for,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import experiment_fixtures as fx

REPO = Path(__file__).resolve().parent.parent
OWNER = "U0OWNER"
NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=ET)

# The strategy keys that moved, with a non-default value to set and a reader of the
# consuming config (the RoutinesConfig field the personas / pipeline steps read).
MOVED_KEYS: dict[str, tuple[str, Any]] = {
    "personas.finnhub_context": ("off", lambda r: r.finnhub_context.enabled is False),
    "personas.scalp_movers_context": ("on", lambda r: r.scalp_movers_context.enabled),
    "personas.director_diversification": (
        "strict",
        lambda r: r.director_diversification.mode == "strict",
    ),
    "personas.scout_buzz_velocity": ("on", lambda r: r.scout_buzz_velocity.enabled),
    "personas.retail_sentiment_context": ("on", lambda r: r.retail_sentiment_context.enabled),
    "personas.research_technicals": ("off", lambda r: r.research_technicals.enabled is False),
    "personas.anti_chase": ("off", lambda r: r.anti_chase.enabled is False),
    "personas.market_health_context": (
        "off",
        lambda r: r.market_health_context.enabled is False,
    ),
    "anti_chase.combine": ("any", lambda r: r.anti_chase.combine == "any"),
    "anti_chase.max_stretch_atr": ("3", lambda r: r.anti_chase.max_stretch_atr == 3.0),
    "anti_chase.rsi_overbought": ("80", lambda r: r.anti_chase.rsi_overbought == 80.0),
    "anti_chase.rsi_oversold": ("20", lambda r: r.anti_chase.rsi_oversold == 20.0),
    "anti_chase.max_dist_high20_atr": ("0.5", lambda r: r.anti_chase.max_dist_high20_atr == 0.5),
    "anti_chase.vwap": ("on", lambda r: r.anti_chase.vwap is True),
    "anti_chase.max_vwap_stretch_atr": ("1", lambda r: r.anti_chase.max_vwap_stretch_atr == 1.0),
    "market_health.breadth_weak": ("0.35", lambda r: r.market_health.breadth_weak == 0.35),
    "market_health.max_lag_sessions": ("3", lambda r: r.market_health.max_lag_sessions == 3),
    "funnel.scout.max_discovery": ("20", lambda r: r.funnel.scout.max_discovery == 20),
    "funnel.scout.min_discovery_alert": (
        "3",
        lambda r: r.funnel.scout.min_discovery_alert == 3,
    ),
    "funnel.research.max_scout_only_ideas": (
        "10",
        lambda r: r.funnel.research.max_scout_only_ideas == 10,
    ),
}


def _svc(conn: sqlite3.Connection) -> ControlService:
    base = ArcSettings(_env_file=None, approver_slack_user_ids=[OWNER])  # type: ignore[call-arg]
    return ControlService(conn, base=base, now=lambda: NOW, is_halted=lambda: False)


def _set(svc: ControlService, key: str, value: str) -> None:
    r = svc.set(key, value, actor=OWNER, source="slack")
    if r.pending is not None:
        r = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert r.outcome == "applied", (key, r)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    c.row_factory = sqlite3.Row
    return c


# -- layout ---------------------------------------------------------------------


def test_routines_yaml_keeps_no_strategy_block() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert moved_keys(raw) == []
    assert not set(MOVED_BLOCKS) & set(raw)


def test_strategy_yaml_holds_switch_and_settings_in_one_block() -> None:
    raw = yaml.safe_load(DEFAULT_STRATEGY_PATH.read_text())
    for block in (
        "finnhub_context",
        "scalp_movers_context",
        "scout_buzz_velocity",
        "retail_sentiment_context",
        "research_technicals",
        "anti_chase",
        "market_health_context",
    ):
        assert isinstance(raw[block]["enabled"], bool), block
    assert raw["director_diversification"]["mode"] == "relaxed"
    assert raw["anti_chase"]["max_stretch_atr"] == 2.5  # knobs next to the switch
    assert "thresholds" in raw["market_health_context"]
    assert set(raw) <= set(StrategyConfig.model_fields)
    StrategyConfig.model_validate(raw)


def test_shipped_effective_values_unchanged() -> None:
    # The D85 shipped values, now read from strategy.yaml (byte-identical behaviour).
    r = load_routines()
    assert r.finnhub_context.enabled and r.research_technicals.enabled
    assert r.anti_chase.enabled and r.market_health_context.enabled
    assert not r.scalp_movers_context.enabled
    assert not r.scout_buzz_velocity.enabled and not r.retail_sentiment_context.enabled
    assert r.director_diversification.mode == "relaxed"
    assert r.anti_chase.combine == "all" and r.anti_chase.vwap is False
    assert r.market_health.breadth_weak == 0.4
    assert r.funnel.scout.max_discovery == 25
    assert r.funnel.research.max_scout_only_ideas == 20


def test_strategy_model_is_strict() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        StrategyConfig.model_validate({"anti_chase": {"enabled": True, "nope": 1}})
    with pytest.raises(ValueError, match="Extra inputs"):
        StrategyConfig.model_validate({"routines_knob": 1})
    with pytest.raises(ValueError, match="less than or equal"):
        StrategyConfig.model_validate({"anti_chase": {"max_stretch_atr": 99}})


def test_missing_sibling_strategy_file_means_model_defaults(tmp_path: Path) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text("timezone: America/New_York\n")
    r = load_routines(p)
    assert r.anti_chase.enabled is False  # every feature's control (rollback) value
    assert r.director_diversification.mode == "strict"
    assert strategy_path_for(p) == tmp_path / "strategy.yaml"
    assert strategy_path_for(None) == DEFAULT_STRATEGY_PATH


def test_sibling_strategy_file_is_read(tmp_path: Path) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text("timezone: America/New_York\n")
    (tmp_path / "strategy.yaml").write_text("anti_chase: {enabled: on, max_stretch_atr: 3}\n")
    r = load_routines(p)
    assert r.anti_chase.enabled is True and r.anti_chase.max_stretch_atr == 3.0


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ('personas:\n  anti_chase: "on"\n', "personas.anti_chase"),
        ("personas:\n  director_diversification: relaxed\n", "personas.director_diversification"),
        ("anti_chase:\n  combine: all\n", "anti_chase"),
        ("market_health:\n  breadth_weak: 0.4\n", "market_health"),
        ("funnel:\n  research: {max_scout_only_ideas: 20}\n", "funnel"),
        ("finnhub_context:\n  scalp_max_tickers: 8\n", "finnhub_context"),
    ],
)
def test_old_routines_block_is_refused_with_a_pointer(
    tmp_path: Path, extra: str, named: str
) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text("timezone: America/New_York\n" + extra)
    with pytest.raises(ValueError, match=r"moved to config/strategy\.yaml") as exc:
        load_routines(p)
    assert named in str(exc.value)


def test_routines_validate_cli_ok() -> None:
    import argparse

    from arc.routines.cli import _load

    assert isinstance(_load(argparse.Namespace(config=None)), RoutinesConfig)


# -- registry / Slack keys ------------------------------------------------------


def test_every_registry_key_resolves_and_reads() -> None:
    raws = {t: raw_yaml(t) for t in YAML_PATHS}
    for key, t in REGISTRY.items():
        assert lookup(key) is t
        for alias in t.aliases:
            assert lookup(alias) is t, alias
        if t.target is not Target.SETTINGS:
            read_raw(t, raws[t.target])  # the path exists in its file's shape


def test_moved_keys_target_strategy_and_keep_their_names() -> None:
    for key in MOVED_KEYS:
        t = lookup(key)
        assert t.key == key
        assert t.target is Target.STRATEGY, key
    for alias in ("anti_chase", "routines.personas.anti_chase", "finnhub_context"):
        assert lookup(alias).target is Target.STRATEGY
    # Non-strategy routines keys stay on routines.yaml.
    for key in ("loop.max_idle", "monitoring.coverage_min"):
        assert lookup(key).target is Target.ROUTINES
    # E20.4 (D85): the trending ranker knob moved to universe.yaml, same key.
    assert lookup("universe.trending.scoring").target is Target.UNIVERSE


@pytest.mark.parametrize("key", sorted(MOVED_KEYS))
def test_slack_override_reaches_the_consuming_config(conn: sqlite3.Connection, key: str) -> None:
    value, check = MOVED_KEYS[key]
    svc = _svc(conn)
    before = svc.view(key).value
    _set(svc, key, value)
    assert svc.view(key).value != before
    assert check(effective_routines(conn)), key
    # strategy overrides never leak into the settings' YAML overrides
    s = effective_settings(conn)
    assert s.yaml_overrides("routines") in (None, {})


def test_stored_config_change_row_still_applies(conn: sqlite3.Connection) -> None:
    # A row written before E20.3 (same key, same stored value) applies unchanged.
    conn.execute(
        "insert into config_changes (key, old, new, actor, at, source, status, direction) "
        "values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "personas.anti_chase",
            '"on"',
            '"off"',
            OWNER,
            to_db(NOW),
            "slack",
            "applied",
            "riskier",
        ),
    )
    conn.commit()
    assert effective_routines(conn).anti_chase.enabled is False
    assert _svc(conn).view("personas.anti_chase").value == "off"


def test_market_health_job_keys_read_the_job_not_the_thresholds(
    conn: sqlite3.Connection,
) -> None:
    # Before E20.3 the E16.4 `market_health:` thresholds block shadowed the job's own
    # keys (`routines.market_health.enabled` showed none, `.cadence` a threshold dict).
    svc = _svc(conn)
    assert svc.view("routines.market_health.enabled").value is True
    assert svc.view("routines.market_health.cadence").value.startswith("at ")
    assert svc.view("market_health.breadth_weak").value == 0.4


def test_config_show_groups_unchanged(conn: sqlite3.Connection) -> None:
    views = _svc(conn).show("routines")
    keys = [v.tunable.key for v in views]
    for key in MOVED_KEYS:
        assert key in keys, key
    assert all(v.tunable.group.value == "routines" for v in views)


# -- legacy paths (D26 overrides, experiment overlays) --------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (("personas", "anti_chase"), ("anti_chase", "enabled")),
        (("personas", "director_diversification"), ("director_diversification", "mode")),
        (
            ("market_health", "breadth_weak"),
            ("market_health_context", "thresholds", "breadth_weak"),
        ),
        (("anti_chase", "vwap"), ("anti_chase", "vwap")),
        (("funnel", "scout", "max_discovery"), ("funnel", "scout", "max_discovery")),
        (("personas", "scout"), None),  # a job, not a switch
        (("personas", "scout", "schedule"), None),
        (("loop", "max_idle"), None),
        (("sources", "universe.trending", "scoring"), None),
    ],
)
def test_legacy_path(old: tuple[str, ...], new: tuple[str, ...] | None) -> None:
    assert legacy_path(old) == new


def test_old_override_paths_map_to_strategy() -> None:
    r, s = split_overrides({("personas", "anti_chase"): "off", ("loop", "max_idle"): 5})
    assert r == {("loop", "max_idle"): 5}
    assert s == {("anti_chase", "enabled"): "off"}
    cfg = load_routines(
        overrides={("personas", "anti_chase"): "off", ("market_health", "pc_fear_pct"): 0.95}
    )
    assert cfg.anti_chase.enabled is False and cfg.market_health.pc_fear_pct == 0.95


def test_extract_legacy_keeps_jobs() -> None:
    data = {"personas": {"anti_chase": "off", "scout": {"label": "x"}}, "loop": {}}
    rest, ov = extract_legacy(data)
    assert rest == {"personas": {"scout": {"label": "x"}}, "loop": {}}
    assert ov == {("anti_chase", "enabled"): "off"}
    assert data["personas"]["anti_chase"] == "off"  # input not mutated


@pytest.mark.parametrize(
    "overlay",
    [
        {"routines": {"personas": {"anti_chase": "off"}}},  # pre-E20.3 shape
        {"strategy": {"anti_chase": {"enabled": False}}},  # E20.3 shape
    ],
)
def test_arm_overlay_old_and_new_paths(conn: sqlite3.Connection, overlay: dict) -> None:
    spec = fx.spec("XP-30", arms={"treatment": {"overlay": overlay}})
    target = next(iter(overlay))
    arm_config_data(spec, "treatment", target)  # validates
    arm = routines_for_overlay(conn, overlay)
    assert arm.anti_chase.enabled is False
    base = load_routines()
    assert arm.model_copy(update={"anti_chase": base.anti_chase}) == base


def test_bad_overlay_still_fails_validation() -> None:
    spec = fx.spec(
        "XP-31", arms={"treatment": {"overlay": {"strategy": {"anti_chase": {"nope": 1}}}}}
    )
    with pytest.raises(ValueError, match="Extra inputs"):
        arm_config_data(spec, "treatment", "strategy")
    old = fx.spec(
        "XP-32",
        arms={"treatment": {"overlay": {"routines": {"personas": {"anti_chase": "maybe"}}}}},
    )
    with pytest.raises(ValueError, match="on | off"):
        arm_config_data(old, "treatment", "routines")


def test_strategy_is_an_overlay_target_and_a_research_input() -> None:
    assert "strategy" in OVERLAY_TARGETS
    assert target_path("strategy") == DEFAULT_STRATEGY_PATH
    assert "strategy" in STEP_TARGETS["research"]
    assert arm_owned_personas({"strategy": {"scalp_movers_context": {"enabled": True}}}) == {
        "scalp"
    }
    assert arm_owned_personas({"routines": {"personas": {"scalp_movers_context": "on"}}}) == {
        "scalp"
    }
    assert "scout" in arm_owned_personas({"strategy": {"funnel": {"scout": {"max_discovery": 5}}}})


def test_registered_spec_hashes_unchanged() -> None:
    # XP-1 (registered A/A) and the owner's K=4 A/A XP-12 (E15.9, D107) are the only
    # committed specs (D86); their hashes must not move.
    specs = {p.name: load_spec(p) for p in sorted(LIVE_SPECS_DIR.glob("*.yaml"))}
    assert set(specs) == {"xp1_aa_baseline.yaml", "xp12_aa_k4.yaml"}
    assert (
        spec_hash(specs["xp1_aa_baseline.yaml"])
        == "7d02a85573156876e3b8d0a4e57e195d9ce04f6399966103843224d8749d8d20"
    )
    assert (
        spec_hash(specs["xp12_aa_k4.yaml"])
        == "7fc1cebb559f584d174d954eb0ad777641c6a1be6944a48adeaa4141b3d2c566"
    )


# -- strategy lane ----------------------------------------------------------------


def test_strategy_yaml_is_in_the_strategy_lane() -> None:
    lane = yaml.safe_load((REPO / "config" / "strategy_lane.yaml").read_text())
    assert "config/strategy.yaml" in lane["strategy_paths"]


def test_effective_overlay_flattens_strategy() -> None:
    ov = overlay_overrides({"strategy": {"anti_chase": {"enabled": False}}})
    assert ov == {"strategy": {("anti_chase", "enabled"): False}}
