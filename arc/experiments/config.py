"""Forward A/B experiment defaults: ``config/experiments.yaml`` (PLAN D44, E10.1).

Pure loader (no DB, no clock). The D26 control panel overrides these keys
(``experiments.*`` in :mod:`arc.control.registry`); read the effective values via
:func:`arc.control.effective.experiments_config`, never this loader directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_EXPERIMENTS_PATH",
    "FORBIDDEN_ARM_KEYS",
    "ArmRunner",
    "ExperimentDefaults",
    "RunnerConfig",
    "ExperimentsConfig",
    "StatsConfig",
    "load_experiments_config",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EXPERIMENTS_PATH = REPO_ROOT / "config" / "experiments.yaml"

_FORBID = ConfigDict(extra="forbid", frozen=True)


class StatsConfig(BaseModel):
    """How E10.3 computes the numbers (not hash-locked into specs)."""

    model_config = _FORBID

    sigma_upper_q: float = Field(
        default=0.05,
        gt=0.0,
        lt=0.5,
        description="No A/A sigma: inflate the running sd to its (1 - q) upper chi2 bound",
    )
    bootstrap_resamples: int = Field(default=2000, ge=200, le=20000)


class ExperimentDefaults(BaseModel):
    model_config = _FORBID

    alpha: float = Field(default=0.05, gt=0.0, lt=0.5, description="Two-sided significance")
    power: float = Field(default=0.8, gt=0.0, lt=1.0)
    min_sessions: int = Field(default=20, ge=1)
    max_sessions: int = Field(default=60, ge=1)
    aa_sessions: int = Field(default=10, ge=1, description="A/A run length (min = max)")

    @model_validator(mode="after")
    def _window(self) -> ExperimentDefaults:
        if self.min_sessions > self.max_sessions:
            msg = f"min_sessions {self.min_sessions} > max_sessions {self.max_sessions}"
            raise ValueError(msg)
        return self


_ARM_RE = r"^[a-z][a-z0-9_]{0,31}$"
_ENV_RE = r"^[A-Z][A-Z0-9_]*$"
# Key prefixes an experiment arm may never trade with (AGENTS / D44): production
# (ALPACA) and the integration-test account (ALPACA_TEST).
FORBIDDEN_ARM_KEYS: frozenset[str] = frozenset({"ALPACA", "ALPACA_TEST"})


class ArmRunner(BaseModel):
    """One experiment arm the runner executes (E10.2): keys + store + which spec overlay.

    N-arm by configuration: a treatment arm, a paper shadow-control for a future
    live control (``spec_arm: control`` on its own paper keys), or both. The
    production control is never listed here: it is the normal tick on ``data/arc.db``.
    """

    model_config = _FORBID

    spec_arm: Literal["control", "treatment"] = Field(
        ..., description="Whose overlay this arm runs (the spec's control or treatment arm)"
    )
    keys_env: str = Field(
        ...,
        pattern=_ENV_RE,
        description="Env prefix of the arm's broker keys: <prefix>_API_KEY / <prefix>_SECRET_KEY",
    )
    db: str = Field(
        ...,
        min_length=1,
        description=(
            "The arm's own store, relative to the repo; '{experiment_id}' is replaced, so "
            "every experiment starts on a fresh store (arm_identity is written once)"
        ),
    )

    def db_path(self, experiment_id: str) -> str:
        return self.db.replace("{experiment_id}", experiment_id)

    @model_validator(mode="after")
    def _keys(self) -> ArmRunner:
        if self.keys_env in FORBIDDEN_ARM_KEYS:
            msg = f"arm keys {self.keys_env}_* are production/test keys; arms use their own"
            raise ValueError(msg)
        return self


class RunnerConfig(BaseModel):
    """``experiments.runner`` (E10.2): pairing an arm with control's trading loop."""

    model_config = _FORBID

    enabled: bool = Field(default=True, description="Run arms while an experiment is running")
    max_lag_seconds: int = Field(
        default=240,
        ge=30,
        le=1800,
        description="Skip pairing a control chain older than this (the slot's inputs went stale)",
    )
    tape_keep_days: int = Field(default=3, ge=1, le=30, description="market_tape retention")
    arm_jobs: list[str] = Field(
        default_factory=lambda: ["monitor", "positions.evaluate", "auditor", "investor"],
        description=(
            "Jobs an arm runs on its own store and account (position management, "
            "reconcile, ladders). Sources and the Scout are shared from control; the loop "
            "runs paired; everything else is control's."
        ),
    )
    arms: dict[str, ArmRunner] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _arms(self) -> RunnerConfig:
        import re

        dbs: set[str] = set()
        for name, arm in self.arms.items():
            if not re.match(_ARM_RE, name) or name == "control":
                msg = f"runner arm name {name!r}: lowercase identifier, never 'control'"
                raise ValueError(msg)
            if arm.db in dbs:
                msg = f"runner arms share a store ({arm.db}); each arm needs its own"
                raise ValueError(msg)
            dbs.add(arm.db)
        keys = [a.keys_env for a in self.arms.values()]
        if len(keys) != len(set(keys)):
            msg = "runner arms share broker keys; each arm needs its own paper account"
            raise ValueError(msg)
        return self


class ExperimentsConfig(BaseModel):
    model_config = _FORBID

    defaults: ExperimentDefaults = Field(default_factory=lambda: ExperimentDefaults())
    stats: StatsConfig = Field(default_factory=lambda: StatsConfig())
    runner: RunnerConfig = Field(default_factory=lambda: RunnerConfig())


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
