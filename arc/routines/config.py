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
- ``days: daily | trading | weekdays | month_start`` (default ``daily``), or a
  list of weekdays for weekly jobs (``days: [fri]``). ``month_start`` = the first
  trading session of each calendar month (holiday-aware, E12.2 / D51).
- ``catch_up: {days, until_written: <kind>:<subject>}`` — a slow job's retry
  cadence: on ``catch_up.days`` slots that are not regular slots, the job also
  runs while no entry of that kind/subject was written since its latest regular
  slot (first deploy, or a failed regular run). E.g. a monthly job retries every
  trading session until one run succeeds.
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
- ``halt_exempt: true`` — persona keeps running while halted (Broker reconcile, monitor).
- ``notify: quiet | summary | card`` — heartbeat policy: sources default
  ``quiet`` (folded into the next persona post), personas and chain steps
  default ``card`` (E5.5 digest card; the one-liner when a job has no card).
- ``llm: true|false`` — whether the job calls an LLM (default: personas and
  chain steps yes, sources no). D39: an LLM job takes the global LLM lock only
  when its persona's route is a local model (``local: true`` in
  ``config/llm_routing.yaml``); remote API routes run concurrently.
- ``lane: inline | background`` (D39, default ``inline``) — a ``background`` job's
  scheduled slot is planned and claimed by the tick, then run by a detached
  ``arc routines run-claimed <run_id>`` process, so the tick never waits on it.
- Any other key (e.g. ``only_for: open_positions``, ``channel: <url>``) is kept
  as a handler option in :attr:`JobSpec.options`, so new filters never break
  validation.
