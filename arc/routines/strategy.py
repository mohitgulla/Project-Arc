"""``config/strategy.yaml``: the strategy knobs of the routines (E20.3, PLAN D85).

``routines.yaml`` says *when and what* runs (schedules, sources, jobs, steps, context
TTLs, categories, monitoring, loop, tower). The trading-behaviour knobs the personas
and pipeline steps read live here instead, so they sit in the strategy lane
(``config/strategy_lane.yaml``). Each feature's on/off switch and its settings are one
block (``anti_chase: {enabled: on, combine: all, …}``).

:func:`arc.routines.config.load_routines` reads this file next to the routines file
(``<dir>/strategy.yaml``; a routines file with no sibling uses the model defaults =
every feature off) and folds it into :class:`~arc.routines.config.RoutinesConfig`, so
consumers keep reading ``ctx.routines.anti_chase`` etc. unchanged.

Compatibility: the pre-E20.3 routines paths (``personas.anti_chase``,
``anti_chase.max_stretch_atr``, ``market_health.breadth_weak``, ``funnel.…``) are
mapped by :func:`legacy_path`, so old experiment overlays (``routines: personas: …``)
and old override paths still reach the same setting. Registry keys are unchanged
(their path points here, :class:`arc.control.registry.Target` ``STRATEGY``). A
routines.yaml that still carries a moved block is refused with a pointer to this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from arc.features.market_health import HealthThresholds
from arc.routines.config import (
    AntiChaseSettings,
    FinnhubContextSettings,
    FunnelConfig,
    MarketHealthContextSettings,
    OiWallsSettings,
    PullbackWatchSettings,
    RangeEdgesSettings,
    ResearchDiversificationSettings,
    ResearchTechnicalsSettings,
    RetailSentimentContextSettings,
    ScalpMoversContextSettings,
    ScannerStrategySettings,
    ScoutBuzzVelocitySettings,
    StrikeLevelsSettings,
    VolumeProfileSettings,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_STRATEGY_PATH",
    "LEGACY_CHOICES",
    "LEGACY_SWITCHES",
    "MOVED_BLOCKS",
    "MarketHealthStrategy",
    "StrategyConfig",
    "legacy_path",
    "load_strategy_data",
    "moved_keys",
    "split_overrides",
    "strategy_path_for",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STRATEGY_PATH = REPO_ROOT / "config" / "strategy.yaml"

#: Pre-E20.3 ``personas.<flag>: on | off`` switches -> ``<flag>.enabled``.
LEGACY_SWITCHES: tuple[str, ...] = (
    "finnhub_context",
    "scalp_movers_context",
    "scout_buzz_velocity",
    "retail_sentiment_context",
    "research_technicals",
    "anti_chase",
    "market_health_context",
)
#: Pre-E20.3 ``personas.<flag>: <choice>`` switches -> ``<flag>.mode``.
LEGACY_CHOICES: tuple[str, ...] = ("director_diversification",)
#: Top-level routines.yaml blocks that moved here (``market_health`` became
#: ``market_health_context.thresholds``; ``technicals`` never shipped a block but is
#: refused too, so a stale local copy cannot silently configure nothing).
MOVED_BLOCKS: tuple[str, ...] = (
    *LEGACY_SWITCHES,
    *LEGACY_CHOICES,
    "market_health",
    "technicals",
    "funnel",
    "volume_profile",  # E23.0 (D98): born in strategy.yaml; refused in routines.yaml
    "oi_walls",  # E23.0 (D98)
    "range_edges",  # E23.1 (D98)
    "pullback_watch",  # E23.2 (D98)
    "strike_levels",  # E23.3 (D98)
    "scanner",  # E2.6 (D116): scanner.liquidity_rank
)


class MarketHealthStrategy(MarketHealthContextSettings):
    """E16.4 (D76/D83): the Research ``Market health`` line switch + its label thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thresholds: HealthThresholds = Field(default_factory=HealthThresholds)


