"""The routine dispatcher: ``arc routines tick`` (D16).

One Hermes cron job calls :meth:`Dispatcher.tick` every 5 minutes. Each tick:

1. Expires context entries whose TTL has passed.
2. Works out, per job, which slots fell due since that job's cursor
   (bounded by ``tick.max_lookback``). Several missed slots collapse into
   **one** run of the latest slot, and only while it is inside the job's
   catch-up window (``ttl``, or one period / 2 h by default). A slot outside
   the window is recorded as ``skipped``.
3. Runs sources first, then personas, in slot order, so a persona with
   ``after_sources: true`` always sees the docs fetched in the same tick.
4. While halted (``!halt``), every persona job except ``halt_exempt`` ones
   (Auditor) is recorded as ``skipped``. Sources keep fetching.
5. Runs chains step by step under one ``chain_run_id``; each step records the
   snapshot it read. A failed step stops the chain and alerts; re-running the
   chain resumes from the failed step.
6. Fires trigger rules on ``<job>.completed`` (in-process, with the job's
   metrics as the condition environment) and on queued external events
   (``approval``, ``halt``, ... from ``arc routines emit``).

A ``(job, scheduled_for)`` unique key in ``routine_runs`` means a duplicate
tick never runs a job twice; an event-triggered run is unique per
``(job, event_id)`` instead (E6.2d), so events sharing a timestamp each run. File
locks (per job + one global LLM lock) stop
overlapping ticks from running the same job or two LLM jobs at once; a job whose
lock is busy is deferred to the next tick without being recorded.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.store import ContextSnapshot, ContextStore
from arc.monitoring.correlation import bind as bind_ids
from arc.routines.conditions import evaluate_condition
from arc.routines.config import JobKind, Notify
from arc.routines.handlers import (
    Handler,
    JobContext,
    JobResult,
    JobSkippedError,
    RunEnv,
    resolve_handler,
)
from arc.routines.heartbeat import Heartbeats, LogNotifier, Notifier
from arc.routines.locks import LLM_LOCK, LockBusyError, LockManager, NullLocks
from arc.routines.runs import (
    RoutineEvent,
    RoutineEventRepo,
    RoutineRun,
    RoutineRunRepo,
    RoutineStateRepo,
    RunStatus,
)
from arc.routines.schedule import catchup_deadline, slots_between
from arc.utils.calendar import ET, now_et, session_phase

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from arc.config import ArcSettings
    from arc.routines.config import JobSpec, RoutinesConfig, StepSpec

log = structlog.get_logger(__name__)

_CURSOR = "cursor:{job}"
_LAST_TICK = "dispatcher:last_tick"
# E6.2d: an approval event deferred by a halt (its lapse is then "under halt").
_HALT_DEFERRED = "halt_deferred:{event}"


# ---------------------------------------------------------------------------
# Plan / report types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DueJob:
    """A job slot the tick decided about."""

    job: str
    kind: JobKind
    slot: _dt.datetime
    action: str  # run | skip-halted | skip-missed
    collapsed: int = 0  # earlier missed slots folded into this one
    chain: tuple[str, ...] = ()
    note: str = ""


@dataclass
class Outcome:
    """What happened to one job/step (a routine_runs row, or a non-recorded decision)."""

    job: str
    scheduled_for: _dt.datetime
    status: str  # ok | failed | skipped | duplicate | deferred | planned
    reason: str
    run_id: str | None = None
    chain_run_id: str | None = None
    step_index: int = 0
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickReport:
    now: _dt.datetime
    since: _dt.datetime | None
    dry_run: bool
    halted: bool
    expired: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def lines(self) -> list[str]:
        head = f"tick @ {self.now:%Y-%m-%d %H:%M %Z}" + (" (dry-run)" if self.dry_run else "")
        if self.since is not None:
            head += f" · window ({self.since:%Y-%m-%d %H:%M} → {self.now:%H:%M}]"
        if self.halted:
            head += " · HALTED"
        out = [head]
        if not self.outcomes:
            out.append("  (nothing due)")
        for i, o in enumerate(self.outcomes, 1):
            indent = "    ↳ " if o.step_index else f"  {i:>2}. "
            text = f"{indent}{o.scheduled_for:%a %H:%M} {o.job:<20} {o.status:<9} {o.reason}"
            if o.summary:
                text += f" — {o.summary}"
            out.append(text.rstrip())
        return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        routines: RoutinesConfig,
        *,
        handlers: Mapping[str, Handler] | None = None,
        locks: LockManager | None = None,
        notifier: Notifier | None = None,
        is_halted: Callable[[], bool] | None = None,
        settings_factory: Callable[[], ArcSettings] | None = None,
        clock: Callable[[], _dt.datetime] | None = None,
        run_env: RunEnv | None = None,
    ) -> None:
        self.conn = conn
        self.routines = routines
        # D34: what a step hands to a subprocess it spawns (db, config, locks, slack).
        self.run_env = run_env or RunEnv()
        self.handlers = dict(handlers or {})
        self.locks = locks or NullLocks()
        self.heartbeats = Heartbeats(
            conn, notifier or LogNotifier(), day_rollover=routines.heartbeat.day_rollover
        )
        self.runs = RoutineRunRepo(conn)
        self.events = RoutineEventRepo(conn)
        self.state = RoutineStateRepo(conn)
        self.store = ContextStore(conn)
        self._is_halted = is_halted or self._default_halted
        # D26: without an explicit factory every run reads the effective config
        # (defaults < YAML/env < config_changes overrides) from this DB.
        self._settings_factory = settings_factory or self._effective_settings
        # E5.2b: fresh wall clock handed to steps (None = the tick's frozen ``now``).
        self._clock = clock
        self._manifest_alerted = False
        self._loop_root_ts: str | None = None  # D36: set while a loop chain's root is open
        self._loop_no_change = (
            False  # D36: this loop skipped its LLM steps (thread = [Routines] only)
        )

    def _effective_settings(self) -> ArcSettings:
        from arc.control.effective import effective_settings

        return effective_settings(self.conn)

    def _config_version(self) -> int | None:
        from arc.control.store import ConfigChangeRepo

        try:
            return ConfigChangeRepo(self.conn).version()
        except Exception:  # noqa: BLE001 - store not migrated: no version to record
            return None

    def _default_halted(self) -> bool:
        # E3.3 kill switch; fails closed (halted) if the halt store is unreadable.
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        return HaltSwitch(HaltRepo(self.conn)).is_halted()

    # -- planning ------------------------------------------------------------

    def _window_start(
        self, job: str, now: _dt.datetime, since: _dt.datetime | None
    ) -> _dt.datetime:
        floor = now - self.routines.tick.max_lookback
        if since is not None:
            return max(since, floor)
        cursor = self.state.get_time(_CURSOR.format(job=job))
        if cursor is None:
            cursor = self.state.get_time(_LAST_TICK)
        if cursor is None:
            # First tick ever: look back one tick interval, never backfill history.
            cursor = now - self.routines.tick.interval
        return max(cursor, floor)

    def plan(
        self, now: _dt.datetime, *, since: _dt.datetime | None = None, halted: bool | None = None
    ) -> list[DueJob]:
        """Decide which job slots are due in ``(cursor, now]``; no side effects."""
        halted = self._is_halted() if halted is None else halted
        due: list[DueJob] = []
        for name, (kind, spec) in self.routines.jobs().items():
            slots = slots_between(spec, self._window_start(name, now, since), now)
            if not slots:
                continue
            slot = slots[-1]
            collapsed = len(slots) - 1
            chain = tuple(spec.chain)
            if kind is JobKind.PERSONA and halted and not spec.halt_exempt:
                due.append(DueJob(name, kind, slot, "skip-halted", collapsed, chain))
            elif now > catchup_deadline(spec, slot):
                note = f"missed; catch-up window ended {catchup_deadline(spec, slot):%a %H:%M}"
                due.append(DueJob(name, kind, slot, "skip-missed", collapsed, chain, note))
            else:
                due.append(DueJob(name, kind, slot, "run", collapsed, chain))
        # Sources first (after_sources), then personas; each group in slot order.
        due.sort(key=lambda d: (d.kind is not JobKind.SOURCE, d.slot, d.job))
        return due

    # -- tick ----------------------------------------------------------------

    def tick(
        self,
        now: _dt.datetime | None = None,
        *,
        dry_run: bool = False,
        since: _dt.datetime | None = None,
    ) -> TickReport:
        now = (now or now_et()).astimezone(ET)
        halted = self._is_halted()
        report = TickReport(now=now, since=since, dry_run=dry_run, halted=halted)
        plan = self.plan(now, since=since, halted=halted)

        if dry_run:
            for d in plan:
                reason = self._reason(d)
                status = "planned" if d.action == "run" else "skipped"
                report.outcomes.append(Outcome(d.job, d.slot, status, reason))
                if d.action == "run":
                    for i, step in enumerate(d.chain, 1):
                        report.outcomes.append(
                            Outcome(step, d.slot, "planned", f"chain step {i}", step_index=i)
                        )
                    for rule in self.routines.triggers_for(f"{d.job}.completed"):
                        cond = f" if {rule.condition}" if rule.condition else ""
                        report.outcomes.append(
                            Outcome(
                                rule.run,
                                d.slot,
                                "may-run",
                                f"trigger on {d.job}.completed{cond}",
                                step_index=1,
                            )
                        )
            return report

        report.expired = self.store.expire_due(now)
        for d in plan:
            report.outcomes.extend(self._handle_due(d, now))
        report.outcomes.extend(self._drain_events(now))
        self.state.set_time(_LAST_TICK, now)
        return report

    @staticmethod
    def _reason(d: DueJob) -> str:
        if d.action == "skip-halted":
            return "halted (persona)"
        if d.action == "skip-missed":
            return d.note
        if d.collapsed:
            return f"schedule; catch-up ({d.collapsed} earlier slot(s) collapsed)"
        return "schedule"

    def _handle_due(self, d: DueJob, now: _dt.datetime) -> list[Outcome]:
        cursor_key = _CURSOR.format(job=d.job)
        reason = self._reason(d)
        if d.action != "run":
            claimed = self.runs.claim(
                job=d.job,
                scheduled_for=d.slot,
                reason="schedule",
                status=RunStatus.SKIPPED,
                summary=reason,
                now=now,
            )
            self.state.set_time(cursor_key, now)
            log.info("routines.skipped", job=d.job, slot=d.slot.isoformat(), why=reason)
            if claimed is None:
                return [Outcome(d.job, d.slot, "duplicate", "already recorded for this slot")]
            self._write_manifest(claimed, _RunTrace(), now=now, started=now, t0=time.monotonic())
            return [Outcome(d.job, d.slot, "skipped", reason, run_id=claimed.run_id)]
        outcomes = self.run_job(d.job, d.slot, reason="schedule", now=now, note=reason)
        if outcomes[0].status == "deferred":
            # Keep the slot inside the next tick's window so it retries (within
            # its catch-up deadline); earlier collapsed slots stay collapsed.
            self.state.set_time(cursor_key, d.slot - _dt.timedelta(microseconds=1))
        else:
            self.state.set_time(cursor_key, now)
        return outcomes

    # -- running -------------------------------------------------------------

    def _llm(self, kind: JobKind, spec: StepSpec) -> bool:
        return spec.llm if spec.llm is not None else kind is JobKind.PERSONA

    def run_job(
        self,
        job: str,
        scheduled_for: _dt.datetime,
        *,
        reason: str,
        now: _dt.datetime,
        chain: bool = True,
        event: RoutineEvent | None = None,
        depth: int = 0,
        note: str = "",
        parent_run_id: str | None = None,
    ) -> list[Outcome]:
        """Run *job* for *scheduled_for* (plus its chain), then fire triggers."""
        found = self.routines.job(job)
        if found is None:
            msg = f"unknown job {job!r}"
            raise KeyError(msg)
        kind, spec = found
        steps = [job, *(spec.chain if chain else [])]
        chain_run_id = f"chain-{uuid.uuid4().hex[:12]}" if len(steps) > 1 else None
        lock_names = [job, *([LLM_LOCK] if self._llm(kind, spec) or len(steps) > 1 else [])]
        try:
            with self.locks.hold(*lock_names):
                outcomes = self._run_steps(
                    steps,
                    scheduled_for,
                    reason=reason,
                    now=now,
                    chain_run_id=chain_run_id,
                    event=event,
                    note=note,
                    parent_run_id=parent_run_id,
                )
        except LockBusyError as exc:
            if self.routines.is_loop(job) and reason == "schedule":
                # D31 non-overlap: a loop slot never queues behind the previous loop
                # (or a Scout holding the LLM lock); it is recorded as skipped and
                # the next slot gets a fresh look. No catch-up, no deferral counter.
                return [self._skip_loop_slot(job, scheduled_for, exc, now)]
            log.info("routines.deferred", job=job, why=str(exc))
            return [Outcome(job, scheduled_for, "deferred", f"lock busy ({exc})")]
        return outcomes + self._fire_completed(outcomes, now, depth)

    def _skip_loop_slot(
        self, job: str, scheduled_for: _dt.datetime, exc: LockBusyError, now: _dt.datetime
    ) -> Outcome:
        held = str(exc)
        why = (
            "previous loop running"
            if f"lock {job!r}" in held
            else f"{LLM_LOCK} lock busy (another persona running)"
        )
        summary = f"skipped: {why}"
        claimed = self.runs.claim(
            job=job,
            scheduled_for=scheduled_for,
            reason="schedule",
            status=RunStatus.SKIPPED,
            summary=summary,
            now=now,
        )
        log.info("routines.loop_skipped", job=job, slot=scheduled_for.isoformat(), why=why)
        if claimed is None:
            return Outcome(job, scheduled_for, "duplicate", "already recorded for this slot")
        trace = _RunTrace(metrics={"loop_skipped": why})
        self._write_manifest(claimed, trace, now=now, started=now, t0=time.monotonic())
        self._post_skipped_root(claimed.run_id, scheduled_for, why)
        return Outcome(job, scheduled_for, "skipped", summary, run_id=claimed.run_id)

    def _post_skipped_root(self, run_id: str, slot: _dt.datetime, why: str) -> None:
        """D36: a skipped slot still gets its one-line root (``HOLD (skipped: …)``)
        when ``loop.post_hold_roots`` is on, so the channel shows every slot."""
        from arc.routines.config import LoopLayout
        from arc.slack.loop import LoopRoot

        loop = self.routines.loop
        if loop.slack_layout is not LoopLayout.ROOT_PER_LOOP or not loop.post_hold_roots:
            return
        ts = self.heartbeats.open_loop_root(LoopRoot(slot=slot, skipped=why).text())
        self.heartbeats.close_loop_root()
        log.info("routines.loop_root", run_id=run_id, ts=ts, skipped=why)

    def resume_chain(self, chain_run_id: str, *, now: _dt.datetime) -> list[Outcome]:
        """Re-run a chain from its first failed/skipped step; ok steps are not re-run."""
        rows = self.runs.chain(chain_run_id)
        if not rows:
            msg = f"unknown chain {chain_run_id!r}"
            raise KeyError(msg)
        root = rows[0]
        found = self.routines.job(root.job)
        if found is None:
            msg = f"chain root {root.job!r} is no longer configured"
            raise KeyError(msg)
        kind, spec = found
        steps = [root.job, *spec.chain]
        try:
            with self.locks.hold(root.job, LLM_LOCK):
                outcomes = self._run_steps(
                    steps,
                    root.scheduled_for,
                    reason=root.reason,
                    now=now,
                    chain_run_id=chain_run_id,
                    existing={r.job: r for r in rows},
                )
        except LockBusyError as exc:
            return [Outcome(root.job, root.scheduled_for, "deferred", f"lock busy ({exc})")]
        return outcomes + self._fire_completed(outcomes, now, 0)

    def _run_steps(
        self,
        steps: list[str],
        scheduled_for: _dt.datetime,
        *,
        reason: str,
        now: _dt.datetime,
        chain_run_id: str | None,
        event: RoutineEvent | None = None,
        existing: Mapping[str, RoutineRun] | None = None,
        note: str = "",
        parent_run_id: str | None = None,
    ) -> list[Outcome]:
        outcomes: list[Outcome] = []
        prev_run_id = parent_run_id
        existing = existing or {}
        # D31: the loop chain has a deadline. A step that is running when it passes
        # may finish; no later step starts. `no_change` (the Director found the same
        # inputs as last time) skips the LLM steps and runs the deterministic tail.
        is_loop = bool(chain_run_id) and reason == "schedule" and self.routines.is_loop(steps[0])
        deadline = time.monotonic() + self.routines.loop.max_runtime.total_seconds()
        durations: dict[str, int] = {}
        no_change = False
        timed_out = False
        root_ts: str | None = None
        if is_loop and chain_run_id:
            root_ts = self._open_loop_root(chain_run_id, scheduled_for)
        self._loop_root_ts = root_ts
        try:
            for index, step in enumerate(steps):
                prior = existing.get(step)
                if prior is not None and prior.status is RunStatus.OK:
                    outcomes.append(
                        Outcome(
                            step,
                            scheduled_for,
                            "ok",
                            "already done (resume)",
                            run_id=prior.run_id,
                            chain_run_id=chain_run_id,
                            step_index=index,
                            summary=prior.summary or "",
                        )
                    )
                    continue
                _, step_spec = self.routines.step(step)
                if is_loop and index and time.monotonic() > deadline:
                    timed_out = True
                    outcomes.append(
                        self._record_skipped_step(
                            step,
                            scheduled_for,
                            reason=f"chain:{steps[0]}",
                            chain_run_id=chain_run_id,
                            step_index=index,
                            summary=f"timeout: loop exceeded {self._max_runtime_label()}",
                            now=now,
                        )
                    )
                    continue
                if is_loop and no_change and index and step_spec.on_no_change == "skip":
                    outcomes.append(
                        self._record_skipped_step(
                            step,
                            scheduled_for,
                            reason=f"chain:{steps[0]}",
                            chain_run_id=chain_run_id,
                            step_index=index,
                            summary="no_change: inputs unchanged since the last full loop",
                            now=now,
                        )
                    )
                    continue
                if prior is not None:
                    run = self.runs.restart(prior.run_id, now=now)
                else:
                    claimed = self.runs.claim(
                        job=step,
                        scheduled_for=scheduled_for,
                        reason=reason if index == 0 else f"chain:{steps[0]}",
                        chain_run_id=chain_run_id,
                        step_index=index,
                        now=now,
                        # E6.2d: an event-triggered run is unique per (job, event), not per slot.
                        event_id=event.id if event is not None else None,
                    )
                    if claimed is None:
                        outcomes.append(
                            Outcome(
                                step,
                                scheduled_for,
                                "duplicate",
                                "already ran for this slot",
                                chain_run_id=chain_run_id,
                                step_index=index,
                            )
                        )
                        break
                    run = claimed
                t_step = time.monotonic()
                outcome = self._execute(
                    run,
                    now=now,
                    event=event,
                    note=note if index == 0 else "",
                    parent_run_id=prev_run_id,
                )
                durations[step] = int((time.monotonic() - t_step) * 1000)
                prev_run_id = run.run_id
                outcomes.append(outcome)
                if index == 0 and outcome.metrics.get("no_change"):
                    no_change = True
                    self._loop_no_change = True
                stop = outcome.status != "ok" or bool(outcome.metrics.get("stop_chain"))
                if stop:
                    if chain_run_id and index + 1 < len(steps):
                        log.warning(
                            "routines.chain_stopped",
                            chain_run_id=chain_run_id,
                            at=step,
                            status=outcome.status,
                            reason="stop_chain" if outcome.status == "ok" else outcome.status,
                            remaining=steps[index + 1 :],
                        )
                    break
            if is_loop and chain_run_id:
                self._finish_loop(
                    chain_run_id,
                    outcomes,
                    durations,
                    now=now,
                    timed_out=timed_out,
                    no_change=no_change,
                    scheduled_for=scheduled_for,
                    root_ts=root_ts,
                )
        finally:
            self._loop_root_ts = None
            self._loop_no_change = False
            if root_ts:
                self.heartbeats.close_loop_root()
        return outcomes

    def _open_loop_root(self, chain_run_id: str, slot: _dt.datetime) -> str | None:
        """D36: post the loop's root line first, so every persona card threads under it.

        The line starts as ``HOLD`` and is re-rendered from the DB when the chain
        ends (and again on approval / fill / expiry). ``slack_layout: day_thread``
        keeps everything in the day thread (the rollback).
        """
        from arc.routines.config import LoopLayout
        from arc.routines.loop import LoopState, loop_root_from_db

        if self.routines.loop.slack_layout is not LoopLayout.ROOT_PER_LOOP:
            return None
        root = loop_root_from_db(self.conn, chain_run_id, slot)
        ts = self.heartbeats.open_loop_root(root.text())
        if ts:
            state = LoopState(self.conn)
            state.set_thread_ts(chain_run_id, ts)
            state.set_root(chain_run_id, root.model_dump(mode="json"))
        return ts

    def _post_scout_context(self, now: _dt.datetime, ctx: JobContext) -> str | None:
        """D36 thread item 1: the ``[Scout]`` card of the candidates the Director read.

        Rendered from the Director run's recorded context snapshot (the same
        ``candidate`` entries it was given), so the card names the Scout run
        and its time without re-posting the Scout's own 30-min card.
        """
        from arc.slack.digests import scout_context_card

        entries = ctx.snapshot.of_kind("candidate")
        card = scout_context_card(entries, chain_run_id=ctx.chain_run_id)
        return self.heartbeats.summary(now, "scout", card.text, blocks=card.blocks)

    def _max_runtime_label(self) -> str:
        secs = int(self.routines.loop.max_runtime.total_seconds())
        return f"{secs // 60}m" if secs % 60 == 0 else f"{secs}s"

    def _record_skipped_step(
        self,
        step: str,
        scheduled_for: _dt.datetime,
        *,
        reason: str,
        chain_run_id: str | None,
        step_index: int,
        summary: str,
        now: _dt.datetime,
    ) -> Outcome:
        """A chain step the loop decided not to run (timeout / no_change), on the record."""
        claimed = self.runs.claim(
            job=step,
            scheduled_for=scheduled_for,
            reason=reason,
            chain_run_id=chain_run_id,
            step_index=step_index,
            status=RunStatus.SKIPPED,
            summary=summary,
            now=now,
        )
        log.info("routines.step_skipped", job=step, why=summary, chain_run_id=chain_run_id)
        if claimed is None:
            return Outcome(
                step,
                scheduled_for,
                "duplicate",
                "already ran for this slot",
                chain_run_id=chain_run_id,
                step_index=step_index,
            )
        self._write_manifest(
            claimed,
            _RunTrace(metrics={"loop_skipped": summary.split(":", 1)[0]}),
            now=now,
            started=now,
            t0=time.monotonic(),
        )
        return Outcome(
            step,
            scheduled_for,
            "skipped",
            summary,
            run_id=claimed.run_id,
            chain_run_id=chain_run_id,
            step_index=step_index,
        )

    def _finish_loop(
        self,
        chain_run_id: str,
        outcomes: list[Outcome],
        durations: dict[str, int],
        *,
        now: _dt.datetime,
        timed_out: bool,
        no_change: bool,
        scheduled_for: _dt.datetime,
        root_ts: str | None,
    ) -> None:
        """Record the loop's step durations / flags (D27), alert a timeout once a day,
        and (D36) re-render the root line plus post the ``[Routines]`` metadata reply."""
        from arc.routines.loop import LoopState, loop_root_from_db

        state = LoopState(self.conn)
        state.set_chain_summary(
            chain_run_id,
            {
                "durations_ms": durations,
                "timeout": timed_out,
                "no_change": no_change,
                "steps": [(o.job, o.status) for o in outcomes],
            },
        )
        if timed_out:
            root = outcomes[0]
            log.warning(
                "routines.loop_timeout",
                chain_run_id=chain_run_id,
                durations_ms=durations,
                max_runtime=self._max_runtime_label(),
            )
            day = self.heartbeats.day(now)
            if state.first_timeout_today(day):
                self.heartbeats.alert(
                    now,
                    root.job,
                    f"loop exceeded {self._max_runtime_label()} (steps after the deadline "
                    f"skipped; once-a-day notice)",
                    run_id=root.run_id,
                )
        if not root_ts:
            return
        # The metadata reply, then the root re-rendered from what the chain wrote.
        steps = " ".join(
            f"{o.job}={durations[o.job]}ms" if o.job in durations else f"{o.job}={o.status}"
            for o in outcomes
        )
        digest = str(outcomes[0].metrics.get("loop_digest") or "")[:12]
        flags = " ".join(f for f, on in (("no_change", no_change), ("timeout", timed_out)) if on)
        meta = f"{chain_run_id} {steps} digest={digest or 'n/a'}"
        if flags:
            meta += f" {flags}"
        self.heartbeats.loop_metadata(now, meta)
        line = loop_root_from_db(
            self.conn, chain_run_id, scheduled_for, no_change=no_change, timeout=timed_out
        )
        state.set_root(chain_run_id, line.model_dump(mode="json"))
        try:
            self.heartbeats.update_loop_root(root_ts, line.text())
        except Exception as exc:  # noqa: BLE001 - the loop's work is committed; the edit is best-effort
            log.warning("routines.loop_root_update_failed", chain_run_id=chain_run_id, err=str(exc))
        log.info("routines.loop_root", chain_run_id=chain_run_id, ts=root_ts, text=line.text())

    def _execute(
        self,
        run: RoutineRun,
        *,
        now: _dt.datetime,
        event: RoutineEvent | None,
        note: str,
        parent_run_id: str | None = None,
        job_lock: bool = True,
    ) -> Outcome:
        # E8.2: every log line of this run (handler, Slack, LLM calls) carries its ids.
        with bind_ids(
            run_id=run.run_id,
            chain_run_id=run.chain_run_id,
            job=run.job,
            step_index=run.step_index if run.chain_run_id else None,
        ):
            trace = _RunTrace(event_id=event.id if event else None, parent_run_id=parent_run_id)
            started, t0 = now_et(), time.monotonic()
            # D26: the config_changes version this run executes under (E7.4 attribution).
            self.runs.set_config_version(run.run_id, self._config_version())
            try:
                return self._execute_bound(
                    run, now=now, event=event, note=note, trace=trace, job_lock=job_lock
                )
            finally:
                # D27: one manifest per run attempt, on every path (ok / failed / skipped).
                self._write_manifest(run, trace, now=now, started=started, t0=t0)

    def _execute_bound(
        self,
        run: RoutineRun,
        *,
        now: _dt.datetime,
        event: RoutineEvent | None,
        note: str,
        trace: _RunTrace,
        job_lock: bool = True,
    ) -> Outcome:
        kind, spec = self.routines.step(run.job)
        ctx: JobContext | None = None
        try:
            handler = resolve_handler(run.job, spec, self.handlers)
            snapshot_kinds = spec.reads if spec.reads is not None else []
            snapshot = None
            if kind is JobKind.PERSONA or spec.reads is not None:
                snapshot = self.store.snapshot(now, kinds=snapshot_kinds, run_id=run.run_id)
                self.runs.set_inputs(run.run_id, [snapshot.id])
            ctx = JobContext(
                job=run.job,
                kind=kind,
                spec=spec,
                run_id=run.run_id,
                chain_run_id=run.chain_run_id,
                scheduled_for=run.scheduled_for,
                now=now,
                conn=self.conn,
                snapshot=snapshot or _empty_snapshot(now),
                routines=self.routines,
                event=event,
                settings_factory=self._settings_factory,
                clock_fn=self._clock,
                run_env=self.run_env,
                reason=run.reason,
            )
            trace.ctx = ctx
            hold = run.step_index and job_lock  # a chain step; run_event holds its own lock
            with self.locks.hold(run.job) if hold else contextlib.nullcontext():
                result = handler(ctx)
            if not isinstance(result, JobResult):
                msg = f"handler for {run.job!r} returned {type(result).__name__}, not JobResult"
                raise TypeError(msg)
        except JobSkippedError as exc:
            trace.exc = exc
            outputs = ctx.outputs if ctx else []
            self.runs.finish(
                run.run_id, status=RunStatus.SKIPPED, outputs=outputs, summary=str(exc), now=now
            )
            log.info("routines.step_skipped", job=run.job, why=str(exc))
            return self._outcome(run, "skipped", str(exc))
        except Exception as exc:  # noqa: BLE001 - a job failure is recorded, never raised
            trace.exc = exc
            error = f"{type(exc).__name__}: {exc}"
            outputs = ctx.outputs if ctx else []
            self.runs.finish(
                run.run_id, status=RunStatus.FAILED, outputs=outputs, error=error, now=now
            )
            log.error("routines.failed", job=run.job, run_id=run.run_id, error=error)
            trace.notifications.append(
                self.heartbeats.alert(now, run.job, error, run_id=run.run_id)
            )
            return self._outcome(run, "failed", error)

        if result.stop_chain:
            result.metrics = {**result.metrics, "stop_chain": True}
        trace.metrics = result.metrics
        summary = result.summary
        if note and note != "schedule":
            summary = f"{summary} ({note})" if summary else note
        self.runs.finish(
            run.run_id, status=RunStatus.OK, outputs=ctx.outputs, summary=summary, now=now
        )
        notify = spec.notify or (Notify.QUIET if kind is JobKind.SOURCE else Notify.CARD)
        posts = trace.notifications
        if result.notice:
            posts.append(self.heartbeats.notice(now, run.job, result.notice))
        in_loop_thread = bool(self._loop_root_ts)
        if in_loop_thread and (self._loop_no_change or result.metrics.get("no_change")):
            # D36: a no_change loop gets only the [Routines] metadata reply in its thread;
            # the step's own summary is folded into that reply (``execute=…ms``).
            log.info("routines.ok", job=run.job, run_id=run.run_id, outputs=len(ctx.outputs))
            return self._outcome(run, "ok", summary, metrics=result.metrics)
        if in_loop_thread and run.step_index == 0:
            # D36 thread item 1: the Scout context the Director read, before its own card.
            posts.append(self._post_scout_context(now, ctx))
        if notify is Notify.QUIET:
            new_docs = result.metrics.get("new_docs")
            self.heartbeats.queue_source(
                run.job, summary, new_docs=new_docs if isinstance(new_docs, int) else None
            )
        elif notify is Notify.CARD and result.card is not None:
            posts.append(self.heartbeats.summary(now, run.job, summary, blocks=result.card.blocks))
        else:
            posts.append(self.heartbeats.summary(now, run.job, summary))
        log.info("routines.ok", job=run.job, run_id=run.run_id, outputs=len(ctx.outputs))
        return self._outcome(run, "ok", summary, metrics=result.metrics)

    def _write_manifest(
        self,
        run: RoutineRun,
        trace: _RunTrace,
        *,
        now: _dt.datetime,
        started: _dt.datetime,
        t0: float,
    ) -> None:
        """Append the D27 run manifest. A manifest failure never masks the job's result."""
        from arc.monitoring.correlation import from_env
        from arc.routines.manifest import ManifestRepo, build_manifest

        try:
            final = self.runs.get(run.run_id) or run
            kind, spec = self.routines.step(run.job)
            ctx = trace.ctx
            settings = None
            try:
                if ctx is not None and ctx._settings is not None:
                    settings = ctx._settings
                elif self._settings_factory is not None:
                    settings = self._settings_factory()
                else:
                    from arc.config import get_settings

                    settings = get_settings()
            except Exception:  # noqa: BLE001 - settings are metadata here, never required
                settings = None
            try:
                halted: bool | None = self._is_halted()
            except Exception:  # noqa: BLE001
                halted = None
            ids = structlog.contextvars.get_contextvars()
            correlation = {**from_env(), **{
                k: str(v) for k, v in ids.items() if k in _CORRELATION_KEYS and v
            }}  # fmt: skip
            manifest = build_manifest(
                self.conn,
                final,
                kind=kind,
                spec=spec,
                routines=self.routines,
                tick_now=now,
                started_at=started,
                finished_at=now_et(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                exc=trace.exc,
                metrics=trace.metrics,
                external_inputs=ctx.external_inputs if ctx is not None else (),
                event_id=trace.event_id,
                parent_run_id=trace.parent_run_id,
                notifications=[n for n in trace.notifications if n],
                settings=settings,
                halted=halted,
                correlation=correlation,
            )
            ManifestRepo(self.conn).insert(manifest)
        except Exception as exc:  # noqa: BLE001 - audit metadata must not fail the job
            log.error("manifest.write_failed", job=run.job, run_id=run.run_id, error=repr(exc))
            if not self._manifest_alerted:
                self._manifest_alerted = True
                with contextlib.suppress(Exception):
                    self.heartbeats.alert(
                        now, run.job, f"run manifest not written: {exc!r}", run_id=run.run_id
                    )

    @staticmethod
    def _outcome(
        run: RoutineRun, status: str, summary: str, *, metrics: dict[str, Any] | None = None
    ) -> Outcome:
        return Outcome(
            run.job,
            run.scheduled_for,
            status,
            run.reason,
            run_id=run.run_id,
            chain_run_id=run.chain_run_id,
            step_index=run.step_index,
            summary=summary,
            metrics=metrics or {},
        )

    # -- triggers ------------------------------------------------------------

    def _fire_completed(
        self, outcomes: list[Outcome], now: _dt.datetime, depth: int
    ) -> list[Outcome]:
        fired: list[Outcome] = []
        for o in outcomes:
            if o.status != "ok" or o.reason == "already done (resume)":
                continue
            env = {
                **o.metrics,
                "job": o.job,
                "session": session_phase(now),
                "hour": now.hour,
                "weekday": now.weekday(),
            }
            fired.extend(
                self._fire(
                    f"{o.job}.completed", env, o.scheduled_for, now, depth, parent_run_id=o.run_id
                )
            )
        return fired

    def _fire(
        self,
        event_name: str,
        env: dict[str, Any],
        scheduled_for: _dt.datetime,
        now: _dt.datetime,
        depth: int,
        event: RoutineEvent | None = None,
        parent_run_id: str | None = None,
    ) -> list[Outcome]:
        out: list[Outcome] = []
        if depth >= self.routines.tick.max_trigger_depth:
            log.warning("routines.trigger_depth", trigger_event=event_name, depth=depth)
            return out
        for rule in self.routines.triggers_for(event_name):
            if not evaluate_condition(rule.condition, env):
                log.info("routines.trigger_false", trigger_event=event_name, run=rule.run)
                continue
            kind, spec = self.routines.jobs().get(rule.run, (None, None))
            if spec is None:
                continue
            if self._is_halted() and not spec.halt_exempt:
                out.append(Outcome(rule.run, scheduled_for, "skipped", "halted (persona)"))
                continue
            out.extend(
                self.run_job(
                    rule.run,
                    scheduled_for,
                    reason=f"event:{event_name}",
                    now=now,
                    event=event,
                    depth=depth + 1,
                    parent_run_id=parent_run_id,
                )
            )
        return out

    def _drain_events(self, now: _dt.datetime) -> list[Outcome]:
        """Fire every pending external event once (E6.2d dispatch-once semantics).

        - An event the D34 ``execute`` step dispatched is never pending here: its
          Investor subprocess owns it.
        - An ``approval`` that arrives (or is still waiting) while halted is
          ``deferred`` until its proposal's ``expires_at``; after ``!resume`` inside
          that TTL the Investor runs. Past it the event is consumed with a journal
          row (``order:refused`` "approval lapsed under halt") and the card updated.
        - Each event-triggered run is keyed by the event id, so two events with the
          same ``created_at`` both run (never ``duplicate``).
        """
        out: list[Outcome] = []
        for ev in self.events.pending(until=now):
            held = self._halt_hold(ev, now)
            if held is not None:
                out.extend(held)
                continue
            env = {**ev.payload, "session": session_phase(now), "event": ev.name}
            results = self._fire(ev.name, env, ev.created_at, now, 0, event=ev)
            if any(o.status == "deferred" for o in results):
                out.extend(results)
                continue  # retry next tick
            self.events.consume(ev.id, [o.run_id for o in results if o.run_id], now=now)
            self.state.delete(_HALT_DEFERRED.format(event=ev.id))
            out.extend(results)
        return out

    def _halt_targets(self, ev: RoutineEvent) -> list[str]:
        """Jobs *ev* would start that a halt stops (not ``halt_exempt``)."""
        jobs = self.routines.jobs()
        return [
            r.run
            for r in self.routines.triggers_for(ev.name)
            if r.run in jobs and not jobs[r.run][1].halt_exempt
        ]

    def _halt_hold(self, ev: RoutineEvent, now: _dt.datetime) -> list[Outcome] | None:
        """E6.2d: defer or lapse an ``approval`` event held by a halt; ``None`` = fire it.

        Only approvals carry a deadline (the proposal TTL); other events keep the
        old behaviour (the halted job is recorded ``skipped`` and the event consumed).
        """
        from arc.routines.investor import approval_deadline

        if ev.name != "approval":
            return None
        targets = self._halt_targets(ev)
        if not targets:
            return None
        phash = str(ev.payload.get("proposal_hash") or "")
        deadline = approval_deadline(self.conn, phash) if phash else None
        if deadline is None:
            return None  # unknown proposal: let the Investor refuse it on the record
        key = _HALT_DEFERRED.format(event=ev.id)
        halted = self._is_halted()
        was_held = self.state.get(key) is not None
        if not halted and not (was_held and now >= deadline):
            return None  # not halted (or resumed inside the TTL): run it now
        if now < deadline:
            self.state.set(key, now.isoformat(), now=now)
            until = deadline.astimezone(ET)
            log.info("routines.event_deferred", event_id=ev.id, why="halted", until=until)
            return [
                Outcome(
                    job,
                    ev.created_at,
                    "deferred",
                    f"halted; approval held until !resume or {until:%H:%M} ET (its TTL)",
                )
                for job in targets
            ]
        return self._lapse(ev, phash, targets, now)

    def _lapse(
        self, ev: RoutineEvent, phash: str, targets: list[str], now: _dt.datetime
    ) -> list[Outcome]:
        """The approval's TTL passed under a halt: journal, card, consume (no order)."""
        from arc.routines.investor import LAPSED_UNDER_HALT, lapse_approval

        out: list[Outcome] = []
        run_ids: list[str] = []
        for job in targets:
            run = self.runs.claim(
                job=job,
                scheduled_for=ev.created_at,
                reason=f"event:{ev.name}",
                status=RunStatus.SKIPPED,
                summary=LAPSED_UNDER_HALT,
                now=now,
                event_id=ev.id,
            )
            if run is None:
                continue
            run_ids.append(run.run_id)
            self._write_manifest(
                run, _RunTrace(event_id=ev.id), now=now, started=now, t0=time.monotonic()
            )
            out.append(Outcome(job, ev.created_at, "skipped", LAPSED_UNDER_HALT, run_id=run.run_id))
        try:
            settings: Any = self._settings_factory()
        except Exception:  # noqa: BLE001 - the journal row matters, not the card's config
            from arc.config import get_settings

            settings = get_settings()
        lapse_approval(
            self.conn,
            phash,
            now=now,
            run_id=run_ids[0] if run_ids else None,
            settings=settings,
            slack=self.run_env.slack,
        )
        self.events.consume(ev.id, run_ids, now=now)
        self.state.delete(_HALT_DEFERRED.format(event=ev.id))
        return out

    # -- one event, out of band (D34) ------------------------------------------

    def run_event(
        self,
        job: str,
        event: RoutineEvent,
        *,
        now: _dt.datetime,
        chain_run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> list[Outcome]:
        """``arc routines run <job> --event <id>``: run *job* for one queued event.

        Used by the in-chain ``execute`` step to hand an auto-approved proposal to
        an Investor subprocess. The run joins the chain (``chain_run_id``, next step
        index) so ``arc context trace <chain>`` shows it, holds a per-event lock
        (``<job>:<event id>``) rather than the job's lock, and never the LLM lock, so
        ladders run in parallel with the next loop.

        E6.2d: the event is normally already ``dispatched`` (claimed by ``execute``
        before the spawn); that is accepted. The run is keyed by the event id, so a
        second invocation for the same event is a ``duplicate`` and never a second
        ladder. A consumed event is refused. While halted (and *job* is not
        ``halt_exempt``) nothing runs: the event is released back to the tick's
        drain, which defers it until ``!resume`` or its TTL. Otherwise the event is
        consumed by this run whatever the outcome.
        """
        found = self.routines.job(job)
        if found is None:
            msg = f"unknown job {job!r}"
            raise KeyError(msg)
        _, spec = found
        current = self.events.get(event.id) or event
        if current.consumed_at is not None:
            return [Outcome(job, now, "duplicate", "event already consumed")]
        if self._is_halted() and not spec.halt_exempt:
            self.events.release(event.id)
            log.info("routines.event_released", job=job, event_id=event.id, why="halted")
            return [Outcome(job, now, "deferred", "halted; event handed back to the tick")]
        try:
            with self.locks.hold(f"{job}:{event.id}"):
                step_index = 0
                if chain_run_id:
                    step_index = (
                        max((r.step_index for r in self.runs.chain(chain_run_id)), default=-1) + 1
                    )
                run = self.runs.claim(
                    job=job,
                    scheduled_for=now,
                    reason=f"event:{event.name}",
                    chain_run_id=chain_run_id,
                    step_index=step_index,
                    now=now,
                    event_id=event.id,
                )
                if run is None:
                    return [Outcome(job, now, "duplicate", "already ran for this event")]
                outcome = self._execute(
                    run, now=now, event=event, note="", parent_run_id=parent_run_id, job_lock=False
                )
        except LockBusyError as exc:
            # Another process holds this very event's lock: it owns the run.
            return [Outcome(job, now, "deferred", f"lock busy ({exc})")]
        self.events.consume(event.id, [run.run_id], now=now)
        return [outcome, *self._fire_completed([outcome], now, 0)]

    # -- manual ---------------------------------------------------------------

    def run_manual(
        self, job: str, *, now: _dt.datetime, chain: bool = False, fresh: bool = False
    ) -> list[Outcome]:
        """``arc routines run <job> [--chain]``: resume today's failed chain, else run now."""
        found = self.routines.job(job)
        if found is None:
            msg = f"unknown job {job!r}"
            raise KeyError(msg)
        if chain and not fresh:
            failed = self.runs.latest_failed_chain(job, now.date())
            if failed:
                return self.resume_chain(failed, now=now)
        outcomes: list[Outcome] = []
        if found[0] is JobKind.PERSONA and found[1].after_sources:
            for source in self.routines.sources:
                if self.routines.sources[source].enabled:
                    outcomes.extend(self.run_job(source, now, reason="manual", now=now))
        return outcomes + self.run_job(job, now, reason="manual", now=now, chain=chain)


_CORRELATION_KEYS = frozenset({"tick_id", "cron_job", "kanban_task", "hermes_session"})


@dataclass
class _RunTrace:
    """What the manifest needs from one execution besides the (final) run row."""

    event_id: str | None = None
    parent_run_id: str | None = None
    ctx: JobContext | None = None
    exc: BaseException | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    notifications: list[str | None] = field(default_factory=list)


def _empty_snapshot(now: _dt.datetime) -> ContextSnapshot:
    """Sources read no context; they get an empty, unrecorded snapshot."""
    return ContextSnapshot(id="snap-none", as_of=now)


def next_due(routines: RoutinesConfig, now: _dt.datetime) -> list[tuple[str, JobSpec, Any]]:
    """``(job, spec, next slot | None)`` for every job, for ``arc routines list``."""
    from arc.routines.schedule import next_slot

    return [(name, spec, next_slot(spec, now)) for name, (_, spec) in routines.jobs().items()]
