"""Schema for ``config/routines.yaml`` (D16): per-source/per-persona cadence, chains, triggers.

Everything the dispatcher does is driven by this file. Adding a source or a
persona, changing a cadence, or re-ordering a chain is a YAML edit validated by
``arc routines validate``, never a code change.

Job keys (``sources.<name>`` / ``personas.<name>``):

- ``schedule: ["22:00", "12:00"]`` — fixed ET wall-clock times, or
- ``every: 30m`` (+ optional ``window: "06:00-20:00"``, inclusive) — a grid
  anchored at the window start (midnight when no window), or
- ``trigger: approval`` — event-driven only (``<job>.completed``, ``approval``,
  ``halt``, or any name emitted with ``arc routines emit``).
- ``days: daily | trading | weekdays`` (default ``daily``), or a list of
  weekdays for weekly jobs (``days: [fri]``).
- ``chain: [a, b, c]`` — steps run in order after this job, in one chain run.
- ``after_sources: true`` — run every source due in the same tick first.
- ``ttl`` — catch-up window: a missed slot runs (once) only while inside it.
- ``context: {ttl, supersede}`` — override the TTL/supersede policy of the
  context entries this job writes (per-source TTL, D14).
- ``reads: [kind, ...]`` — kinds included in the input snapshot (default: all).
- ``writes: [kind, ...]`` — kinds the job may write (D27). Declared I/O contract:
  a write of any other kind fails the run, and a job with no ``writes`` may
  write nothing (fail-closed). ``[]`` declares a job that writes nothing.
- ``handler: "module:function"`` — explicit handler; default resolves by job name.
- ``halt_exempt: true`` — persona keeps running while halted (Auditor only).
- ``notify: quiet | summary | card`` — heartbeat policy: sources default
  ``quiet`` (folded into the next persona post), personas and chain steps
  default ``card`` (E5.5 digest card; the one-liner when a job has no card).
- ``llm: true|false`` — whether the job takes the global LLM lock (default:
  personas and chain steps yes, sources no).
- Any other key (e.g. ``only_for: open_positions``, ``channel: <url>``) is kept
  as a handler option in :attr:`JobSpec.options`, so new filters never break
  validation.

``steps.<name>`` configures chain steps that are not scheduled on their own
(e.g. ``propose``): same keys as a job minus the cadence.
"""

from __future__ import annotations

import datetime as _dt
import enum
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from arc.context.kinds import KINDS
from arc.context.store import Supersede
from arc.context.ttl import Ttl, parse_duration
from arc.monitoring.config import MonitoringSettings
from arc.routines.conditions import parse_condition

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROUTINES_PATH = REPO_ROOT / "config" / "routines.yaml"

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_JOB_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


def _parse_hhmm(text: str) -> _dt.time:
    m = _HHMM.match(str(text).strip())
    if not m:
        msg = f"invalid time {text!r}; expected 24h 'HH:MM' in ET"
        raise ValueError(msg)
    return _dt.time(int(m.group(1)), int(m.group(2)))


class Days(enum.StrEnum):
    DAILY = "daily"
    TRADING = "trading"
    WEEKDAYS = "weekdays"


class Weekday(enum.StrEnum):
    """Calendar weekday for weekly jobs (``days: [fri]``); value order = ISO weekday - 1."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @property
    def weekday_index(self) -> int:
        """``datetime.date.weekday()`` of this day (Mon = 0)."""
        return list(Weekday).index(self)


class Notify(enum.StrEnum):
    QUIET = "quiet"  # summarised into the next persona heartbeat
    SUMMARY = "summary"  # one-line heartbeat per run
    CARD = "card"  # E5.5 digest card (Block Kit); one-liner if the job has no card


class JobKind(enum.StrEnum):
    SOURCE = "source"
    PERSONA = "persona"


class ContextPolicy(BaseModel):
    """TTL + supersede policy for context entries a producer writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: Ttl | None = None
    supersede: Supersede = Supersede.LATEST


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: _dt.time
    end: _dt.time

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        if isinstance(v, str):
            parts = v.split("-")
            if len(parts) != 2:
                msg = f"invalid window {v!r}; expected 'HH:MM-HH:MM'"
                raise ValueError(msg)
            return {"start": _parse_hhmm(parts[0]), "end": _parse_hhmm(parts[1])}
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.end <= self.start:
            msg = "window end must be after start (windows cannot cross midnight)"
            raise ValueError(msg)
        return self

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


