"""Data contracts for forward A/B experiments (PLAN D44, card E10.1).

Pure: pydantic models, the canonical-JSON spec hash and the status transition
table. No DB, no clock, no network. :mod:`arc.experiments.store` persists them.

An experiment compares control with K treatment arms on the same tick inputs:

- **control** is the production config (its overlay must be empty);
- each **treatment** ``t1`` .. ``tK`` (D69, spec v2; K <= 16) is the production
  config with a config *overlay* deep-merged on top: the same format and the same
  :func:`arc.utils.yamlpatch.deep_merge` as ``arc backtest rank --experiment``. A
  forward overlay is keyed by the config file it patches (``ranking``, ``exits``,
  ``costs``, ``account_profiles``, ``routines``); each value is a partial copy of
  that file.

``aa`` experiments run identical arms (every overlay empty) to measure the noise
floor (sigma, achievable MDE); ``ab`` experiments change one thing per treatment.

Spec versions: v1 (``arms.treatment``) is the one-treatment format of D44; it still
loads, normalised to ``treatments: {t1: ...}``, and its canonical JSON (hence its
hash) is the v1 document exactly as before. v2 writes ``arms.treatments``.

Pre-registration: once registered, the spec's canonical-JSON SHA-256 is locked
and any edit needs a new experiment id.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ACTIVE_STATUSES",
    "CONTROL_ARM",
    "FIRST_TREATMENT",
    "LEGACY_TREATMENT",
    "MAX_TREATMENTS",
    "OVERLAY_TARGETS",
    "SPEC_VERSION",
    "TRANSITIONS",
    "ARM_PERSONAS",
    "Area",
    "Arm",
    "ArmPlan",
    "Arms",
    "ExperimentEvent",
    "ExperimentKind",
    "ExperimentSpec",
    "ExperimentState",
    "ExperimentStatus",
    "RunningDetail",
    "StopDetail",
    "StopReason",
    "arm_id",
    "canonical_json",
    "is_spec_arm",
    "spec_arm_name",
    "spec_hash",
    "treatment_key",
]

SPEC_VERSION = 2
CONTROL_ARM = "control"  # arm_id NULL on a row means this arm
#: D69: the v1 spec arm name; an alias of ``t1``. Reserved, like ``control``.
LEGACY_TREATMENT = "treatment"
FIRST_TREATMENT = "t1"
MAX_TREATMENTS = 16
_TREATMENT_RE = re.compile(r"^t([1-9]|1[0-6])$")
_FORBID = ConfigDict(extra="forbid", frozen=True)
_ID_RE = re.compile(r"^XP-[1-9]\d*$")
_PROPOSER_RE = re.compile(r"^(owner|A-[1-9]\d*)$")

# Config files a forward overlay may patch (key = file stem under config/).
# E13.12: ``universe`` (tiers / liquidity screens; e.g. a discovery screen arm). It is NOT a
# strategy-lane promotion stem (config/strategy_lane.yaml says why).
OVERLAY_TARGETS: tuple[str, ...] = (
    "ranking",
    "exits",
    "costs",
    "account_profiles",
    "routines",
    "universe",
)

#: E13.12 (D56): the non-loop personas an experiment arm may run on its own store.
#: E14.5: ``trending`` = the daily trending tier job (an XP-10-style ranker overlay).
ARM_PERSONAS: tuple[str, ...] = ("scout", "scalp", "trending")


class Area(StrEnum):
    """What part of the strategy the experiment changes; one running per area."""

    ENTRIES = "entries"
    EXITS = "exits"
    RANKING = "ranking"
    SIZING = "sizing"
    UNIVERSE = "universe"  # E13.12: tier layout / screens (config/universe.yaml)
    FUNNEL = "funnel"  # E13.12: Scout / Scalp inputs (routines funnel, categories)
    OTHER = "other"


class ExperimentKind(StrEnum):
    AA = "aa"  # identical arms: noise floor (sigma, MDE, fill gap)
    AB = "ab"  # treatment overlay vs control


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"  # registered, waiting for its area to free up
    REGISTERED = "registered"  # hash-locked and next to run in its area
    RUNNING = "running"
    STOPPED = "stopped"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class StopReason(StrEnum):
    WIN = "win"
    HARM = "harm"
    FUTILITY = "futility"
    OWNER = "owner"
    INVALID = "invalid"


# Statuses that hold an area (a new registration in the same area queues).
ACTIVE_STATUSES: frozenset[ExperimentStatus] = frozenset(
    {ExperimentStatus.REGISTERED, ExperimentStatus.RUNNING}
)

# Allowed status changes (draft -> draft is a spec revision, not an event).
TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.REGISTERED, ExperimentStatus.QUEUED}),
    ExperimentStatus.QUEUED: frozenset({ExperimentStatus.REGISTERED, ExperimentStatus.STOPPED}),
    ExperimentStatus.REGISTERED: frozenset({ExperimentStatus.RUNNING, ExperimentStatus.STOPPED}),
    ExperimentStatus.RUNNING: frozenset({ExperimentStatus.STOPPED}),
    ExperimentStatus.STOPPED: frozenset({ExperimentStatus.PROMOTED, ExperimentStatus.REJECTED}),
    ExperimentStatus.PROMOTED: frozenset(),
    ExperimentStatus.REJECTED: frozenset(),
}


def arm_id(experiment_id: str, arm: str) -> str | None:
    """The ``arm_id`` a row of *arm* carries: NULL for control, ``XP-<n>:<arm>`` otherwise."""
    return None if arm == CONTROL_ARM else f"{experiment_id}:{arm}"


class Arm(BaseModel):
    """One arm: a config overlay per target file (empty = production config)."""

    model_config = _FORBID

    overlay: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="config file stem -> partial file, deep-merged over config/<stem>.yaml",
    )

    @field_validator("overlay")
    @classmethod
    def _targets(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for target, body in v.items():
            if target not in OVERLAY_TARGETS:
                msg = f"overlay target {target!r} is not one of {', '.join(OVERLAY_TARGETS)}"
                raise ValueError(msg)
            if not body:
                msg = f"overlay target {target!r} is empty; drop it"
                raise ValueError(msg)
        return v


def treatment_key(name: str) -> int:
    """Sort key of a treatment name (``t2`` before ``t10``)."""
    return int(name[1:])


def spec_arm_name(name: str) -> str:
    """The spec arm a name refers to: ``treatment`` (v1 alias) -> ``t1``; else unchanged."""
    return FIRST_TREATMENT if name == LEGACY_TREATMENT else name


def is_spec_arm(name: str) -> bool:
    """``control``, ``treatment`` (alias of t1) or ``t1`` .. ``t16``."""
    return name in (CONTROL_ARM, LEGACY_TREATMENT) or bool(_TREATMENT_RE.match(name))


class Arms(BaseModel):
    """Control plus K treatments (D69). Input ``treatment:`` (v1) becomes ``t1``."""

    model_config = _FORBID

    control: Arm = Field(default_factory=lambda: Arm())
    treatments: dict[str, Arm] = Field(
        default_factory=lambda: {FIRST_TREATMENT: Arm()},
        description="t1..t16 -> the treatment arm (v1 `treatment` loads as t1)",
    )

    @model_validator(mode="before")
    @classmethod
    def _alias(cls, data: Any) -> Any:
        if not isinstance(data, dict) or LEGACY_TREATMENT not in data:
            return data
        if "treatments" in data:
            msg = "arms: give either `treatment` (spec v1) or `treatments` (v2), not both"
            raise ValueError(msg)
        out = {k: v for k, v in data.items() if k != LEGACY_TREATMENT}
        out["treatments"] = {FIRST_TREATMENT: data[LEGACY_TREATMENT]}
        return out

    @field_validator("treatments")
    @classmethod
    def _names(cls, v: dict[str, Arm]) -> dict[str, Arm]:
        if not v:
            msg = "an experiment needs at least one treatment arm"
            raise ValueError(msg)
        for name in v:
            if not _TREATMENT_RE.match(name):
                msg = (
                    f"treatment arm {name!r}: name it t1..t{MAX_TREATMENTS} "
                    "('control' and 'treatment' are reserved)"
                )
                raise ValueError(msg)
        return {k: v[k] for k in sorted(v, key=treatment_key)}

    @property
    def names(self) -> list[str]:
        """The treatment names in order (t1, t2, ..., t10)."""
        return list(self.treatments)

    @property
    def treatment(self) -> Arm:
        """The only treatment of a one-treatment spec (v1 accessor; K > 1 raises)."""
        if len(self.treatments) != 1:
            msg = f"spec has {len(self.treatments)} treatments; read arms.treatments"
            raise ValueError(msg)
        return next(iter(self.treatments.values()))

    def arm(self, name: str) -> Arm:
        """The spec arm *name* (``control``, ``treatment`` = t1, or ``tN``)."""
        n = spec_arm_name(name)
        if n == CONTROL_ARM:
            return self.control
        if n not in self.treatments:
            msg = f"spec has no arm {name!r} (treatments: {', '.join(self.treatments)})"
            raise KeyError(msg)
        return self.treatments[n]


class ExperimentSpec(BaseModel):
    """A pre-registrable experiment (``config/experiments/live/*.yaml``).

    Fields left ``None`` (alpha, power, sessions) are filled from
    ``config/experiments.yaml`` by ``arc experiment create``; the filled spec is
    what gets hash-locked.
    """

    model_config = _FORBID

    spec_version: Literal[1, 2] = SPEC_VERSION
    id: str = Field(..., description="XP-<n>")
    title: str = Field(..., min_length=1)
    hypothesis: str = Field(..., min_length=1)
    area: Area
    kind: ExperimentKind
    arms: Arms = Field(default_factory=lambda: Arms())
    primary_metric: Literal["paired_daily_net_pnl_pct"] = "paired_daily_net_pnl_pct"
    secondary_metric: Literal["sortino"] = "sortino"
    non_inferiority_margin: float | None = Field(
        default=None,
        gt=0.0,
        description="Sortino may be worse than control by at most this (required for ab)",
    )
    alpha: float | None = Field(default=None, gt=0.0, lt=0.5)
    power: float | None = Field(default=None, gt=0.0, lt=1.0)
    mde: float | None = Field(
        default=None, gt=0.0, description="Daily P&L diff (% equity); None until the A/A"
    )
    min_sessions: int | None = Field(default=None, ge=1)
    max_sessions: int | None = Field(default=None, ge=1)
    proposed_by: str = Field(..., description="owner | A-<n> (an arc-analyst finding)")
    backtest_ref: str | None = Field(
        default=None, description="E7.5 run dir / compare verdict (required for ab)"
    )

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            msg = f"experiment id {v!r} must look like XP-<n>"
            raise ValueError(msg)
        return v

    @field_validator("proposed_by")
    @classmethod
    def _proposer(cls, v: str) -> str:
        if not _PROPOSER_RE.match(v):
            msg = f"proposed_by {v!r} must be 'owner' or an arc-analyst id A-<n>"
            raise ValueError(msg)
        return v

    @model_validator(mode="before")
    @classmethod
    def _v1(cls, data: Any) -> Any:
        """A spec with v1 ``arms.treatment`` and no ``spec_version`` is a v1 spec."""
        if (
            isinstance(data, dict)
            and "spec_version" not in data
            and isinstance(data.get("arms"), dict)
            and LEGACY_TREATMENT in data["arms"]
        ):
            return {**data, "spec_version": 1}
        return data

    @model_validator(mode="after")
    def _shape(self) -> ExperimentSpec:
        if self.spec_version == 1 and self.arms.names != [FIRST_TREATMENT]:
            msg = (
                "a spec_version 1 spec has exactly one treatment (`treatment`); "
                "use spec_version 2 and `treatments: {t1: ..., t2: ...}` for more"
            )
            raise ValueError(msg)
        if self.arms.control.overlay:
            msg = "the control arm is the production config: its overlay must be empty"
            raise ValueError(msg)
        for name, arm in self.arms.treatments.items():
            if self.kind is ExperimentKind.AA and arm.overlay:
                msg = (
                    "an aa experiment runs identical arms: every treatment overlay must be "
                    f"empty ({name} has one)"
                )
                raise ValueError(msg)
            if self.kind is ExperimentKind.AB and not arm.overlay:
                msg = f"an ab experiment needs a treatment overlay (what changes) on {name}"
                raise ValueError(msg)
        if self.kind is ExperimentKind.AB:
            if self.backtest_ref is None:
                msg = "an ab experiment needs a backtest_ref (E7.5 run / compare verdict)"
                raise ValueError(msg)
            if self.non_inferiority_margin is None:
                msg = "an ab experiment needs a non_inferiority_margin for the secondary metric"
                raise ValueError(msg)
        if (
            self.min_sessions is not None
            and self.max_sessions is not None
            and self.min_sessions > self.max_sessions
        ):
            msg = f"min_sessions {self.min_sessions} > max_sessions {self.max_sessions}"
            raise ValueError(msg)
        return self

    @property
    def complete(self) -> bool:
        """Every default-able field is set (what registration requires)."""
        return None not in (
            self.alpha,
            self.power,
            self.min_sessions,
            self.max_sessions,
        )


def _canonical_doc(spec: ExperimentSpec) -> dict[str, Any]:
    """The hashed document. A v1 spec keeps its v1 shape (``arms.treatment``), so every
    hash locked before D69 recomputes unchanged."""
    doc = spec.model_dump(mode="json")
    if spec.spec_version == 1:
        arms = doc["arms"]
        doc["arms"] = {
            "control": arms["control"],
            LEGACY_TREATMENT: arms["treatments"][FIRST_TREATMENT],
        }
    return doc


def canonical_json(spec: ExperimentSpec) -> str:
    """The spec as canonical JSON: sorted keys, no whitespace, every field present."""
    return json.dumps(
        _canonical_doc(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def spec_hash(spec: ExperimentSpec) -> str:
    """SHA-256 hex of :func:`canonical_json` (the pre-registration lock)."""
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


class ArmPlan(BaseModel):
    """How one runner arm runs (E13.12, D56): computed at t0, stored on ``arm_identity``.

    ``fork_step`` is the first loop step the arm runs itself; ``arm_personas`` the
    non-loop personas it runs on its own store (their output never comes from
    control); ``shared_kinds`` the context kinds synced from control before each
    paired chain (and before the arm's own Scout / Scalp), minus every row
    ``own_producers`` wrote: the arm writes those itself.
    """

    model_config = _FORBID

    fork_step: str
    arm_personas: list[Literal["scout", "scalp", "trending"]] = Field(default_factory=list)
    arm_jobs: list[str] = Field(default_factory=list)
    shared_kinds: list[str] = Field(default_factory=list)
    own_producers: list[str] = Field(
        default_factory=list,
        description="Jobs whose context rows are never synced from control (the arm's own)",
    )


class RunningDetail(BaseModel):
    """What ``running`` records at t0 (E10.2 fills it)."""

    model_config = _FORBID

    t0: _dt.datetime
    t0_equity: float = Field(..., gt=0.0)
    legacy_book: list[str] = Field(
        default_factory=list, description="open_structures ids open at t0 (excluded from metrics)"
    )
    control_sha: str = Field(..., min_length=7)
    config_hashes: dict[str, str] = Field(default_factory=dict)
    aa_override: bool = False
    arm_plans: dict[str, ArmPlan] = Field(
        default_factory=dict,
        description="E13.12: runner arm -> its ArmPlan at t0 (empty before E13.12)",
    )

    @field_validator("t0")
    @classmethod
    def _aware(cls, v: _dt.datetime) -> _dt.datetime:
        if v.tzinfo is None:
            msg = "t0 must be timezone-aware"
            raise ValueError(msg)
        return v


class StopDetail(BaseModel):
    model_config = _FORBID

    sigma: float | None = Field(
        default=None, gt=0.0, description="Daily P&L-diff stdev (% equity); A/A records it"
    )
    sessions: int | None = Field(default=None, ge=0)
    note: str | None = None


class ExperimentEvent(BaseModel):
    model_config = _FORBID

    id: int
    experiment_id: str
    status: ExperimentStatus
    reason: StopReason | None = None
    spec_hash: str
    actor: str
    detail: dict[str, Any] = Field(default_factory=dict)
    at: _dt.datetime


class ExperimentState(BaseModel):
    """An experiment's latest spec revision plus its status from the event log."""

    model_config = _FORBID

    experiment_id: str
    status: ExperimentStatus
    reason: StopReason | None = None
    revision: int
    spec: ExperimentSpec
    spec_hash: str
    registered_hash: str | None = Field(
        default=None, description="The hash locked at registration (None while draft)"
    )
    running: RunningDetail | None = None
    stop: StopDetail | None = None
    events: list[ExperimentEvent] = Field(default_factory=list)

    @property
    def area(self) -> Area:
        return self.spec.area

    @property
    def kind(self) -> ExperimentKind:
        return self.spec.kind