- Display keys (E8.8d, D48), read only by the Arc Tower's Session Timeline and
  validated here: ``label`` (friendly name), ``group`` (one of
  :data:`TIMELINE_GROUPS`; a source's band is its D47 category instead),
  ``persona`` (one of :data:`TIMELINE_PERSONAS`, the chip) and ``about`` (one
  line on what the job does). A job without them shows under "Other" with its key.

``steps.<name>`` configures chain steps that are not scheduled on their own
(e.g. ``propose``): same keys as a job minus the cadence.
"""

from __future__ import annotations

import datetime as _dt
import enum
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import structlog
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from arc.context.categories import (
    DEFAULT_CATEGORIES,
    CategorySpec,
    SourceCategory,
    parse_category,
    parse_youtube_category,
)
from arc.context.kinds import KINDS
from arc.context.store import Supersede
from arc.context.ttl import Ttl, parse_duration
from arc.features.market_health import HealthThresholds  # noqa: TC001 - pydantic field
from arc.monitoring.config import MonitoringSettings
from arc.routines.conditions import parse_condition
from arc.universe.tiers import CarryoverSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROUTINES_PATH = REPO_ROOT / "config" / "routines.yaml"

#: E8.8d (D48): Session Timeline bands, in display order (key, label). ``sources`` is
#: assigned by D47 category (one sub-band per category); ``other`` is the fallback.
TIMELINE_GROUPS: tuple[tuple[str, str], ...] = (
    ("sources", "Sources"),
    ("scout", "Scout"),  # E13.7 (D56): the daily slow-feed read
    ("scalp", "Scalp"),
    ("trading_loop", "Trading loop"),
    ("position_management", "Position management"),
    ("post_market", "Post-market"),
    ("other", "Other"),
)
#: D51: universe bookkeeping kinds. A source job writing only these is not a context
#: data source (no D47 category; it never reaches a persona's category block).
UNIVERSE_KINDS: frozenset[str] = frozenset({"universe_tier", "active_universe"})
#: E8.8d: persona chips a job may declare (``persona:``); sources declare none.
#: D56 (E13.2): Investor/Auditor removed; Broker runs ladders + reconcile, Ops the scorecard.
TIMELINE_PERSONAS: tuple[str, ...] = (
    "scout",  # E13.7 (D56)
    "scalp",
    "research",
    "quant",
    "risk",
    "broker",
    "ops",
    "monitor",
)
#: E8.8d: ``about:`` is one line; longer text belongs in docs, not the timeline ⓘ.
ABOUT_MAX_CHARS = 160

#: D54: a source refreshed at most this often (intraday ``every:``) is a fast (Scalp) feed.
_FAST_FEED_MAX_EVERY = _dt.timedelta(minutes=60)
#: E13.5: the per-session template of ``catch_up.until_written`` (``<kind>:{day}``).
SESSION_DAY_TOKEN = "{day}"

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_JOB_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


def _parse_hhmm(text: str) -> _dt.time:
    m = _HHMM.match(str(text).strip())
    if not m:
        msg = f"invalid time {text!r}; expected 24h 'HH:MM' in ET"
        raise ValueError(msg)
    return _dt.time(int(m.group(1)), int(m.group(2)))


def _check_display_keys(name: str, spec: JobSpec) -> None:
    """E8.8d: optional Session Timeline keys (``label``/``group``/``persona``/``about``).

    Typos fail config load, so a job never lands in "Other" by accident.
    """
    opts = spec.options
    groups = [g for g, _ in TIMELINE_GROUPS]
    for key in ("label", "about"):
        if key in opts and (not isinstance(opts[key], str) or not opts[key].strip()):
            msg = f"job {name!r}: {key} must be a non-empty string"
            raise ValueError(msg)
    if len(str(opts.get("about", ""))) > ABOUT_MAX_CHARS:
        msg = f"job {name!r}: about is one line (<= {ABOUT_MAX_CHARS} chars)"
        raise ValueError(msg)
    if "group" in opts and opts["group"] not in groups:
        msg = f"job {name!r}: unknown group {opts['group']!r}; expected {' | '.join(groups)}"
        raise ValueError(msg)
    if "persona" in opts and opts["persona"] not in TIMELINE_PERSONAS:
        names = " | ".join(TIMELINE_PERSONAS)
        msg = f"job {name!r}: unknown persona {opts['persona']!r}; expected {names}"
        raise ValueError(msg)


class Days(enum.StrEnum):
    DAILY = "daily"
    TRADING = "trading"
    WEEKDAYS = "weekdays"
    MONTH_START = "month_start"  # first trading session of each calendar month (E12.2)


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


class Lane(enum.StrEnum):
    """Where a scheduled slot runs (D39)."""

    INLINE = "inline"  # inside the tick process; the tick waits for it
    BACKGROUND = "background"  # detached `arc routines run-claimed` child; the tick doesn't wait


class ContextPolicy(BaseModel):
    """TTL + supersede policy for context entries a producer writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: Ttl | None = None
    supersede: Supersede = Supersede.LATEST


class CatchUp(BaseModel):
    """Retry cadence of a slow job (E12.2): run on ``days`` slots that are not regular
    slots while no ``until_written`` (``kind:subject``) entry was written since the
    job's latest regular slot (or ever, when no regular slot is within ``lookback``).

    E13.5 (D56): ``until_written: "<kind>:{day}"`` is per *session* instead. ``{day}``
    is the session a slot reads (:func:`arc.ingest.cboe_daily.session_date`: today
    from 16:30 ET on a session, else the previous session), and "written" means an
    entry of ``kind`` whose payload ``as_of`` is that date. Every slot of the job
    (regular or catch-up) is then dropped from the plan once its session is written,
    so an evening + morning schedule runs the morning slot only when the evening one
    did not write.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    days: Days | list[Weekday] = Days.TRADING
    until_written: str
    lookback: _dt.timedelta = _dt.timedelta(days=45)

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v: Any) -> Any:
        return _weekday_list(v)

    @field_validator("lookback", mode="before")
    @classmethod
    def _lookback(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @field_validator("until_written")
    @classmethod
    def _target(cls, v: str) -> str:
        kind, sep, subject = v.partition(":")
        if not sep or not subject.strip():
            msg = f"catch_up.until_written must be '<kind>:<subject>', got {v!r}"
            raise ValueError(msg)
        if kind not in KINDS:
            msg = f"catch_up.until_written: unknown context kind {kind!r}"
            raise ValueError(msg)
        if "{" in subject and subject.strip() != SESSION_DAY_TOKEN:
            msg = (
                f"catch_up.until_written: the only template is '<kind>:{SESSION_DAY_TOKEN}', "
                f"got {v!r}"
            )
            raise ValueError(msg)
        return v

    @property
    def target(self) -> tuple[str, str]:
        kind, _, subject = self.until_written.partition(":")
        return kind, subject.strip()

    @property
    def per_session(self) -> bool:
        """E13.5: ``<kind>:{day}``, written once per session (payload ``as_of``)."""
        return self.target[1] == SESSION_DAY_TOKEN


def _weekday_list(v: Any) -> Any:
    """``days: [Fri, mon]`` -> ``["fri", "mon"]`` (each weekday once)."""
    if isinstance(v, list):
        names = [str(d).strip().lower()[:3] for d in v]
        if not names or len(set(names)) != len(names):
            msg = "days list must name each weekday once (e.g. [fri])"
            raise ValueError(msg)
        return names
    return v


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
    # D64 (E14.7): per-kind producer policy, for a job that writes several kinds but
    # overrides only one (the Scout's universe_tier 48 h). Beats `context:` for its kinds.
    context_kinds: dict[str, ContextPolicy] = Field(default_factory=dict)
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
    # E13.9: a loop chain step needing this many seconds of the ``loop.max_runtime``
    # budget is skipped (``step_skipped_deadline``) when less is left; later steps run.
    min_remaining_s: Annotated[int, Field(ge=0)] | None = None

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

    @model_validator(mode="after")
    def _context_kinds(self) -> StepSpec:
        for kind in self.context_kinds:
            if kind not in KINDS:
                msg = f"unknown context kind {kind!r} in context_kinds"
                raise ValueError(msg)
            if kind not in (self.writes or []):
                msg = f"context_kinds: {kind!r} is not in this job's writes"
                raise ValueError(msg)
        return self

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
    # ``auto`` (E13.9) = resolved at load to the fixed :data:`AUTO_CHAINS` entry.
    chain: list[str] = Field(default_factory=list)
    after_sources: bool = False
    ttl: Ttl | None = None
    halt_exempt: bool = False
    enabled: bool = True
    lane: Lane = Lane.INLINE  # D39: background = the tick claims the slot and spawns it
    catch_up: CatchUp | None = None  # E12.2: retry a slow job until it has written

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
        return _weekday_list(v)

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
        if self.catch_up is not None and not self.schedule:
            msg = "catch_up is only valid with schedule"
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
    # E6.2e: a dispatched event whose Broker never claimed its run is released
    # back to the drain this long after its dispatch (and flagged by monitoring).
    dispatch_grace: _dt.timedelta = _dt.timedelta(minutes=10)
    # D39: a background persona with `after_sources: true` waits at most this long for
    # the background sources spawned in the same tick to finish, then reads whatever
    # is committed (it never blocks the tick: it runs in its own process).
    after_sources_wait: _dt.timedelta = _dt.timedelta(minutes=5)

    @field_validator(
        "interval", "max_lookback", "dispatch_grace", "after_sources_wait", mode="before"
    )
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
    and the ET time is before ``day_rollover`` (default ``"24:00"``, i.e. the whole
    calendar day). Posts after an earlier rollover and posts on weekends/holidays
    go to the **next** session's thread,
    so a Sunday-night StockedUp run lands in Monday's thread (D14/D15).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    day_rollover: _dt.time = _dt.time.max  # "24:00": a trading day's posts stay in its thread

    @field_validator("day_rollover", mode="before")
    @classmethod
    def _hhmm(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "24:00":
            return _dt.time.max  # end of day: never roll over before midnight ET
        return _parse_hhmm(v) if isinstance(v, str) else v


class LoopLayout(enum.StrEnum):
    ROOT_PER_LOOP = "root_per_loop"  # D36: one #arc-investor root line per loop slot
    DAY_THREAD = "day_thread"  # rollback: everything in the day thread (pre-D36)


class LoopSettings(BaseModel):
    """The 5-min trading loop (D31 / D36): cost, overlap and Slack-shape knobs.

    ``action_roots`` lists other chained jobs (D38: the position manager) whose
    run gets the same root line in #arc-investor, opened only once the chain has
    a proposal (so closes show as ``SELL: …``) and never for a quiet run.

    ``job`` names the persona whose chain is the loop. ``max_idle`` bounds the
    change-aware skip: a loop whose input digest equals the previous one skips the
    LLM chain as ``no_change`` unless that long has passed since the last full
    run. ``max_runtime`` is the chain deadline: a step still running at the
    deadline finishes, but no new step starts after it. ``pnl_bucket_pct`` rounds
    P&L (as % of equity) before it enters the digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: str = "research"
    max_idle: _dt.timedelta = _dt.timedelta(minutes=30)
    max_runtime: _dt.timedelta = _dt.timedelta(minutes=7)
    pnl_bucket_pct: Annotated[float, Field(gt=0, le=10)] = 0.5
    post_hold_roots: bool = True
    slack_layout: LoopLayout = LoopLayout.ROOT_PER_LOOP
    # D38: other chains that get the same one-line root, but only when they act
    # (a proposal published: close / swap). Quiet runs post no root at all.
    action_roots: tuple[str, ...] = ("positions.evaluate",)
    # D63 (E13.21): fork/join inside the loop chain. Each inner list is a branch of
    # consecutive chain steps; the branches run side by side (own thread + own
    # SQLite connection) and join before the next step. [] = serial (the rollback).
    # RoutinesConfig checks them against the chain and the steps' reads/writes.
    parallel_branches: tuple[tuple[str, ...], ...] = ()

    @field_validator("max_idle", "max_runtime", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> Any:
        return parse_duration(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _positive(self) -> LoopSettings:
        if self.max_idle <= _dt.timedelta(0) or self.max_runtime <= _dt.timedelta(0):
            msg = "loop.max_idle and loop.max_runtime must be positive"
            raise ValueError(msg)
        flat = [s for b in self.parallel_branches for s in b]
        if self.parallel_branches and (
            len(self.parallel_branches) < 2 or any(not b for b in self.parallel_branches)  # noqa: PLR2004
        ):
            msg = "loop.parallel_branches: list at least two non-empty branches (or [] for serial)"
            raise ValueError(msg)
        if len(set(flat)) != len(flat):
            msg = "loop.parallel_branches: a step is listed twice"
            raise ValueError(msg)
        return self


class AdvisoryBand(BaseModel):
    """D87: |Greek $| / equity at or above ``med_pct`` reads Med, above ``high_pct`` High."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    med_pct: Annotated[float, Field(gt=0, le=1)]
    high_pct: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def _ordered(self) -> AdvisoryBand:
        if self.high_pct <= self.med_pct:
            msg = "greek_advisory: high_pct must be above med_pct"
            raise ValueError(msg)
        return self


class GreekAdvisorySettings(BaseModel):
    """D87: info-only Low / Med / High bands for the uncapped Greeks on the Tower.

    Display only: no gate, no halt reads them. Θ = $ per day; Γ = dollar gamma ($Δ change
    for a 1% move in every underlying). Both as a share of equity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    theta: AdvisoryBand = Field(
        default_factory=lambda: AdvisoryBand(med_pct=0.001, high_pct=0.0025)
    )
    gamma: AdvisoryBand = Field(default_factory=lambda: AdvisoryBand(med_pct=0.005, high_pct=0.015))


class TowerOverviewSettings(BaseModel):
    """Arc Tower Overview knobs (E8.8b, D48). Display only: never read by trading code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Recent Activity is a rolling window of this many hours (the API's `activity_hours`
    # query parameter overrides it per request, same bounds).
    activity_hours: Annotated[int, Field(ge=1, le=168)] = 24
    # D87: Low / Med / High advisory bands for the uncapped Greeks (Θ, $Γ).
    greek_advisory: GreekAdvisorySettings = Field(default_factory=GreekAdvisorySettings)


class TowerSettings(BaseModel):
    """``tower:`` in ``config/routines.yaml``: read per request by the Arc Tower."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    overview: TowerOverviewSettings = Field(default_factory=TowerOverviewSettings)
    # E8.8e: display names for change-log actors (Slack user id -> name). An id not in the
    # map shows as is. Display only, never an authorization input (D10 owner check is
    # `owner_slack_user_id`).
    actor_names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names_non_empty(self) -> TowerSettings:
        for k, v in self.actor_names.items():
            if not str(k).strip() or not str(v).strip():
                msg = "tower.actor_names: ids and names must be non-empty"
                raise ValueError(msg)
        return self


# E4.8a (D46/D44): the Finnhub per-ticker kinds a persona may see as compact facts.
FINNHUB_FACT_KINDS: tuple[str, ...] = (
    "earnings_history",
    "insider_activity",
    "analyst_recs",
    "fundamentals",
)
# Persona-level switches that live under ``personas:`` next to the jobs (a scalar,
# not a job mapping). Each maps to the settings block whose ``enabled`` it sets.
PERSONA_FLAGS: tuple[str, ...] = (
    "finnhub_context",
    "scout_buzz_velocity",
    "scalp_movers_context",
    "retail_sentiment_context",  # E14.6
    "research_technicals",  # E16.2 (D76/D78)
    "anti_chase",  # E16.3 (D76/D78)
    "market_health_context",  # E16.4 (D76)
)
#: E13.15 (D56 cutover): switches removed with their off paths. Each is always on
#: now (quant_risk_loop on, scalp_options_tape on, scout_feed on, research_idea_pool
#: all, research_compact_prompt compact, exit_path research); a leftover key in a
#: local routines.yaml is ignored with a log.
REMOVED_PERSONA_SWITCHES: frozenset[str] = frozenset(
    {
        "quant_risk_loop",
        "scalp_options_tape",
        "scout_feed",
        "research_idea_pool",
        "research_compact_prompt",
        "exit_path",
    }
)

#: E13.15 (D56): the fixed chains ``chain: auto`` resolves to. Exits run before
#: opens in the trading loop (a close frees buying power; ``exits.mandatory`` is the
#: deterministic floor); ``positions.evaluate`` is marks plus the mandatory floor.
AUTO_CHAINS: dict[str, tuple[str, ...]] = {
    "research": (
        "exits.mandatory",
        "quant.exit",
        "risk.exit",
        "quant.open",
        "risk.open",
        "quant.revise",
        "quant.propose",
        "broker.execute",
    ),
    "positions.evaluate": ("exits.mandatory", "broker.execute"),
}


# Persona-level choice switches (E12.5): ``personas.<name>: <choice>`` sets the
# ``mode`` of the same-named settings block. The first choice is the control.
PERSONA_CHOICES: dict[str, tuple[str, ...]] = {
    "director_diversification": ("strict", "relaxed"),
}


class RelaxedConcentration(BaseModel):
    """E12.5 (D51): the concentration-flag thresholds used in ``relaxed`` mode.

    They never tighten the strict (Slack-tunable ``portfolio.*_max_pct``) values:
    the effective threshold is the larger of the two.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sector_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.55
    stance_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.85
    expiry_max_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.70


class ResearchDiversificationSettings(BaseModel):
    """E12.5 (D51, D44): how strictly Research stage diversifies the book.

    ``mode`` comes from ``personas.director_diversification: strict | relaxed``
    (default ``strict`` = the E5.9 behaviour, byte-identical prompts). ``relaxed``:
    the prompt says correlation / a shared industry alone is no reason to exclude,
    an ``adds_concentration`` pick is dropped only when its sector is flagged **and**
    the book already holds ``max_names_per_industry`` names in its industry (stance
    skew alone no longer drops), and the flag thresholds are the ``relaxed`` block.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["strict", "relaxed"] = "strict"
    max_names_per_industry: Annotated[int, Field(ge=1, le=10)] = 2
    relaxed: RelaxedConcentration = Field(default_factory=RelaxedConcentration)

    @property
    def is_relaxed(self) -> bool:
        return self.mode == "relaxed"

    def thresholds(self, sector: float, stance: float, expiry: float) -> tuple[float, float, float]:
        """The (sector, stance, expiry) flag thresholds for this mode, given the strict ones."""
        if not self.is_relaxed:
            return sector, stance, expiry
        r = self.relaxed
        return (
            max(sector, r.sector_max_pct),
            max(stance, r.stance_max_pct),
            max(expiry, r.expiry_max_pct),
        )


def parse_choice(v: Any, choices: tuple[str, ...], *, where: str) -> str:
    if isinstance(v, str) and v.strip().lower() in choices:
        return v.strip().lower()
    msg = f"{where}: expected {' | '.join(choices)}, got {v!r}"
    raise ValueError(msg)


def parse_on_off(v: Any, *, where: str) -> bool:
    """``on``/``off`` (YAML 1.1 also reads bare ``on``/``off`` as booleans)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in {"on", "off"}:
        return v.strip().lower() == "on"
    msg = f"{where}: expected on | off, got {v!r}"
    raise ValueError(msg)


class ScoutBuzzVelocitySettings(BaseModel):
    """E14.5 (D60, D44): Reddit mention velocity in the Scout's retail-buzz section.

    ``enabled`` comes from ``personas.scout_buzz_velocity: off | on`` (default off = the
    Scout prompt is byte-identical to E13.20). The velocity knobs (``smoothing``,
    ``min_mentions``) are the ``universe.trending`` job's ``velocity`` block, so the
    Scout and the ranker's velocity arm read one definition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False


class RetailSentimentContextSettings(BaseModel):
    """E14.6 (D60, D44): Stocktwits sentiment in the Scout and Research prompts.

    ``enabled`` comes from ``personas.retail_sentiment_context: off | on`` (default off
    = both prompts byte-identical to before E14.6). On: the Scout gets a code-built
    "Retail sentiment" block (top ``scout_top`` tickers by tagged count) and each
    Research pool line gets one ``ST 80% bull (10 tagged, 2.7h)`` fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    scout_top: int = Field(15, ge=1, le=55)


class FinnhubContextSettings(BaseModel):
    """E4.8a: Finnhub facts in the Scalp/Research prompts (default off, D44 experiment).

    ``enabled`` comes from ``personas.finnhub_context: off | on``; the other knobs
    are the ``finnhub_context:`` block. Off = the prompts are byte-identical to the
    pre-E4.8a prompts and the D31 digest is unchanged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    max_chars_per_ticker: Annotated[int, Field(ge=80, le=1000)] = 300
    scalp_max_tickers: Annotated[int, Field(ge=1, le=50)] = 8
    research_max_tickers: Annotated[int, Field(ge=1, le=50)] = 10
    # A part whose payload ``as_of`` is older than this many days is omitted (the
    # context TTL already expires the entry; this guards a stale fetch date).
    max_age_days: dict[str, Annotated[int, Field(ge=1, le=60)]] = Field(
        default_factory=lambda: {
            "earnings_history": 8,
            "insider_activity": 2,
            "analyst_recs": 8,
            "fundamentals": 8,
        }
    )
    # Market-cap bucket floors in USD millions, checked largest first; below all = micro.
    cap_buckets_musd: dict[str, Annotated[float, Field(gt=0)]] = Field(
        default_factory=lambda: {
            "mega": 200_000.0,
            "large": 10_000.0,
            "mid": 2_000.0,
            "small": 300.0,
        }
    )

    @field_validator("max_age_days")
    @classmethod
    def _known(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = sorted(set(v) - set(FINNHUB_FACT_KINDS))
        if unknown:
            msg = f"finnhub_context.max_age_days: unknown kinds {unknown}"
            raise ValueError(msg)
        return {**{k: 8 for k in FINNHUB_FACT_KINDS}, **v}

    def prompt_options(self, tickers: list[str], max_tickers: int) -> dict[str, Any]:
        """The JSON-able ``ticker_facts`` prompt input (recorded for journal replay)."""
        return {
            "tickers": tickers[:max_tickers],
            "max_chars": self.max_chars_per_ticker,
            "max_age_days": dict(self.max_age_days),
            "cap_buckets_musd": dict(self.cap_buckets_musd),
        }


_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class ResearchTechnicalsSettings(BaseModel):
    """E16.2 (D76/D78): the code-rendered ``tech …`` segment on Research's regime lines.

    ``enabled`` comes from ``personas.research_technicals: off | on`` (D78 ships it
    on; ``off`` is the rollback and keeps the Research prompt byte-identical to before
    E16.2). The indicators are always computed and stored on the ``regime`` entry
    (audit); this switch only decides whether Research sees them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False


class MarketHealthContextSettings(BaseModel):
    """E16.4 (D76, D44): the code-rendered ``Market health (…)`` line for Research.

    ``enabled`` comes from ``personas.market_health_context: off | on`` (D83 ships it
    **on**; ``off`` is the rollback: the Research prompt is byte-identical to before
    E16.4). The ``market_health`` job writes its daily entry whatever this says (audit);
    the switch only decides whether Research sees it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False


class AntiChaseSettings(BaseModel):
    """E16.3 (D76/D78): the deterministic anti-chase entry filter.

    ``enabled`` comes from ``personas.anti_chase: off | on`` (D78 ships it **on**;
    ``off`` is the rollback: the pipeline is byte-identical to before E16.3). On:
    after Research ranks and before Quant prices, a bullish / bearish idea whose
    account profile maps it to long premium only (``cash_debit`` / ``cash_long_only``
    debit structures) is dropped when its move is already stretched, journaled
    ``stretched_entry`` with the numbers that tripped it. Neutral ideas and any idea
    the profile could structure as a credit spread are never filtered. Missing
    technicals keep the idea (journaled ``technicals_missing``): an optional filter,
    not a safety rule. Exits are untouched. The rule itself is
    :func:`arc.features.technicals.is_stretched`.

    ``combine: all`` (D78): stretch >= ``max_stretch_atr`` **and** RSI14 >=
    ``rsi_overbought`` (bears: <= -stretch and RSI <= ``rsi_oversold``). ``any`` is the
    D76 card rule (stretch, **or** RSI near the 20-day extreme), kept for XP-13.
    ``vwap: on`` (default off) adds the intraday VWAP stretch input (one 5-min bars
    request per surviving directional idea, on the shared ``alpaca_data:calls``
    budget; a failed fetch skips that part, journaled ``vwap_missing``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    combine: Literal["all", "any"] = "all"
    max_stretch_atr: Annotated[float, Field(gt=0.0, le=10.0)] = 2.5
    rsi_overbought: Annotated[float, Field(gt=50.0, le=100.0)] = 75.0
    rsi_oversold: Annotated[float, Field(ge=0.0, lt=50.0)] = 25.0
    max_dist_high20_atr: Annotated[float, Field(ge=0.0, le=5.0)] = 0.25
    vwap: bool = False
    max_vwap_stretch_atr: Annotated[float, Field(gt=0.0, le=10.0)] = 0.75
    vwap_bar_minutes: Literal[5] = 5

    @field_validator("vwap", mode="before")
    @classmethod
    def _vwap(cls, v: Any) -> bool:
        return parse_on_off(v, where="anti_chase.vwap")

    def rule(self) -> Any:
        """The pure rule's thresholds (:class:`arc.features.technicals.AntiChaseRule`)."""
        from arc.features.technicals import AntiChaseRule

        return AntiChaseRule(
            combine=self.combine,
            max_stretch_atr=self.max_stretch_atr,
            rsi_overbought=self.rsi_overbought,
            rsi_oversold=self.rsi_oversold,
            max_dist_high20_atr=self.max_dist_high20_atr,
            vwap=self.vwap,
            max_vwap_stretch_atr=self.max_vwap_stretch_atr,
        )


class ScalpMoversContextSettings(BaseModel):
    """E14.3 (D60, D44): the "Tape movers" block in the Scalp prompt (default off).

    ``enabled`` comes from ``personas.scalp_movers_context: off | on``; the knobs are
    the ``scalp_movers_context:`` block. Off = the Scalp prompt is byte-identical to
    the pre-E14.3 prompt (the ``market_movers`` source still writes its context).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    max_lines: Annotated[int, Field(ge=1, le=10)] = 10


class FunnelScalp(BaseModel):
    """D56 ``funnel.scalp``: the Scalp's doc budget split (fixed by D56)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_budget_split: Literal["equal"] = "equal"


class FunnelScout(BaseModel):
    """D56 ``funnel.scout``: discovery tier size, video budget split, under-fill alert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_discovery: Annotated[int, Field(ge=0, le=25)] = 25  # D58 (was 20)
    video_budget_split: Literal["equal"] = "equal"
    # owner decision 1 (D56): the ``coverage:scout`` alert fires below this many names
    min_discovery_alert: Annotated[int, Field(ge=0, le=20)] = 5


class FunnelResearch(BaseModel):
    """D56 ``funnel.research``: Scout-only ideas Research may weigh per loop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_scout_only_ideas: Annotated[int, Field(ge=0, le=50)] = 20


class FunnelConfig(BaseModel):
    """D56 (E13.3, folds E5.14's config half): the ``funnel:`` block.

    Config only in E13.3: the Scout (E13.7), the coverage alert and the Tower funnel
    report (E13.14) read it. No ``most_active_input`` and no ``discovery_backfill``
    (owner decisions 1 and 2: discovery comes from the Scout's YouTube calls only).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scalp: FunnelScalp = Field(default_factory=FunnelScalp)
    scout: FunnelScout = Field(default_factory=FunnelScout)
    research: FunnelResearch = Field(default_factory=FunnelResearch)


class OptionsSlowSettings(BaseModel):
    """E13.5 (D56): the ``options_slow:`` block (Cboe daily stats + CFE VX curve knobs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # VX curve shape: |slope month1 -> month2| below this many percent = flat.
    vx_flat_band: Annotated[float, Field(ge=0, le=10)] = 0.5
    # Measurement only (not runtime-tunable): when > 0, an evening options_daily /
    # vix_futures run that finds the session unpublished re-probes once a minute for
    # up to this many minutes and records how long it waited (metric probe_wait_s).
    publish_probe_minutes: Annotated[int, Field(ge=0, le=60)] = 0


class OptionsFastVixFlags(BaseModel):
    """E13.6: VIX levels that label the tape (``vix_gt_25`` / ``vix_gt_35``); labels only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vix_gt_25: Annotated[float, Field(gt=0, le=100)] = 25.0
    vix_gt_35: Annotated[float, Field(gt=0, le=100)] = 35.0


class OptionsFastSettings(BaseModel):
    """E13.6 (D56): the ``options_fast:`` block (Cboe delayed tape knobs). Not tunable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vix_flags: OptionsFastVixFlags = Field(default_factory=OptionsFastVixFlags)
    # Size guard on one symbol_data CSV download (~1.7 MB measured 2026-10-06).
    max_csv_bytes: Annotated[int, Field(ge=1_000_000, le=100_000_000)] = 20_000_000


class IvBackfillSettings(BaseModel):
    """E16.1 (D76): the ``iv_backfill:`` block (pre-market ``iv.backfill`` top-up knobs, D81).

    Context data only (IV rank / percentile on ``regime``); never a gate input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Short names backfilled per run (open underlyings first, then active-list order).
    max_tickers_per_run: Annotated[int, Field(ge=1, le=60)] = 10
    # No new ticker starts after this many seconds (resumable: the next run continues).
    max_runtime_s: Annotated[int, Field(ge=60, le=3600)] = 900
    # First day backfilled (Alpaca's option-bars history starts 2024-02).
    since: _dt.date = _dt.date(2024, 3, 1)


class RoutinesUniverseSettings(BaseModel):
    """D64 (E14.7): the ``universe:`` block (tier-writer knobs shared by the Scout's
    discovery tier and ``universe.trending``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    carryover: CarryoverSettings = Field(default_factory=CarryoverSettings)


class RoutinesConfig(BaseModel):
    """Top-level ``config/routines.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: Literal["America/New_York"] = "America/New_York"
    tick: TickSettings = Field(default_factory=TickSettings)
    heartbeat: HeartbeatSettings = Field(default_factory=HeartbeatSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)  # E8.2
    loop: LoopSettings = Field(default_factory=LoopSettings)  # D31/D36 trading loop
    tower: TowerSettings = Field(default_factory=TowerSettings)  # E8.8b Arc Tower display knobs
    # D47/D49: six equal-weight source categories with a freshness window each. A
    # missing block (or a category missing from it) uses DEFAULT_CATEGORIES.
    categories: dict[SourceCategory, CategorySpec] = Field(
        default_factory=lambda: dict(DEFAULT_CATEGORIES)
    )
    context_ttl: dict[str, ContextPolicy] = Field(default_factory=dict)
    sources: dict[str, JobSpec] = Field(default_factory=dict)
    personas: dict[str, JobSpec] = Field(default_factory=dict)
    steps: dict[str, StepSpec] = Field(default_factory=dict)
    triggers: list[TriggerRule] = Field(default_factory=list)
    # E4.8a: knobs + the ``personas.finnhub_context`` flag (as ``enabled``).
    finnhub_context: FinnhubContextSettings = Field(default_factory=FinnhubContextSettings)
    # E14.5: the ``personas.scout_buzz_velocity`` flag (as ``enabled``).
    scout_buzz_velocity: ScoutBuzzVelocitySettings = Field(
        default_factory=ScoutBuzzVelocitySettings
    )
    # E14.3: knobs + the ``personas.scalp_movers_context`` flag (as ``enabled``).
    scalp_movers_context: ScalpMoversContextSettings = Field(
        default_factory=ScalpMoversContextSettings
    )
    # E14.6: the ``personas.retail_sentiment_context`` flag (as ``enabled``).
    retail_sentiment_context: RetailSentimentContextSettings = Field(
        default_factory=RetailSentimentContextSettings
    )
    # E16.2 (D76/D78): knobs + the ``personas.research_technicals`` flag (as ``enabled``).
    research_technicals: ResearchTechnicalsSettings = Field(
        default_factory=ResearchTechnicalsSettings
    )
    # E16.3 (D76/D78): knobs + the ``personas.anti_chase`` flag (as ``enabled``).
    anti_chase: AntiChaseSettings = Field(default_factory=AntiChaseSettings)
    # E16.4 (D76): the ``personas.market_health_context`` flag (as ``enabled``) and the
    # ``market_health:`` label thresholds / staleness limit of the daily read.
    market_health_context: MarketHealthContextSettings = Field(
        default_factory=MarketHealthContextSettings
    )
    market_health: HealthThresholds = Field(default_factory=HealthThresholds)
    # E12.5: knobs + the ``personas.director_diversification`` switch (as ``mode``).
    director_diversification: ResearchDiversificationSettings = Field(
        default_factory=ResearchDiversificationSettings
    )
    funnel: FunnelConfig = Field(default_factory=FunnelConfig)  # D56 (E13.3)
    options_slow: OptionsSlowSettings = Field(default_factory=OptionsSlowSettings)  # E13.5
    options_fast: OptionsFastSettings = Field(default_factory=OptionsFastSettings)  # E13.6
    universe: RoutinesUniverseSettings = Field(default_factory=RoutinesUniverseSettings)  # D64
    iv_backfill: IvBackfillSettings = Field(default_factory=IvBackfillSettings)  # E16.1 (D76)

    @model_validator(mode="before")
    @classmethod
    def _persona_flags(cls, data: Any) -> Any:
        """Lift scalar ``personas.<flag>`` switches out of the job map (input not mutated)."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if not isinstance(data.get("personas"), dict):
            return out
        personas = dict(data["personas"])
        for flag in sorted(REMOVED_PERSONA_SWITCHES & set(personas)):
            structlog.get_logger(__name__).warning(
                "routines.removed_switch_ignored", switch=f"personas.{flag}", value=personas[flag]
            )
            del personas[flag]
        for flag in PERSONA_FLAGS:
            if flag not in personas:
                continue
            raw = personas.pop(flag)
            block = dict(out.get(flag) or {})
            if "enabled" in block:
                msg = f"{flag}.enabled: set the switch as personas.{flag}: on | off"
                raise ValueError(msg)
            block["enabled"] = parse_on_off(raw, where=f"personas.{flag}")
            out[flag] = block
        for flag, choices in PERSONA_CHOICES.items():
            if flag not in personas:
                continue
            raw = personas.pop(flag)
            block = dict(out.get(flag) or {})
            if "mode" in block:
                msg = f"{flag}.mode: set the switch as personas.{flag}: {' | '.join(choices)}"
                raise ValueError(msg)
            block["mode"] = parse_choice(raw, choices, where=f"personas.{flag}")
            out[flag] = block
        for name, chain in AUTO_CHAINS.items():
            body = personas.get(name)
            if isinstance(body, dict) and body.get("chain") == "auto":
                personas[name] = {**body, "chain": list(chain)}
        out["personas"] = personas
        return out

    @field_validator("sources", "personas", "steps", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return {} if v is None else v

    @field_validator("categories", mode="before")
    @classmethod
    def _categories(cls, v: Any) -> Any:
        """Strict D47 names (no aliases here); unset categories keep their default."""
        if v is None:
            return dict(DEFAULT_CATEGORIES)
        if not isinstance(v, dict):
            msg = "categories: must be a mapping of category -> {weight, max_age, label}"
            raise ValueError(msg)
        out: dict[SourceCategory, Any] = dict(DEFAULT_CATEGORIES)
        for key, spec in v.items():
            try:  # renamed names load as logged aliases; `video`/`macro_data` refuse
                cat = parse_category(key, where="categories")
            except ValueError as exc:
                if "split in two" in str(exc) or "was removed" in str(exc):
                    raise
                names = " | ".join(c.value for c in SourceCategory)
                msg = f"categories: unknown category {key!r}; expected {names}"
                raise ValueError(msg) from None
            if isinstance(spec, dict):  # partial entry: unset fields keep the default
                base = DEFAULT_CATEGORIES[cat]
                spec = {"weight": base.weight, "max_age": base.max_age, "label": base.label, **spec}
            out[cat] = spec
        return out

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
            self._check_source_category(name, spec)
            self._check_source_feed(name, spec)
            if name == "retail_buzz":  # E13.19: the inputs block validates at load
                from arc.ingest.retail_buzz_config import RetailBuzzConfig

                RetailBuzzConfig.from_options(spec.options)
            if name == "retail_sentiment":  # E14.6: the knobs validate at load
                from arc.ingest.retail_sentiment_config import RetailSentimentConfig

                RetailSentimentConfig.from_options(spec.options)
            if name == "ticker_news":  # E14.1: the inputs block validates at load
                from arc.ingest.ticker_news_config import TickerNewsConfig

                TickerNewsConfig.from_options(spec.options)
            if name in HORIZON_JOBS:  # E20.2 (D85): the job option is the only source
                horizon_days_option(name, spec.options)
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
        for name, spec in [*self.sources.items(), *self.personas.items()]:
            # D39: a background slot is one detached run; chains and the D31 loop
            # (deadline, Slack root, non-overlap) stay in the tick process.
            if spec.lane is Lane.BACKGROUND and (
                spec.chain or spec.trigger or name == self.loop.job
            ):
                msg = f"job {name!r}: lane background is only for scheduled jobs without a chain"
                raise ValueError(msg)
        for name, spec in [*self.sources.items(), *self.personas.items()]:
            _check_display_keys(name, spec)
        known_jobs = set(names)
        if "loop" in self.model_fields_set and self.loop.job not in self.personas:
            msg = f"loop.job: unknown persona {self.loop.job!r}"
            raise ValueError(msg)
        self._check_parallel_branches()
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

    def branch_conflicts(self, branches: Sequence[Sequence[str]]) -> list[str]:
        """D63: why *branches* may NOT run side by side (``[]`` = independent).

        Computed from the steps' declared ``reads``/``writes`` (D27, enforced at
        write time): no branch may read or write a kind another branch writes,
        except kinds the writer stores with ``supersede: accumulate`` (e.g. ``note``:
        entries stack, so both branches may append). Undeclared ``reads`` (= every
        kind) or ``writes`` conflict with any non-accumulate write.
        """
        out: list[str] = []
        for a, branch_a in enumerate(branches):
            for b, branch_b in enumerate(branches):
                if a == b:
                    continue
                for writer in branch_a:
                    wspec = self.step(writer)[1]
                    if wspec.writes is None:
                        out.append(f"{writer}: writes undeclared")
                        continue
                    shared = {
                        k
                        for k in wspec.writes
                        if self.context_policy(k, writer).supersede is not Supersede.ACCUMULATE
                    }
                    for other in branch_b:
                        ospec = self.step(other)[1]
                        reads = set(KINDS) if ospec.reads is None else set(ospec.reads)
                        for kind in sorted(shared & reads):
                            out.append(f"{other} reads {kind!r}, written by {writer}")
                        for kind in sorted(shared & set(ospec.writes or [])):
                            if a < b:
                                out.append(f"{other} and {writer} both write {kind!r}")
        return sorted(set(out))

    def _check_parallel_branches(self) -> None:
        """D63: ``loop.parallel_branches`` must be a contiguous run of LLM steps of the
        loop chain (each branch in chain order, after the root and before the first
        deterministic step that follows them), with independent reads/writes."""
        branches = self.loop.parallel_branches
        if not branches:
            return
        where = "loop.parallel_branches"
        found = self.personas.get(self.loop.job)
        if found is None:
            msg = f"{where}: loop.job {self.loop.job!r} is not a persona"
            raise ValueError(msg)
        chain = list(found.chain)
        if not any(s in chain for b in branches for s in b):
            # loop.job re-pointed at another persona (tests, a rollback): the branches
            # name none of its steps, so its chain simply runs serially.
            structlog.get_logger(__name__).info(
                "routines.parallel_branches_unused", loop_job=self.loop.job
            )
            return
        for step in (s for b in branches for s in b):
            if step not in chain:
                msg = f"{where}: {step!r} is not a step of the {self.loop.job} chain"
                raise ValueError(msg)
            kind, spec = self.step(step)
            llm = spec.llm if spec.llm is not None else kind is JobKind.PERSONA
            if not llm:
                msg = (
                    f"{where}: {step!r} is deterministic (llm: false); account steps "
                    "(exits.mandatory, quant.propose, broker.execute) stay serial"
                )
                raise ValueError(msg)
        for b in branches:
            idx = [chain.index(s) for s in b]
            if idx != list(range(idx[0], idx[0] + len(idx))):
                msg = f"{where}: branch {list(b)} must list consecutive chain steps in order"
                raise ValueError(msg)
        idx = sorted(chain.index(s) for b in branches for s in b)
        if idx != list(range(idx[0], idx[0] + len(idx))):
            msg = f"{where}: the branches must cover consecutive chain steps (no gap)"
            raise ValueError(msg)
        conflicts = self.branch_conflicts(branches)
        if conflicts:
            msg = f"{where}: branches are not independent: {'; '.join(conflicts)}"
            raise ValueError(msg)

    @staticmethod
    def _check_source_feed(name: str, spec: JobSpec) -> None:
        """D54: a declared ``feed:`` must match the source's refresh cadence.

        ``scalp`` (fast feed) needs an intraday ``every:`` of at most 60 min; ``scout``
        (slow feed) needs a ``schedule:`` (a few times a day or slower) and no intraday
        ``every:`` of 60 min or less. A source without ``feed:`` is the Scalp's.
        """
        raw = spec.options.get("feed")
        declared = [(raw, f"source {name!r}")] if raw is not None else []
        for f in spec.options.get("feeds") or []:  # D55: each RSS feed declares its feed
            if isinstance(f, dict) and f.get("feed") is not None:
                declared.append(
                    (f["feed"], f"source {name!r} feed {f.get('name') or f.get('url')!r}")
                )
        fast = spec.every is not None and spec.every <= _FAST_FEED_MAX_EVERY
        for value, where in declared:
            if value == "scalp" and not fast:
                msg = f"{where}: feed scalp needs an intraday `every:` of at most 60m (D54)"
                raise ValueError(msg)
            if value == "scout" and (fast or not spec.schedule):
                msg = (
                    f"{where}: feed scout needs a `schedule:` and no intraday "
                    "`every:` of 60m or less (D54)"
                )
                raise ValueError(msg)
            if value not in ("scalp", "scout"):
                msg = f"{where}: feed must be scalp | scout, got {value!r} (D54)"
                raise ValueError(msg)

    @staticmethod
    def _check_source_category(name: str, spec: JobSpec) -> None:
        """D47/D56: a source that writes context declares its category or ``reference: true``.

        Exactly one of the two (job or every feed): neither or both fails. Unknown
        names fail; old names load as logged aliases for one release. A per-source
        ``max_age:`` override must be a valid TTL.
        """
        opts = spec.options
        if "max_age" in opts:
            Ttl.model_validate(opts["max_age"])
        job_cat = opts.get("category")
        reference = opts.get("reference")
        if reference is not None and not isinstance(reference, bool):
            msg = f"source {name!r}: `reference:` must be true or false, got {reference!r}"
            raise ValueError(msg)
        if reference and job_cat is not None:
            msg = (
                f"source {name!r}: declares both `category:` and `reference: true`; "
                "a source is either in a category or reference data, never both (D56)"
            )
            raise ValueError(msg)
        if reference and (opts.get("feeds") or opts.get("channels")):
            msg = f"source {name!r}: `reference: true` is a job-level flag (no feeds/channels; D56)"
            raise ValueError(msg)
        if reference:
            return
        if job_cat is not None:
            parse_category(job_cat, where=f"sources.{name}")
        channels = opts.get("channels")
        if channels:  # D49: each YouTube channel declares its own category
            if job_cat is not None:
                msg = (
                    f"source {name!r}: remove the job-level `category:` and set "
                    "`category: youtube_macro | youtube_micro` on each channel (D49)"
                )
                raise ValueError(msg)
            for raw in channels:
                slug = raw.get("slug") if isinstance(raw, dict) else raw
                cat = raw.get("category") if isinstance(raw, dict) else None
                parse_youtube_category(cat, where=f"sources.{name}.channels.{slug}")
            return
        feeds = opts.get("feeds") or []
        for raw in feeds:
            if isinstance(raw, dict):
                if raw.get("category") is not None:
                    parse_category(raw["category"], where=f"sources.{name}.feeds")
                if raw.get("max_age") is not None:
                    Ttl.model_validate(raw["max_age"])
        if not set(spec.writes or []) - UNIVERSE_KINDS:
            # writes nothing, or only D51 universe bookkeeping: not a context data source
            return
        if job_cat is not None:
            return
        uncategorised = [
            (raw.get("name") or raw.get("url")) if isinstance(raw, dict) else raw
            for raw in feeds
            if not (isinstance(raw, dict) and raw.get("category") is not None)
        ]
        if feeds and not uncategorised:
            return
        names = " | ".join(c.value for c in SourceCategory)
        what = f"feeds {uncategorised}" if feeds else "the job"
        msg = (
            f"source {name!r}: {what} must declare `category:` ({names}) "
            "or `reference: true` (D47, D56)"
        )
        raise ValueError(msg)

    def category_spec(self, category: SourceCategory) -> CategorySpec:
        """The ``categories:`` entry for *category* (default when not configured)."""
        return self.categories.get(category) or DEFAULT_CATEGORIES[category]

    def is_reference(self, job: str) -> bool:
        """D56: source *job* is reference data (``reference: true``, no category)."""
        spec = self.sources.get(job)
        return bool(spec is not None and spec.options.get("reference") is True)

    def source_category(self, job: str) -> SourceCategory | None:
        """Category declared by source *job* (``None`` for an undeclared/feed-only job
        or reference data)."""
        spec = self.sources.get(job)
        raw = spec.options.get("category") if spec is not None else None
        return parse_category(raw, where=f"sources.{job}") if raw is not None else None

    def source_max_age(self, job: str, category: SourceCategory | None = None) -> Ttl:
        """D47 freshness window: the job's own ``max_age:`` else its category's."""
        spec = self.sources.get(job)
        raw = spec.options.get("max_age") if spec is not None else None
        if raw is not None:
            return Ttl.model_validate(raw)
        cat = category or self.source_category(job) or SourceCategory.MARKET_NEWS
        return self.category_spec(cat).max_age

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
        """Producer policy for *kind*: job/step ``context_kinds`` > job/step ``context``
        > ``context_ttl`` > none."""
        if job is not None:
            spec = self.step(job)[1]
            if kind in spec.context_kinds:
                return spec.context_kinds[kind]
            if spec.context is not None:
                return spec.context
        return self.context_ttl.get(kind, ContextPolicy())


#: E20.2 (D85): source jobs whose look-ahead is the required ``horizon_days:`` option.
HORIZON_JOBS: frozenset[str] = frozenset({"macro_calendar", "ex_dividend"})


def horizon_days_option(job: str, options: Mapping[str, Any]) -> int:
    """The required ``horizon_days:`` option (an int, 1-180) of *job*."""
    value = options.get("horizon_days")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 180:
        msg = f"source {job!r}: horizon_days must be an int 1-180 (got {value!r})"
        raise ValueError(msg)
    return value


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