class StepSpec(BaseModel):
    """Settings shared by every runnable unit (jobs and chain steps).

    Unknown keys are kept as handler options (:attr:`options`).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    context: ContextPolicy | None = None
    reads: list[str] | None = None
    # D27: kinds this unit may write. None = undeclared -> ANY write fails the run;
    # [] = writes nothing. Enforced in JobContext.write (fail-closed).
    writes: list[str] | None = None
    handler: str | None = None
    notify: Notify | None = None
    llm: bool | None = None  # holds the global LLM lock; default: personas yes, sources no
    # D31: what a chain step does when the loop's root step reports `no_change`.
    # `skip` (default) records a skipped run; `run` executes it anyway (e.g. `execute`,
    # which carries pending approvals and ladders forward).
    on_no_change: Literal["skip", "run"] = "skip"

    @field_validator("reads", "writes")
    @classmethod
    def _kinds(cls, v: list[str] | None, info: ValidationInfo) -> list[str] | None:
        for kind in v or []:
            if kind not in KINDS:
                msg = f"unknown context kind {kind!r} in {info.field_name}"
                raise ValueError(msg)
        if v is not None and len(set(v)) != len(v):
            msg = f"{info.field_name} lists a kind twice"
            raise ValueError(msg)
        return v

    @field_validator("handler")
    @classmethod
    def _handler(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$", v):
            msg = f"handler must be 'package.module:function', got {v!r}"
            raise ValueError(msg)
        return v

    @property
    def options(self) -> dict[str, Any]:
        """Extra per-job filters/params (e.g. ``only_for``, ``channel``)."""
        return dict(self.model_extra or {})


class JobSpec(StepSpec):
    """One source or persona job: a :class:`StepSpec` plus a cadence."""

    schedule: list[_dt.time] = Field(default_factory=list)
    every: _dt.timedelta | None = None
    window: Window | None = None
    trigger: str | None = None
    days: Days | list[Weekday] = Days.DAILY
    chain: list[str] = Field(default_factory=list)
    after_sources: bool = False
    ttl: Ttl | None = None
    halt_exempt: bool = False
    enabled: bool = True

    @field_validator("schedule", mode="before")
    @classmethod
    def _schedule(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [_parse_hhmm(t) if isinstance(t, str) else t for t in v]

    @field_validator("every", mode="before")
    @classmethod
    def _every(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v: Any) -> Any:
        if isinstance(v, list):
            names = [str(d).strip().lower()[:3] for d in v]
            if not names or len(set(names)) != len(names):
                msg = "days list must name each weekday once (e.g. [fri])"
                raise ValueError(msg)
            return names
        return v

    @property
    def days_label(self) -> str:
        if isinstance(self.days, list):
            return ",".join(d.value for d in self.days)
        return str(self.days)

    @model_validator(mode="after")
    def _cadence(self) -> JobSpec:
        if self.every is not None and self.every <= _dt.timedelta(0):
            msg = "every must be positive"
            raise ValueError(msg)
        modes = sum(bool(x) for x in (self.schedule, self.every, self.trigger))
        if modes != 1:
            msg = "a job needs exactly one of schedule, every, or trigger"
            raise ValueError(msg)
        if self.window is not None and self.every is None:
            msg = "window is only valid with every"
            raise ValueError(msg)
        if len(set(self.schedule)) != len(self.schedule):
            msg = "schedule has duplicate times"
            raise ValueError(msg)
        return self

    @property
    def cadence(self) -> str:
        if self.schedule:
            times = ", ".join(f"{t:%H:%M}" for t in sorted(self.schedule))
            return f"at {times} ET ({self.days_label})"
        if self.every is not None:
            secs = int(self.every.total_seconds())
            every = f"{secs // 60}m" if secs % 3600 else f"{secs // 3600}h"
            window = f" {self.window}" if self.window else ""
            return f"every {every}{window} ET ({self.days_label})"
        return f"on {self.trigger}"


class TriggerRule(BaseModel):
    """``on: <event>`` [``if: <condition>``] ``run: <job>``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    on: str
    run: str
    condition: str | None = Field(None, alias="if")

    @model_validator(mode="before")
    @classmethod
    def _yaml_on(cls, v: Any) -> Any:
        # YAML 1.1 (PyYAML) reads a bare `on:` key as boolean True.
        if isinstance(v, dict) and True in v and "on" not in v:
            v = {("on" if k is True else k): val for k, val in v.items()}
        return v

    @field_validator("condition")
    @classmethod
    def _condition(cls, v: str | None) -> str | None:
        if v:
            parse_condition(v)
        return v


class TickSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interval: _dt.timedelta = _dt.timedelta(minutes=5)
    max_lookback: _dt.timedelta = _dt.timedelta(days=7)
    max_trigger_depth: Annotated[int, Field(ge=1, le=20)] = 5
    # E6.2e: a dispatched event whose Investor never claimed its run is released
    # back to the drain this long after its dispatch (and flagged by monitoring).
    dispatch_grace: _dt.timedelta = _dt.timedelta(minutes=10)

    @field_validator("interval", "max_lookback", "dispatch_grace", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @field_validator("dispatch_grace")
    @classmethod
    def _positive_grace(cls, v: _dt.timedelta) -> _dt.timedelta:
        if v <= _dt.timedelta(0):
            msg = "tick.dispatch_grace must be positive"
            raise ValueError(msg)
        return v


class HeartbeatSettings(BaseModel):
    """Which #arc-investor day thread a heartbeat goes to.

    A post belongs to today's session thread while today is a trading session
    and the ET time is before ``day_rollover``. Later posts (e.g. the 22:00
    Scout) and posts on weekends/holidays go to the **next** session's thread,
    so a Sunday-night StockedUp run lands in Monday's thread (D14/D15).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    day_rollover: _dt.time = _dt.time(20, 0)

    @field_validator("day_rollover", mode="before")
    @classmethod
    def _hhmm(cls, v: Any) -> Any:
        return _parse_hhmm(v) if isinstance(v, str) else v


class LoopLayout(enum.StrEnum):
    ROOT_PER_LOOP = "root_per_loop"  # D36: one #arc-investor root line per loop slot
    DAY_THREAD = "day_thread"  # rollback: everything in the day thread (pre-D36)


class LoopSettings(BaseModel):
    """The 5-min trading loop (D31 / D36): cost, overlap and Slack-shape knobs.

    ``job`` names the persona whose chain is the loop. ``max_idle`` bounds the
    change-aware skip: a loop whose input digest equals the previous one skips the
    LLM chain as ``no_change`` unless that long has passed since the last full
    run. ``max_runtime`` is the chain deadline: a step still running at the
    deadline finishes, but no new step starts after it. ``pnl_bucket_pct`` rounds
    P&L (as % of equity) before it enters the digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: str = "director"
    max_idle: _dt.timedelta = _dt.timedelta(minutes=30)
    max_runtime: _dt.timedelta = _dt.timedelta(minutes=4)
    pnl_bucket_pct: Annotated[float, Field(gt=0, le=10)] = 0.5
    post_hold_roots: bool = True
    slack_layout: LoopLayout = LoopLayout.ROOT_PER_LOOP

    @field_validator("max_idle", "max_runtime", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _positive(self) -> LoopSettings:
        if self.max_idle <= _dt.timedelta(0) or self.max_runtime <= _dt.timedelta(0):
            msg = "loop.max_idle and loop.max_runtime must be positive"
            raise ValueError(msg)
        return self


class RoutinesConfig(BaseModel):
    """Top-level ``config/routines.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: Literal["America/New_York"] = "America/New_York"
    tick: TickSettings = Field(default_factory=TickSettings)
    heartbeat: HeartbeatSettings = Field(default_factory=HeartbeatSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)  # E8.2
    loop: LoopSettings = Field(default_factory=LoopSettings)  # D31/D36 trading loop
    context_ttl: dict[str, ContextPolicy] = Field(default_factory=dict)
    sources: dict[str, JobSpec] = Field(default_factory=dict)
    personas: dict[str, JobSpec] = Field(default_factory=dict)
    steps: dict[str, StepSpec] = Field(default_factory=dict)
    triggers: list[TriggerRule] = Field(default_factory=list)

    @field_validator("sources", "personas", "steps", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return {} if v is None else v

    @field_validator("context_ttl")
    @classmethod
    def _known_kinds(cls, v: dict[str, ContextPolicy]) -> dict[str, ContextPolicy]:
        for kind in v:
            if kind not in KINDS:
                msg = f"context_ttl: unknown context kind {kind!r}"
                raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> RoutinesConfig:
        names = [*self.sources, *self.personas]
        for name in names:
            if not _JOB_NAME.match(name):
                msg = f"invalid job name {name!r} (lower-case, dots allowed)"
                raise ValueError(msg)
        dup = set(self.sources) & set(self.personas)
        if dup:
            msg = f"job names used as both source and persona: {sorted(dup)}"
            raise ValueError(msg)
        for name in self.steps:
            if not _JOB_NAME.match(name) or name in self.sources:
                msg = f"invalid step name {name!r} (must be lower-case and not a source)"
                raise ValueError(msg)
        for name, spec in self.sources.items():
            if spec.chain or spec.after_sources or spec.halt_exempt:
                msg = f"source {name!r}: chain/after_sources/halt_exempt are persona-only"
                raise ValueError(msg)
        for name, spec in self.personas.items():
            if name in spec.chain or len(set(spec.chain)) != len(spec.chain):
                msg = f"persona {name!r}: chain repeats a step"
                raise ValueError(msg)
            for step in spec.chain:
                if step in self.sources:
                    msg = f"persona {name!r}: chain step {step!r} is a source"
                    raise ValueError(msg)
                if not _JOB_NAME.match(step):
                    msg = f"persona {name!r}: invalid chain step {step!r}"
                    raise ValueError(msg)
            if spec.halt_exempt and spec.chain:
                msg = f"persona {name!r}: a halt-exempt persona cannot run a chain"
                raise ValueError(msg)
        known_jobs = set(names)
        if "loop" in self.model_fields_set and self.loop.job not in self.personas:
            msg = f"loop.job: unknown persona {self.loop.job!r}"
            raise ValueError(msg)
        for job in self.monitoring.stuck_after_jobs:
            if job not in known_jobs and not self._is_chain_step(job):
                msg = f"monitoring.stuck_after_jobs: unknown job {job!r}"
                raise ValueError(msg)
        for rule in self.all_triggers():
            if rule.run not in self.personas:
                msg = f"trigger on {rule.on!r}: run target {rule.run!r} is not a persona"
                raise ValueError(msg)
            event = rule.on
            if event.endswith(".completed"):
                job = event.removesuffix(".completed")
                if job not in known_jobs and not self._is_chain_step(job):
                    msg = f"trigger on {event!r}: unknown job {job!r}"
                    raise ValueError(msg)
        return self

    def _is_chain_step(self, name: str) -> bool:
        return any(name in p.chain for p in self.personas.values())

    def is_loop(self, job: str) -> bool:
        """True when *job* is the D31 trading-loop persona (its chain is the loop)."""
        return job == self.loop.job and job in self.personas

    # -- lookups -------------------------------------------------------------

    def jobs(self) -> dict[str, tuple[JobKind, JobSpec]]:
        """All enabled jobs (sources first), by name."""
        out: dict[str, tuple[JobKind, JobSpec]] = {}
        for name, spec in self.sources.items():
            if spec.enabled:
                out[name] = (JobKind.SOURCE, spec)
        for name, spec in self.personas.items():
            if spec.enabled:
                out[name] = (JobKind.PERSONA, spec)
        return out

    def job(self, name: str) -> tuple[JobKind, JobSpec] | None:
        if name in self.sources:
            return JobKind.SOURCE, self.sources[name]
        if name in self.personas:
            return JobKind.PERSONA, self.personas[name]
        return None

    def step(self, name: str) -> tuple[JobKind, StepSpec]:
        """Settings for *name* as a runnable unit (job or chain-only step)."""
        found = self.job(name)
        if found is not None:
            return found
        return JobKind.PERSONA, self.steps.get(name, StepSpec())

    def all_triggers(self) -> list[TriggerRule]:
        """Explicit ``triggers`` plus each persona's ``trigger:`` shorthand."""
        rules = list(self.triggers)
        for name, spec in self.personas.items():
            if spec.trigger and spec.enabled:
                rules.append(TriggerRule.model_validate({"on": spec.trigger, "run": name}))
        return rules

    def triggers_for(self, event: str) -> list[TriggerRule]:
        return [r for r in self.all_triggers() if r.on == event]

    def context_policy(self, kind: str, job: str | None = None) -> ContextPolicy:
        """Producer policy for *kind*: job/step override > ``context_ttl`` > none."""
        if job is not None:
            spec = self.step(job)[1]
            if spec.context is not None:
                return spec.context
        return self.context_ttl.get(kind, ContextPolicy())


def load_routines(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], Any] | None = None
) -> RoutinesConfig:
    """Load and validate a routines YAML file.

    *overrides* (D26 control panel: routine enable/cadence, ``path -> value``)
    patch the YAML before validation, so they pass the same checks as the file.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_ROUTINES_PATH
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{p}: top level must be a mapping"
        raise ValueError(msg)
    return RoutinesConfig.model_validate(apply_overrides(data, overrides))
