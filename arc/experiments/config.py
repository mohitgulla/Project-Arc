"""Forward A/B experiment defaults: ``config/experiments.yaml`` (PLAN D44, E10.1).

Pure loader (no DB, no clock). The D26 control panel overrides these keys
(``experiments.*`` in :mod:`arc.control.registry`); read the effective values via
:func:`arc.control.effective.experiments_config`, never this loader directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_EXPERIMENTS_PATH",
    "ExperimentDefaults",
    "ExperimentsConfig",
    "Guardrails",
    "load_experiments_config",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EXPERIMENTS_PATH = REPO_ROOT / "config" / "experiments.yaml"

_FORBID = ConfigDict(extra="forbid", frozen=True)


class Guardrails(BaseModel):
    """D44 harm stops for the treatment arm (early stop only, never a win condition)."""

    model_config = _FORBID

    max_dd_worse: float = Field(
        default=0.03,
        gt=0.0,
        le=1.0,
        description="Stop when treatment max drawdown is worse than control's by more (equity)",
    )
    worst_day: float = Field(
        default=-0.02, lt=0.0, ge=-1.0, description="Stop on any treatment day below this"
    )
    order_rate_ratio: float = Field(
        default=1.5, ge=1.0, description="Stop when treatment orders exceed this x control's"
    )
    stop_on_halt: bool = Field(
        default=True, description="Stop on any halt / reconcile fill_unknown on the exp. account"
    )


class ExperimentDefaults(BaseModel):
    model_config = _FORBID

    alpha: float = Field(default=0.05, gt=0.0, lt=0.5, description="Two-sided significance")
    power: float = Field(default=0.8, gt=0.0, lt=1.0)
    min_sessions: int = Field(default=20, ge=1)
    max_sessions: int = Field(default=60, ge=1)
    aa_sessions: int = Field(default=10, ge=1, description="A/A run length (min = max)")
    guardrails: Guardrails = Field(default_factory=lambda: Guardrails())

    @model_validator(mode="after")
    def _window(self) -> ExperimentDefaults:
        if self.min_sessions > self.max_sessions:
            msg = f"min_sessions {self.min_sessions} > max_sessions {self.max_sessions}"
            raise ValueError(msg)
        return self


class ExperimentsConfig(BaseModel):
    model_config = _FORBID

    defaults: ExperimentDefaults = Field(default_factory=lambda: ExperimentDefaults())


def load_experiments_config(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> ExperimentsConfig:
    """Load and validate ``config/experiments.yaml`` (or *path*).

    *overrides* (D26, ``path -> value`` from the file root, e.g.
    ``("experiments", "defaults", "alpha")``) patch the YAML before validation.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_EXPERIMENTS_PATH
    data = apply_overrides(yaml.safe_load(p.read_text()) or {}, overrides)
    return ExperimentsConfig.model_validate(data.get("experiments", data))
