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
tick never runs a job twice. File locks (per job + one global LLM lock) stop
overlapping ticks from running the same job or two LLM jobs at once; a job whose
lock is busy is deferred to the next tick without being recorded.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.store import ContextSnapshot, ContextStore
from arc.routines.conditions import evaluate_condition
from arc.routines.config import JobKind, Notify
from arc.routines.handlers import (
    Handler,
    JobContext,
    JobResult,
    JobSkippedError,
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
    ) -> None:
        self.conn = conn
        self.routines = routines
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
        self._settings_factory = settings_factory

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
                )
        except LockBusyError as exc:
            log.info("routines.deferred", job=job, why=str(exc))
            return [Outcome(job, scheduled_for, "deferred", f"lock busy ({exc})")]
        return outcomes + self._fire_completed(outcomes, now, depth)

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
    ) -> list[Outcome]:
        outcomes: list[Outcome] = []
        existing = existing or {}
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
            outcome = self._execute(run, now=now, event=event, note=note if index == 0 else "")
            outcomes.append(outcome)
            if outcome.status != "ok":
                if chain_run_id and index + 1 < len(steps):
                    log.warning(
                        "routines.chain_stopped",
                        chain_run_id=chain_run_id,
                        at=step,
                        status=outcome.status,
                        remaining=steps[index + 1 :],
                    )
                break
        return outcomes

    def _execute(
        self,
        run: RoutineRun,
        *,
        now: _dt.datetime,
        event: RoutineEvent | None,
        note: str,
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
            )
            with self.locks.hold(run.job) if run.step_index else contextlib.nullcontext():
                result = handler(ctx)
            if not isinstance(result, JobResult):
                msg = f"handler for {run.job!r} returned {type(result).__name__}, not JobResult"
                raise TypeError(msg)
        except JobSkippedError as exc:
            outputs = ctx.outputs if ctx else []
            self.runs.finish(
                run.run_id, status=RunStatus.SKIPPED, outputs=outputs, summary=str(exc), now=now
            )
            log.info("routines.step_skipped", job=run.job, why=str(exc))
            return self._outcome(run, "skipped", str(exc))
        except Exception as exc:  # noqa: BLE001 - a job failure is recorded, never raised
            error = f"{type(exc).__name__}: {exc}"
            outputs = ctx.outputs if ctx else []
            self.runs.finish(
                run.run_id, status=RunStatus.FAILED, outputs=outputs, error=error, now=now
            )
            log.error("routines.failed", job=run.job, run_id=run.run_id, error=error)
            self.heartbeats.alert(now, run.job, error)
            return self._outcome(run, "failed", error)

        summary = result.summary
        if note and note != "schedule":
            summary = f"{summary} ({note})" if summary else note
        self.runs.finish(
            run.run_id, status=RunStatus.OK, outputs=ctx.outputs, summary=summary, now=now
        )
        notify = spec.notify or (Notify.QUIET if kind is JobKind.SOURCE else Notify.CARD)
        if result.notice:
            self.heartbeats.notice(now, run.job, result.notice)
        if notify is Notify.QUIET:
            new_docs = result.metrics.get("new_docs")
            self.heartbeats.queue_source(
                run.job, summary, new_docs=new_docs if isinstance(new_docs, int) else None
            )
        elif notify is Notify.CARD and result.card is not None:
            self.heartbeats.summary(now, run.job, summary, blocks=result.card.blocks)
        else:
            self.heartbeats.summary(now, run.job, summary)
        log.info("routines.ok", job=run.job, run_id=run.run_id, outputs=len(ctx.outputs))
        return self._outcome(run, "ok", summary, metrics=result.metrics)

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
            fired.extend(self._fire(f"{o.job}.completed", env, o.scheduled_for, now, depth))
        return fired

    def _fire(
        self,
        event_name: str,
        env: dict[str, Any],
        scheduled_for: _dt.datetime,
        now: _dt.datetime,
        depth: int,
        event: RoutineEvent | None = None,
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
                )
            )
        return out

    def _drain_events(self, now: _dt.datetime) -> list[Outcome]:
        out: list[Outcome] = []
        for ev in self.events.pending(until=now):
            env = {**ev.payload, "session": session_phase(now), "event": ev.name}
            results = self._fire(ev.name, env, ev.created_at, now, 0, event=ev)
            if any(o.status == "deferred" for o in results):
                out.extend(results)
                continue  # retry next tick
            self.events.consume(ev.id, [o.run_id for o in results if o.run_id], now=now)
            out.extend(results)
        return out

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


def _empty_snapshot(now: _dt.datetime) -> ContextSnapshot:
    """Sources read no context; they get an empty, unrecorded snapshot."""
    return ContextSnapshot(id="snap-none", as_of=now)


def next_due(routines: RoutinesConfig, now: _dt.datetime) -> list[tuple[str, JobSpec, Any]]:
    """``(job, spec, next slot | None)`` for every job, for ``arc routines list``."""
    from arc.routines.schedule import next_slot

    return [(name, spec, next_slot(spec, now)) for name, (_, spec) in routines.jobs().items()]