class StrategyConfig(BaseModel):
    """Top-level ``config/strategy.yaml`` (every block optional; absent = model default)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finnhub_context: FinnhubContextSettings = Field(default_factory=FinnhubContextSettings)
    scalp_movers_context: ScalpMoversContextSettings = Field(
        default_factory=ScalpMoversContextSettings
    )
    scout_buzz_velocity: ScoutBuzzVelocitySettings = Field(
        default_factory=ScoutBuzzVelocitySettings
    )
    retail_sentiment_context: RetailSentimentContextSettings = Field(
        default_factory=RetailSentimentContextSettings
    )
    research_technicals: ResearchTechnicalsSettings = Field(
        default_factory=ResearchTechnicalsSettings
    )
    anti_chase: AntiChaseSettings = Field(default_factory=AntiChaseSettings)
    market_health_context: MarketHealthStrategy = Field(default_factory=MarketHealthStrategy)
    director_diversification: ResearchDiversificationSettings = Field(
        default_factory=ResearchDiversificationSettings
    )
    funnel: FunnelConfig = Field(default_factory=FunnelConfig)
    # E23.0 (D98): levels on regime technicals (the pre-market `levels` job)
    volume_profile: VolumeProfileSettings = Field(default_factory=VolumeProfileSettings)
    oi_walls: OiWallsSettings = Field(default_factory=OiWallsSettings)
    # E23.1 (D98): the range-edge entry filter
    range_edges: RangeEdgesSettings = Field(default_factory=RangeEdgesSettings)
    # E23.2 (D98): the pullback watch for anti-chase drops
    pullback_watch: PullbackWatchSettings = Field(default_factory=PullbackWatchSettings)
    # E23.3 (D98): strike vs level context on the Quant menu and trade card
    strike_levels: StrikeLevelsSettings = Field(default_factory=StrikeLevelsSettings)
    # E2.6 (D116): liquidity-aware strike / expiry choice in the scanner
    scanner: ScannerStrategySettings = Field(default_factory=ScannerStrategySettings)

    def routines_fields(self) -> dict[str, Any]:
        """The :class:`~arc.routines.config.RoutinesConfig` fields this file sets."""
        mh = self.market_health_context
        return {
            "finnhub_context": self.finnhub_context,
            "scalp_movers_context": self.scalp_movers_context,
            "scout_buzz_velocity": self.scout_buzz_velocity,
            "retail_sentiment_context": self.retail_sentiment_context,
            "research_technicals": self.research_technicals,
            "anti_chase": self.anti_chase,
            "market_health_context": MarketHealthContextSettings(enabled=mh.enabled),
            "market_health": mh.thresholds,
            "director_diversification": self.director_diversification,
            "funnel": self.funnel,
            "volume_profile": self.volume_profile,
            "oi_walls": self.oi_walls,
            "range_edges": self.range_edges,
            "pullback_watch": self.pullback_watch,
            "strike_levels": self.strike_levels,
            "scanner": self.scanner,
        }


def strategy_path_for(routines_path: Path | str | None) -> Path:
    """The strategy file read with *routines_path*: its sibling ``strategy.yaml``."""
    if routines_path is None:
        return DEFAULT_STRATEGY_PATH
    return Path(routines_path).with_name("strategy.yaml")


def load_strategy_data(path: Path | str | None) -> dict[str, Any]:
    """Raw ``strategy.yaml`` data (``{}`` when the file does not exist)."""
    p = Path(path) if path is not None else DEFAULT_STRATEGY_PATH
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{p}: top level must be a mapping"
        raise ValueError(msg)
    return data


def legacy_path(path: tuple[str, ...]) -> tuple[str, ...] | None:
    """The strategy.yaml path of a pre-E20.3 routines path, ``None`` if it did not move."""
    if not path:
        return None
    head, rest = path[0], path[1:]
    if head == "personas" and len(rest) == 1:
        if rest[0] in LEGACY_SWITCHES:
            return (rest[0], "enabled")
        if rest[0] in LEGACY_CHOICES:
            return (rest[0], "mode")
        return None
    if head == "market_health":
        return ("market_health_context", "thresholds", *rest)
    if head in MOVED_BLOCKS and head != "technicals":
        return path
    return None


def split_overrides(
    overrides: Mapping[tuple[str, ...], Any] | None,
) -> tuple[dict[tuple[str, ...], Any], dict[tuple[str, ...], Any]]:
    """``(routines, strategy)`` overrides: pre-E20.3 routines paths go to strategy.yaml."""
    routines: dict[tuple[str, ...], Any] = {}
    strategy: dict[tuple[str, ...], Any] = {}
    for path, value in (overrides or {}).items():
        new = legacy_path(tuple(path))
        if new is None:
            routines[tuple(path)] = value
        else:
            strategy[new] = value
    return routines, strategy


def extract_legacy(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[tuple[str, ...], Any]]:
    """``(routines data without moved keys, strategy overrides)`` of raw routines *data*.

    For an experiment overlay merged into routines.yaml the pre-E20.3 way
    (``routines: {personas: {anti_chase: off}}``): the moved leaves become strategy.yaml
    overrides, as :func:`split_overrides` maps them at runtime.
    """
    out = dict(data)
    moved: dict[str, Any] = {}
    personas = out.get("personas")
    if isinstance(personas, dict):
        personas = dict(personas)
        for k in (*LEGACY_SWITCHES, *LEGACY_CHOICES):
            if k in personas and not isinstance(personas[k], dict):
                moved.setdefault("personas", {})[k] = personas.pop(k)
        out["personas"] = personas
    for k in MOVED_BLOCKS:
        if k in out:
            moved[k] = out.pop(k)
    _, strategy = split_overrides(_leaves(moved))
    return out, strategy


def _leaves(node: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Leaf ``path -> value`` pairs of a nested mapping (an empty mapping is no leaf)."""
    if isinstance(node, dict):
        out: dict[tuple[str, ...], Any] = {}
        for k, v in node.items():
            out.update(_leaves(v, (*prefix, str(k))))
        return out
    return {prefix: node} if prefix else {}


def moved_keys(data: Mapping[str, Any]) -> list[str]:
    """The moved blocks / switches still present in raw routines.yaml *data*."""
    found = [k for k in MOVED_BLOCKS if k in data]
    personas = data.get("personas")
    if isinstance(personas, dict):
        found += [
            f"personas.{k}"
            for k in (*LEGACY_SWITCHES, *LEGACY_CHOICES)
            if k in personas and not isinstance(personas[k], dict)
        ]
    return found
