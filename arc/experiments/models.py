"""Data contracts for forward A/B experiments (PLAN D44, card E10.1).

Pure: pydantic models, the canonical-JSON spec hash and the status transition
table. No DB, no clock, no network. :mod:`arc.experiments.store` persists them.

An experiment compares two arms on the same tick inputs:

- **control** is the production config (its overlay must be empty);
- **treatment** is the production config with a config *overlay* deep-merged on
  top: the same format and the same :func:`arc.utils.yamlpatch.deep_merge` as
  ``arc backtest rank --experiment``. A forward overlay is keyed by the config
  file it patches (``ranking``, ``exits``, ``costs``, ``account_profiles``,
  ``routines``); each value is a partial copy of that file.

``aa`` experiments run two identical arms (both overlays empty) to measure the
noise floor (sigma, achievable MDE); ``ab`` experiments change one thing.

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
    "OVERLAY_TARGETS",
    "SPEC_VERSION",
    "TRANSITIONS",
    "Area",
    "Arm",
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
    "spec_hash",
]

SPEC_VERSION = 1
CONTROL_ARM = "control"  # arm_id NULL on a row means this arm
_FORBID = ConfigDict(extra="forbid", frozen=True)
_ID_RE = re.compile(r"^X-[1-9]\d*$")
_PROPOSER_RE = re.compile(r"^(owner|A-[1-9]\d*)$")

# Config files a forward overlay may patch (key = file stem under config/).
OVERLAY_TARGETS: tuple[str, ...] = ("ranking", "exits", "costs", "account_profiles", "routines")


class Area(StrEnum):
    """What part of the strategy the experiment changes; one running per area."""

    ENTRIES = "entries"
    EXITS = "exits"
    RANKING = "ranking"
    SIZING = "sizing"
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
    """The ``arm_id`` a row of *arm* carries: NULL for control, ``X-<n>:<arm>`` otherwise."""
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


class Arms(BaseModel):
    model_config = _FORBID

    control: Arm = Field(default_factory=lambda: Arm())
    treatment: Arm = Field(default_factory=lambda: Arm())


class ExperimentSpec(BaseModel):
    """A pre-registrable experiment (``config/experiments/live/*.yaml``).

    Fields left ``None`` (alpha, power, sessions) are filled from
    ``config/experiments.yaml`` by ``arc experiment create``; the filled spec is
    what gets hash-locked.
    """

    model_config = _FORBID

    spec_version: Literal[1] = SPEC_VERSION
    id: str = Field(..., description="X-<n>")
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
            msg = f"experiment id {v!r} must look like X-<n>"
            raise ValueError(msg)
        return v

    @field_validator("proposed_by")
    @classmethod
    def _proposer(cls, v: str) -> str:
        if not _PROPOSER_RE.match(v):
            msg = f"proposed_by {v!r} must be 'owner' or an arc-analyst id A-<n>"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _shape(self) -> ExperimentSpec:
        if self.arms.control.overlay:
            msg = "the control arm is the production config: its overlay must be empty"
            raise ValueError(msg)
        if self.kind is ExperimentKind.AA and self.arms.treatment.overlay:
            msg = "an aa experiment runs identical arms: the treatment overlay must be empty"
            raise ValueError(msg)
        if self.kind is ExperimentKind.AB:
            if not self.arms.treatment.overlay:
                msg = "an ab experiment needs a treatment overlay (what changes)"
                raise ValueError(msg)
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


def canonical_json(spec: ExperimentSpec) -> str:
    """The spec as canonical JSON: sorted keys, no whitespace, every field present."""
    return json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def spec_hash(spec: ExperimentSpec) -> str:
    """SHA-256 hex of :func:`canonical_json` (the pre-registration lock)."""
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


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
