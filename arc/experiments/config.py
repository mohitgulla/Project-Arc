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

    from arc.experiments.models import ExperimentSpec

__all__ = [
    "DEFAULT_EXPERIMENTS_PATH",
    "EACH_TREATMENT",
    "FORBIDDEN_ARM_KEYS",
    "MAX_PARALLEL_ARMS_CEILING",
    "AccountMode",
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
#: D69: an arm entry with this ``spec_arm`` is a template, expanded at t0 into one
#: runner arm per spec treatment (named t1..tK); shared account mode only.
EACH_TREATMENT = "treatments"
#: D69 / D26: code ceiling of ``max_parallel_arms`` (the registry's hard ceiling too).
MAX_PARALLEL_ARMS_CEILING = 16
AccountMode = Literal["dedicated", "shared"]


class ArmRunner(BaseModel):
    """One experiment arm the runner executes (E10.2): keys + store + which spec overlay.

    N-arm by configuration: a treatment arm, a paper shadow-control for a future
    live control (``spec_arm: control`` on its own paper keys), or both. The
    production control is never listed here: it is the normal tick on ``data/arc.db``.

    D69 (shared account mode): ``spec_arm: treatments`` makes the entry a template,
    expanded per spec treatment; its ``db`` must carry ``{arm}`` (e.g.
    ``data/arc-exp-{experiment_id}-{arm}.db``) so every arm gets its own store.
    """

    model_config = _FORBID

    spec_arm: str = Field(
        ...,
        description=(
            "Whose overlay this arm runs: control, treatment (= t1, spec v1), t1..t16, "
            "or 'treatments' (a template: one arm per spec treatment, shared mode)"
        ),
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
            "The arm's own store, relative to the repo; '{experiment_id}' (and '{arm}', the "
            "runner arm name) are replaced, so every experiment starts on a fresh store "
            "(arm_identity is written once)"
        ),
    )

    @property
    def is_template(self) -> bool:
        return self.spec_arm == EACH_TREATMENT

    def db_path(self, experiment_id: str, arm: str | None = None) -> str:
        out = self.db.replace("{experiment_id}", experiment_id)
        return out if arm is None else out.replace("{arm}", arm)

    @model_validator(mode="after")
    def _keys(self) -> ArmRunner:
        from arc.experiments.models import is_spec_arm

        if self.keys_env in FORBIDDEN_ARM_KEYS:
            msg = f"arm keys {self.keys_env}_* are production/test keys; arms use their own"
            raise ValueError(msg)
        if not (self.is_template or is_spec_arm(self.spec_arm)):
            msg = (
                f"spec_arm {self.spec_arm!r}: control, treatment, t1..t16, or "
                f"{EACH_TREATMENT!r} (template)"
            )
            raise ValueError(msg)
        if self.is_template and "{arm}" not in self.db:
            msg = f"a {EACH_TREATMENT!r} template arm needs '{{arm}}' in its db ({self.db})"
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
        default_factory=lambda: ["monitor", "positions.evaluate", "broker.reconcile", "broker"],
        description=(
            "Jobs an arm runs on its own store and account (position management, "
            "reconcile, ladders). Sources are shared from control; the Scout / Scalp too "
            "unless the arm owns them (arm_personas); the loop runs paired; everything "
            "else is control's."
        ),
    )
    arm_personas: list[Literal["scout", "scalp", "trending"]] = Field(
        default_factory=list,
        description=(
            "E13.12 (D56): non-loop personas every arm runs on its own store, on top of "
            "the ones its overlay touches (arc.experiments.runner.arm_owned_personas). "
            "Experiment topology, not a strategy knob (never runtime-tunable)."
        ),
    )
    arms: dict[str, ArmRunner] = Field(default_factory=dict)
    account_mode: AccountMode = Field(
        default="dedicated",
        description=(
            "D69: dedicated = one paper account per arm (keys_env unique, the account flat "
            "at t0); shared = many arms (and experiments) trade one account, each as a "
            "virtual sub-account, and an arm entry may be a 'treatments' template. "
            "Topology: never runtime-tunable."
        ),
    )
    max_parallel_arms: int = Field(
        default=10,
        ge=1,
        le=MAX_PARALLEL_ARMS_CEILING,
        description="D69: arms registered + running across ALL experiments (ceiling 16)",
    )
    shared_capacity_frac: float = Field(
        default=0.9,
        gt=0.0,
        le=1.0,
        description=(
            "D69 shared mode: at t0, the virtual t0 equity of every running arm on the "
            "account plus the new arms must fit in broker equity x this"
        ),
    )

    @model_validator(mode="after")
    def _arms(self) -> RunnerConfig:
        import re

        if len(set(self.arm_personas)) != len(self.arm_personas):
            msg = "runner arm_personas lists a persona twice"
            raise ValueError(msg)

        dbs: set[str] = set()
        for name, arm in self.arms.items():
            if not re.match(_ARM_RE, name) or name == "control":
                msg = f"runner arm name {name!r}: lowercase identifier, never 'control'"
                raise ValueError(msg)
            if arm.db in dbs:
                msg = f"runner arms share a store ({arm.db}); each arm needs its own"
                raise ValueError(msg)
            dbs.add(arm.db)
            if arm.is_template and self.account_mode != "shared":
                msg = (
                    f"runner arm {name!r} is a {EACH_TREATMENT!r} template: only "
                    "account_mode: shared expands one arm per treatment"
                )
                raise ValueError(msg)
        if self.account_mode == "dedicated":
            keys = [a.keys_env for a in self.arms.values()]
            if len(keys) != len(set(keys)):
                msg = (
                    "runner arms share broker keys; each arm needs its own paper account "
                    "(or account_mode: shared, D69)"
                )
                raise ValueError(msg)
        return self

    def arms_for(self, spec: ExperimentSpec) -> dict[str, ArmRunner]:
        """The runner arms of experiment *spec*: templates expanded per treatment (D69).

        Every returned arm names a concrete spec arm. Raises ``ValueError`` when an arm
        names a spec arm the spec lacks, a template name collides with a listed arm, or
        a spec treatment has no runner arm (it would silently never run).
        """
        from arc.experiments.models import spec_arm_name

        out: dict[str, ArmRunner] = {}
        for name, arm in self.arms.items():
            if not arm.is_template:
                try:
                    spec.arms.arm(arm.spec_arm)
                except KeyError as exc:
                    raise ValueError(f"runner arm {name!r}: {exc.args[0]}") from exc
                if name in out:
                    msg = f"runner arm {name!r} collides with a template-expanded arm"
                    raise ValueError(msg)
                out[name] = arm
                continue
            for t in spec.arms.names:
                if t in out or t in self.arms:
                    msg = f"template arm {name!r} expands to {t!r}, which is already an arm"
                    raise ValueError(msg)
                out[t] = arm.model_copy(update={"spec_arm": t, "db": arm.db.replace("{arm}", t)})
        covered = {spec_arm_name(a.spec_arm) for a in out.values()}
        missing = [t for t in spec.arms.names if t not in covered]
        # a one-treatment spec may run without its treatment (e.g. a shadow control
        # only, pre-D69 behaviour); a multi-arm spec must run every treatment
        if missing and len(spec.arms.names) > 1:
            msg = (
                f"{spec.id}: treatment(s) {', '.join(missing)} have no runner arm; list one "
                f"per treatment or use account_mode: shared with a {EACH_TREATMENT!r} template"
            )
            raise ValueError(msg)
        return out


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
