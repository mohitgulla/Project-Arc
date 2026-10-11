"""Experiment specs from YAML and arm configs from overlays (PLAN D44, E10.1).

An arm's config for one file is ``deep_merge(<config file>, overlay[<file>])``,
the same :func:`arc.utils.yamlpatch.deep_merge` that ``arc backtest rank
--experiment`` applies to ``config/ranking.yaml``. The merged data is validated
by that file's own pydantic model, so a treatment overlay with an unknown or
out-of-range key fails at ``arc experiment create``, not at t0.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from arc.experiments.models import OVERLAY_TARGETS, ExperimentSpec
from arc.utils.yamlpatch import deep_merge

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from arc.experiments.config import ExperimentDefaults

__all__ = [
    "LIVE_SPECS_DIR",
    "arm_config_data",
    "fill_defaults",
    "load_spec",
    "target_path",
    "validate_arms",
    "validate_target",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_SPECS_DIR = REPO_ROOT / "config" / "experiments" / "live"


def target_path(target: str) -> Path:
    """``config/<target>.yaml``: the base file a forward overlay patches."""
    if target not in OVERLAY_TARGETS:
        msg = f"unknown overlay target {target!r}"
        raise ValueError(msg)
    return REPO_ROOT / "config" / f"{target}.yaml"


def _validator(target: str) -> Callable[[dict[str, Any]], object]:
    if target == "ranking":
        from arc.backtest.ranking import RankingFile

        return RankingFile.model_validate
    if target == "exits":
        from arc.exits.policy import ExitConfig

        return ExitConfig.model_validate
    if target == "costs":
        from arc.backtest.costs import CostModel

        return lambda d: CostModel.model_validate(d.get("costs", d))
    if target == "account_profiles":
        from arc.account_profiles import AccountProfiles

        return AccountProfiles.model_validate
    if target == "universe":  # E13.12: tiers / liquidity screens
        from arc.universe.config import UniverseConfig

        return UniverseConfig.model_validate
    if target == "strategy":  # E20.3 (D85): persona switches + knobs
        from arc.routines.strategy import StrategyConfig

        return StrategyConfig.model_validate

    def _routines(d: dict[str, Any]) -> object:
        # E20.3: a pre-E20.3 overlay leaf (`personas: {anti_chase: off}`) validates
        # against strategy.yaml, as the runtime maps it (arc.routines.strategy).
        # E20.4: a pre-E20.4 universe leaf (`universe: {carryover: …}`, the trending /
        # momentum job options) validates against universe.yaml.
        from arc.routines.config import routines_with_strategy
        from arc.routines.strategy import extract_legacy, load_strategy_data
        from arc.universe.config import (
            DEFAULT_UNIVERSE_CONFIG,
            UniverseConfig,
            extract_legacy_universe,
        )
        from arc.utils.yamlpatch import apply_overrides

        d, universe_ov = extract_legacy_universe(d)
        if universe_ov:
            base = yaml.safe_load(DEFAULT_UNIVERSE_CONFIG.read_text()) or {}
            UniverseConfig.model_validate(apply_overrides(base, universe_ov))
        rest, strategy_ov = extract_legacy(d)
        return routines_with_strategy(rest, load_strategy_data(None), strategy_ov)

    return _routines


def validate_target(target: str, data: dict[str, Any]) -> object:
    """*data* (a whole ``config/<target>.yaml`` document) validated by that file's model.

    E7.12 backtest studies validate their arm overlays through this, the same
    validators ``arc experiment create`` uses (raises ``ValueError``/``ValidationError``).
    """
    if target not in OVERLAY_TARGETS:
        msg = f"unknown overlay target {target!r}"
        raise ValueError(msg)
    return _validator(target)(data)


def arm_config_data(
    spec: ExperimentSpec,
    arm: str,
    target: str,
    *,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The raw config of *target* for *arm*: base file data with the arm's overlay merged.

    *base* defaults to ``config/<target>.yaml``; the result is validated by the
    file's model (raises ``ValueError`` / ``ValidationError``).
    """
    a = spec.arms.arm(arm)  # control, t1..tK (or v1 `treatment` = t1)
    if base is None:
        base = yaml.safe_load(target_path(target).read_text()) or {}
    merged = deep_merge(base, a.overlay.get(target, {}))
    _validator(target)(merged)
    return merged


def validate_arms(spec: ExperimentSpec) -> None:
    """Every overlay target of every arm still loads under its file's model."""
    for arm in ("control", *spec.arms.names):
        for target in spec.arms.arm(arm).overlay:
            try:
                arm_config_data(spec, arm, target)
            except ValueError as exc:
                lines = str(exc).strip().splitlines()
                label = "treatment" if spec.arms.names == ["t1"] and arm == "t1" else arm
                why = " | ".join(lines[:4])
                msg = f"{label} overlay for {target}.yaml does not validate: {why}"
                raise ValueError(msg) from exc


def fill_defaults(spec: ExperimentSpec, d: ExperimentDefaults) -> ExperimentSpec:
    """*spec* with every unset default-able field taken from ``config/experiments.yaml``.

    A/A experiments default to ``aa_sessions`` for both min and max sessions.
    """
    aa = spec.kind.value == "aa"
    min_s = spec.min_sessions
    max_s = spec.max_sessions
    if min_s is None:
        min_s = d.aa_sessions if aa else d.min_sessions
    if max_s is None:
        max_s = max(min_s, d.aa_sessions if aa else d.max_sessions)
    update: dict[str, Any] = {
        "alpha": spec.alpha if spec.alpha is not None else d.alpha,
        "power": spec.power if spec.power is not None else d.power,
        "min_sessions": min_s,
        "max_sessions": max_s,
    }
    return ExperimentSpec.model_validate(spec.model_dump() | update)


def load_spec(path: Path | str) -> ExperimentSpec:
    """Parse and validate one spec file (``config/experiments/live/*.yaml``)."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{path}: top level must be a mapping"
        raise ValueError(msg)
    spec = ExperimentSpec.model_validate(data)
    validate_arms(spec)
    return spec
