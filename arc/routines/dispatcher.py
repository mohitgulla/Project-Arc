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
   (Broker reconcile) is recorded as ``skipped``. Sources keep fetching.
5. Runs chains step by step under one ``chain_run_id``; each step records the
   snapshot it read. A failed step stops the chain and alerts; re-running the
   chain resumes from the failed step.
6. Fires trigger rules on ``<job>.completed`` (in-process, with the job's
   metrics as the condition environment) and on queued external events
   (``approval``, ``halt``, ... from ``arc routines emit``).

A ``(job, scheduled_for)`` unique key in ``routine_runs`` means a duplicate
tick never runs a job twice; an event-triggered run is unique per
``(job, event_id)`` instead (E6.2d), so events sharing a timestamp each run. File
locks stop overlapping ticks from running the same job twice; a job whose lock is
busy is deferred to the next tick without being recorded. The global LLM lock is
only taken by jobs whose persona routes to a local model (D39; none today).

D39 / E5.10 background lane: a job with ``lane: background`` is planned and claimed
by the tick as above, then run by a detached ``arc routines run-claimed <run_id>``
child (same ``_execute`` path), so the tick never waits on it.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.store import ContextSnapshot, ContextStore
from arc.context.ttl import to_db
from arc.monitoring.correlation import bind as bind_ids
from arc.routines.conditions import evaluate_condition
from arc.routines.config import JobKind, Lane, Notify, current_job_name
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
from arc.routines.schedule import catch_up_slots, catchup_deadline, slots_between
from arc.utils.calendar import ET, now_et, session_phase

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.config import ArcSettings
    from arc.llm_routing import LLMRouting
    from arc.routines.config import JobSpec, RoutinesConfig, StepSpec

    # D39: (argv, env) -> pid of a detached child (arc.routines.spawn.spawn_detached).
    Spawner = Callable[[Sequence[str], Mapping[str, str] | None], int]

log = structlog.get_logger(__name__)

_CURSOR = "cursor:{job}"
_LAST_TICK = "dispatcher:last_tick"
# E6.2d: an approval event deferred by a halt (its lapse is then "under halt").
_HALT_DEFERRED = "halt_deferred:{event}"
# D39: the background child that owns a claimed run (one owner per run).
_BG_OWNER = "bg_owner:{run}"
# Chain steps whose name is not a persona, mapped to the persona whose model they use.
# D56 (E13.2): broker.execute / quant.exits are deterministic (no persona model).
_STEP_PERSONA = {
    "quant": "quant",
    "risk": "risk",
    "propose": "research",
    # E13.9 (D56): the open-path step names (quant.propose runs no LLM)
    "quant.open": "quant",
    "quant.revise": "quant",
    "risk.open": "risk",
    "quant.propose": "quant",
    # E13.17 (D56): exit cases (personas.exit_path shadow | research)
    "quant.exit": "quant",
    # E13.18 (D56): Risk's exit review (personas.exit_path research)
    "risk.exit": "risk",
}

#: E13.18 (D56): loop steps the ``loop.max_runtime`` deadline never skips. The
#: mandatory-exit floor (stop / DTE exit / expiry) is risk management: a slow run
#: cuts new risk, not exits.
DEADLINE_EXEMPT_STEPS: frozenset[str] = frozenset({"exits.mandatory"})


# ---------------------------------------------------------------------------
# Plan / report types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DueJob:
    """A job slot the tick decided about."""

    job: str
    kind: JobKind
    slot: _dt.datetime
    action: str  # run | skip-halted | skip-missed | skip-written (E13.5)
    collapsed: int = 0  # earlier missed slots folded into this one
    chain: tuple[str, ...] = ()
    note: str = ""


@dataclass
class Outcome:
    """What happened to one job/step (a routine_runs row, or a non-recorded decision)."""

    job: str
    scheduled_for: _dt.datetime
    status: str  # ok | failed | skipped | duplicate | deferred | planned | spawned (D39)
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
    reclaimed: int = 0  # E6.2e: stranded dispatched events released to the drain
    outcomes: list[Outcome] = field(default_factory=list)
    # E5.10: wall time the tick spent on each due job (inline work; a spawn is ~0 ms).
    durations_ms: dict[str, int] = field(default_factory=dict)

    def slowest(self, n: int = 3) -> list[dict[str, Any]]:
        """The *n* due jobs the tick spent longest on, slowest first."""
        ranked = sorted(self.durations_ms.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"job": job, "ms": ms} for job, ms in ranked[:n]]

    def lines(self) -> list[str]:
        head = f"tick @ {self.now:%Y-%m-%d %H:%M %Z}" + (" (dry-run)" if self.dry_run else "")
        if self.since is not None:
            head += f" · window ({self.since:%Y-%m-%d %H:%M} → {self.now:%H:%M}]"
        if self.halted:
            head += " · HALTED"
        if self.reclaimed:
            head += f" · reclaimed {self.reclaimed} stranded event(s)"
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
        spawner: Spawner | None = None,
        routing: LLMRouting | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.conn = conn
        self.routines = routines
        # D34: what a step hands to a subprocess it spawns (db, config, locks, slack).
        self.run_env = run_env or RunEnv()
        # D39: with a spawner, `lane: background` slots run in a detached child. Without
        # one (tests, dry runs, `arc routines run`) every job runs inline as before.
        self._spawner = spawner
        # D39: which personas run on a local model (and so take the LLM lock).
        self._routing = routing
        self._sleep = sleep
        self._lane: str | None = None  # "background" while running a claimed child run
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
            start = self._window_start(name, now, since)
            slots = slots_between(spec, start, now)
            if not slots:
                retry = self._catch_up_slot(spec, start, now)
                if retry is not None and spec.catch_up is not None:
                    chain = tuple(spec.chain)
                    if kind is JobKind.PERSONA and halted and not spec.halt_exempt:
                        due.append(DueJob(name, kind, retry, "skip-halted", 0, chain))
                    else:
                        note = (
                            f"catch-up: no {spec.catch_up.until_written} since the last "
                            "regular slot"
                        )
                        due.append(DueJob(name, kind, retry, "run", 0, chain, note))
                continue
            slot = slots[-1]
            collapsed = len(slots) - 1
            chain = tuple(spec.chain)
            if kind is JobKind.PERSONA and halted and not spec.halt_exempt:
                due.append(DueJob(name, kind, slot, "skip-halted", collapsed, chain))
            elif now > catchup_deadline(spec, slot):
                note = f"missed; catch-up window ended {catchup_deadline(spec, slot):%a %H:%M}"
                due.append(DueJob(name, kind, slot, "skip-missed", collapsed, chain, note))
            elif (written := self._session_written(spec, slot)) is not None:
                due.append(DueJob(name, kind, slot, "skip-written", collapsed, chain, written))
            else:
                due.append(DueJob(name, kind, slot, "run", collapsed, chain))
        # Sources first (after_sources), then personas; each group in slot order.
        due.sort(key=lambda d: (d.kind is not JobKind.SOURCE, d.slot, d.job))
        return due

    def _session_written(self, spec: JobSpec, slot: _dt.datetime) -> str | None:
        """E13.5: for ``catch_up.until_written: "<kind>:{day}"``, the skip reason when an
        entry of ``kind`` whose payload ``as_of`` is *slot*'s session is already
        stored (any status: it was written). ``None`` = run the slot. Read-only."""
        from arc.utils.calendar import completed_session

        cu = spec.catch_up
        if cu is None or not cu.per_session:
            return None
        kind, _ = cu.target
        day = completed_session(slot).isoformat()
        try:
            row = self.conn.execute(
                "SELECT 1 FROM context_entries WHERE kind = ? "
                "AND json_extract(payload, '$.as_of') = ? LIMIT 1",
                (kind, day),
            ).fetchone()
        except sqlite3.OperationalError:  # store not migrated: nothing written yet
            row = None
        return f"already written: {kind} for {day}" if row else None

    def _catch_up_slot(
        self, spec: JobSpec, start: _dt.datetime, now: _dt.datetime
    ) -> _dt.datetime | None:
        """E12.2: the latest ``catch_up`` slot in ``(start, now]`` (inside its catch-up
        window), when the job has not written ``catch_up.until_written`` since its
        latest regular slot (or, with no regular slot in ``lookback``, has no valid
        entry at all). ``None`` otherwise. Read-only."""
        cu = spec.catch_up
        if cu is None:
            return None
        retries = [s for s in catch_up_slots(spec, start, now) if now <= catchup_deadline(spec, s)]
        if not retries:
            return None
        if cu.per_session:  # E13.5: written once per session (payload as_of)
            return None if self._session_written(spec, retries[-1]) else retries[-1]
        kind, subject = cu.target
        regular = slots_between(spec, now - cu.lookback, now)
        try:
            if regular:
                row = self.conn.execute(
                    "SELECT 1 FROM context_entries WHERE kind = ? AND subject = ? "
                    "AND created_at >= ? LIMIT 1",
                    (kind, subject, to_db(regular[-1])),
                ).fetchone()
            else:
                row = self.store.query(as_of=now, kinds=[kind], subjects=[subject]) or None
        except sqlite3.OperationalError:  # store not migrated: nothing written yet
            row = None
        return None if row else retries[-1]

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
            t_job = time.monotonic()
            report.outcomes.extend(self._handle_due(d, now))
            ms = int((time.monotonic() - t_job) * 1000)
            report.durations_ms[d.job] = report.durations_ms.get(d.job, 0) + ms
        reclaimed = self._reclaim_stranded(now)
        report.reclaimed = len(reclaimed)
        report.outcomes.extend(self._drain_events(now, reclaimed=reclaimed))
        self.state.set_time(_LAST_TICK, now)
        return report

    @staticmethod
    def _reason(d: DueJob) -> str:
        if d.action == "skip-halted":
            return "halted (persona)"
        if d.action == "skip-missed":
            return d.note
        if d.action == "skip-written":  # E13.5 per-session catch-up already satisfied
            return d.note
        if d.note:  # E12.2 catch-up slot
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
        if self._background(d.job):
            outcomes = [self._spawn_background(d, now, reason)]
        else:
            outcomes = self.run_job(d.job, d.slot, reason="schedule", now=now, note=reason)
        if outcomes[0].status == "deferred":
            # Keep the slot inside the next tick's window so it retries (within
            # its catch-up deadline); earlier collapsed slots stay collapsed.
            self.state.set_time(cursor_key, d.slot - _dt.timedelta(microseconds=1))
        else:
            self.state.set_time(cursor_key, now)
        return outcomes

    # -- background lane (D39) -------------------------------------------------

    def _background(self, job: str) -> bool:
        """True when *job*'s scheduled slots run in a detached child (D39)."""
        found = self.routines.job(job)
        return (
            self._spawner is not None
            and found is not None
            and found[1].lane is Lane.BACKGROUND
            and not found[1].chain
        )

    def _spawn_background(self, d: DueJob, now: _dt.datetime, note: str) -> Outcome:
        """Claim *d*'s slot here, then hand the run to ``arc routines run-claimed``.

        Planning (slot choice, collapse, halt skip, missed recording) and the claim
        stay in the tick, so ``routine_runs`` uniqueness still makes a doubled tick a
        no-op. A job whose lock is held (its previous run is still going) is deferred
        exactly like an inline job: nothing recorded, retried next tick, never queued.
        """
        try:
            with self.locks.hold(d.job):
                pass
        except LockBusyError as exc:
            log.info("routines.deferred", job=d.job, why=str(exc), lane="background")
            return Outcome(d.job, d.slot, "deferred", f"lock busy ({exc})")
        claimed = self.runs.claim(
            job=d.job, scheduled_for=d.slot, reason="schedule", summary=note, now=now
        )
        if claimed is None:
            return Outcome(d.job, d.slot, "duplicate", "already ran for this slot")
        from arc.routines.spawn import arc_command

        argv = arc_command(self.run_env, ["routines", "run-claimed", claimed.run_id])
        assert self._spawner is not None  # _background() checked it
        try:
            pid = self._spawner(argv, _child_env())
        except Exception as exc:  # noqa: BLE001 - recorded + alerted; the next slot retries
            error = f"spawn failed: {type(exc).__name__}: {exc}"
            self.runs.finish(claimed.run_id, status=RunStatus.FAILED, error=error, now=now)
            self._write_manifest(
                claimed, _RunTrace(exc=exc), now=now, started=now, t0=time.monotonic()
            )
            self.heartbeats.alert(now, d.job, error, run_id=claimed.run_id)
            log.error("routines.spawn_failed", job=d.job, run_id=claimed.run_id, error=error)
            return Outcome(d.job, d.slot, "failed", error, run_id=claimed.run_id)
        log.info("routines.spawned", job=d.job, run_id=claimed.run_id, pid=pid)
        return Outcome(
            d.job,
            d.slot,
            "spawned",
            f"{note}; background pid {pid}",
            run_id=claimed.run_id,
            metrics={"pid": pid},
        )

    def run_claimed(self, run_id: str) -> list[Outcome]:
        """``arc routines run-claimed <run_id>``: run a slot the tick claimed (D39).

        Same :meth:`_execute` path as inline (manifest, heartbeats, notifications, gate,
        approvals); only the process differs. In order:

        1. Own the run once: a second invocation for the same run is a ``duplicate``.
        2. Halt check (a persona that is not ``halt_exempt`` is recorded ``skipped``).
        3. ``after_sources``: wait (up to ``tick.after_sources_wait``) for the background
           sources claimed in the same tick, then read whatever is committed.
        4. Take the job's flock (+ the LLM lock for a local model). Busy means a previous
           run is still going: the slot is recorded ``skipped``, never queued.
        """
        run = self.runs.get(run_id)
        if run is None:
            msg = f"unknown run {run_id!r}"
            raise KeyError(msg)
        if run.status is not RunStatus.RUNNING or not self._own_claim(run_id):
            return [Outcome(run.job, run.scheduled_for, "duplicate", "run already handled")]
        self._lane = Lane.BACKGROUND.value
        try:
            return self._run_claimed(run)
        finally:
            self._lane = None
            self.state.delete(_BG_OWNER.format(run=run_id))

    def _own_claim(self, run_id: str) -> bool:
        from arc.context.ttl import to_db

        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
                (_BG_OWNER.format(run=run_id), str(os.getpid()), to_db(now_et())),
            )
        return cur.rowcount == 1

    def _run_claimed(self, run: RoutineRun) -> list[Outcome]:
        kind, spec = self.routines.step(run.job)
        now = (run.started_at or run.scheduled_for).astimezone(ET)  # the tick's `now`
        note = run.summary or "schedule"
        exempt = getattr(spec, "halt_exempt", False)
        if kind is JobKind.PERSONA and not exempt and self._is_halted():
            return [self._finish_unrun(run, now, "halted (persona)")]
        if getattr(spec, "after_sources", False):
            self._await_sources(run, now)
        try:
            with self.locks.hold(*self._lock_names([run.job])):
                outcome = self._execute(run, now=now, event=None, note=note)
        except LockBusyError as exc:
            return [self._finish_unrun(run, now, f"lock busy ({exc}): previous run still going")]
        return [outcome, *self._fire_completed([outcome], now, 0)]

    def _finish_unrun(self, run: RoutineRun, now: _dt.datetime, why: str) -> Outcome:
        self.runs.finish(run.run_id, status=RunStatus.SKIPPED, summary=why, now=now)
        trace = _RunTrace(metrics={"background_skipped": why})
        self._write_manifest(run, trace, now=now, started=now_et(), t0=time.monotonic())
        log.info("routines.skipped", job=run.job, run_id=run.run_id, why=why, lane="background")
        return Outcome(run.job, run.scheduled_for, "skipped", why, run_id=run.run_id)

    def _await_sources(self, run: RoutineRun, now: _dt.datetime) -> None:
        """D39 ``after_sources`` for a background persona.

        Inline sources already finished (the tick runs sources first). Background
        sources claimed by the same tick (same ``started_at``) may still be running:
        wait for them, bounded by ``tick.after_sources_wait``, then go on with what is
        committed. A slow source never holds the Scalp past that bound.
        """
        wait_s = self.routines.tick.after_sources_wait.total_seconds()
        deadline = time.monotonic() + wait_s
        while True:
            pending = self._running_sources(now)
            if not pending:
                return
            if time.monotonic() >= deadline:
                log.warning("routines.after_sources_timeout", job=run.job, pending=pending)
                return
            self._sleep(min(2.0, max(0.0, deadline - time.monotonic())) or 0.01)

    def _running_sources(self, now: _dt.datetime) -> list[str]:
        from arc.context.ttl import to_db

        names = [n for n, spec in self.routines.sources.items() if spec.lane is Lane.BACKGROUND]
        if not names:
            return []
        marks = ",".join("?" for _ in names)
        rows = self.conn.execute(
            f"SELECT job FROM routine_runs WHERE status = 'running' AND started_at = ?"  # noqa: S608
            f" AND job IN ({marks})",
            (to_db(now), *names),
        ).fetchall()
        return sorted(r["job"] for r in rows)

    # -- running -------------------------------------------------------------

    def _llm(self, kind: JobKind, spec: StepSpec) -> bool:
        return spec.llm if spec.llm is not None else kind is JobKind.PERSONA

    def _routing_config(self) -> LLMRouting | None:
        if self._routing is None:
            from arc.llm_routing import DEFAULT_ROUTING_PATH, load_routing

            try:
                factory = self._settings_factory
                path = (factory().llm_routing_file if factory else None) or DEFAULT_ROUTING_PATH
                self._routing = load_routing(path)
            except Exception as exc:  # noqa: BLE001 - unknown routing: serialise (fail-safe)
                log.warning("routines.llm_routing_unavailable", error=str(exc))
                return None
        return self._routing

    def _local_llm(self, name: str) -> bool:
        """D39: does *name* (a job or chain step) call a local, on-device model?

        The persona is the step's name (``scalp.overnight`` -> ``scalp``,
        ``risk.reallocate`` -> ``risk``) or its chain-step owner (``quant``,
        ``propose`` ...). Fail-safe: an unreadable routing file, or a step with no
        persona while any tier is local, counts as local (takes the lock).
        """
        routing = self._routing_config()
        if routing is None:
            return True
        from arc.llm_routing import Persona

        base = name.split(".", 1)[0]
        persona = _STEP_PERSONA.get(name) or (base if base in {p.value for p in Persona} else None)
        if persona is None:
            return any(t.local for t in routing.tiers.values())
        return routing.is_local(persona)

    def _lock_names(self, steps: Sequence[str]) -> list[str]:
        """The job's own flock, plus the global LLM lock iff a step runs a local model.

        D39: remote API routes (every tier today) run concurrently; the LLM lock only
        serialises on-device models (``local: true`` in ``config/llm_routing.yaml``).
        """
        needs_llm = False
        for step in steps:
            kind, spec = self.routines.step(step)
            if self._llm(kind, spec) and self._local_llm(step):
                needs_llm = True
                break
        return [steps[0], *([LLM_LOCK] if needs_llm else [])]

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
        if self.routines.job(job) is None:
            job = current_job_name(job)  # D56: `investor` / `auditor` load as aliases
        found = self.routines.job(job)
        if found is None:
            msg = f"unknown job {job!r}"
            raise KeyError(msg)
        kind, spec = found
        steps = [job, *(spec.chain if chain else [])]
        chain_run_id = f"chain-{uuid.uuid4().hex[:12]}" if len(steps) > 1 else None
        lock_names = self._lock_names(steps)
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
                # (or a Scalp holding the LLM lock); it is recorded as skipped and
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
            with self.locks.hold(*self._lock_names(steps)):
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

    def run_paired(
        self,
        steps: list[str],
        scheduled_for: _dt.datetime,
        *,
        now: _dt.datetime,
        chain_run_id: str,
        reused: Mapping[str, RoutineRun],
    ) -> list[Outcome]:
        """E10.2 (D44): an experiment arm's paired copy of a control loop slot.

        Runs in the arm's store as the same scheduled loop slot (loop deadline,
        no-change digest), resuming at the fork step: *reused* holds the arm's ``ok``
        rows for the steps before it (their outputs were imported from control's
        chain), exactly like :meth:`resume_chain`. Locks are this dispatcher's (the
        arm's own lock dir), so the arm never holds control's ``llm`` lock.
        """
        try:
            with self.locks.hold(*self._lock_names(steps)):
                return self._run_steps(
                    steps,
                    scheduled_for,
                    reason="schedule",
                    now=now,
                    chain_run_id=chain_run_id,
                    existing=reused,
                )
        except LockBusyError as exc:
            return [Outcome(steps[0], scheduled_for, "deferred", f"lock busy ({exc})")]

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
        # may finish; no later step starts. `no_change` (Research found the same
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
        # D38: an action chain (the position manager) opens the same root line, but
        # only once it has a proposal, so a close shows as SELL and quiet runs post nothing.
        is_action = (
            bool(chain_run_id)
            and reason == "schedule"
            and steps[0] in self.routines.loop.action_roots
            and not is_loop
        )
        try:
            for index, step in enumerate(steps):
                if is_action and index and root_ts is None and chain_run_id:
                    root_ts = self._open_action_root(chain_run_id, scheduled_for)
                    self._loop_root_ts = root_ts
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
                if (
                    is_loop
                    and index
                    and step not in DEADLINE_EXEMPT_STEPS
                    and time.monotonic() > deadline
                ):
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
                # E13.9: a step that needs more of the loop budget than is left
                # (``min_remaining_s``, e.g. quant.revise's extra LLM call) is skipped;
                # later steps still run (generic; reused by E13.18's exit path).
                left = deadline - time.monotonic()
                need = step_spec.min_remaining_s
                if is_loop and index and need is not None and left < need:
                    log.info(
                        "routines.step_skipped_deadline",
                        job=step,
                        chain_run_id=chain_run_id,
                        needs_s=need,
                        left_s=round(left, 1),
                    )
                    outcomes.append(
                        self._record_skipped_step(
                            step,
                            scheduled_for,
                            reason=f"chain:{steps[0]}",
                            chain_run_id=chain_run_id,
                            step_index=index,
                            summary=f"step_skipped_deadline: needs {need}s of the "
                            f"{self._max_runtime_label()} loop budget, {max(left, 0):.0f}s left",
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
                # E13.9: an optional step that skips itself (``JobSkippedError(...,
                # continue_chain=True)``, e.g. quant.revise with nothing to revise)
                # lets the chain go on.
                optional_skip = outcome.status == "skipped" and bool(
                    outcome.metrics.get("continue_chain")
                )
                stop = (outcome.status != "ok" and not optional_skip) or bool(
                    outcome.metrics.get("stop_chain")
                )
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
            elif is_action and chain_run_id:
                if root_ts is None:  # the chain stopped right after proposing
                    root_ts = self._open_action_root(chain_run_id, scheduled_for)
                if root_ts:
                    self._finish_action_root(chain_run_id, scheduled_for, root_ts)
        finally:
            self._loop_root_ts = None
            self._loop_no_change = False
            if root_ts:
                self.heartbeats.close_loop_root()
        return outcomes

    def _open_action_root(self, chain_run_id: str, slot: _dt.datetime) -> str | None:
        """D38: the position manager's root line, opened once its chain proposed something.

        Same one-line format as the trading loop (``SELL: IWM`` once the close
        fills); the proposal cards and later step posts thread under it. Equity,
        day P&L and the order budget come from the latest loop / manifest data,
        because this chain writes no ``portfolio_context`` of its own.
        """
        from arc.journal.store import JournalStore
        from arc.routines.config import LoopLayout
        from arc.routines.loop import LoopState, latest_account_facts, loop_root_from_db

        if self.routines.loop.slack_layout is not LoopLayout.ROOT_PER_LOOP:
            return None
        if not JournalStore(self.conn).proposals_in_chain(chain_run_id):
            return None
        root = loop_root_from_db(
            self.conn, chain_run_id, slot, fallback=latest_account_facts(self.conn, slot)
        )
        ts = self.heartbeats.open_loop_root(root.text())
        if ts:
            state = LoopState(self.conn)
            state.set_thread_ts(chain_run_id, ts)
            state.set_root(chain_run_id, root.model_dump(mode="json"))
        log.info("routines.action_root", chain_run_id=chain_run_id, ts=ts, text=root.text())
        return ts

    def _finish_action_root(self, chain_run_id: str, slot: _dt.datetime, root_ts: str) -> None:
        """D38: re-render the action root from what the chain wrote (WORKING / SELL)."""
        from arc.routines.loop import LoopRoot, LoopState, loop_root_from_db

        state = LoopState(self.conn)
        prev = LoopRoot.model_validate(state.root(chain_run_id) or {"slot": slot})
        line = loop_root_from_db(self.conn, chain_run_id, slot, fallback=prev)
        state.set_root(chain_run_id, line.model_dump(mode="json"))
        if line.text() == prev.text():
            return
        try:
            self.heartbeats.update_loop_root(root_ts, line.text())
        except Exception as exc:  # noqa: BLE001 - the chain's work is committed; the edit is best-effort
            log.warning("routines.loop_root_update_failed", chain_run_id=chain_run_id, err=str(exc))

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

    def _post_scalp_context(self, now: _dt.datetime, ctx: JobContext) -> str | None:
        """D36 thread item 1: the ``[Scalp]`` card of the candidates Research read.

        Rendered from Research run's recorded context snapshot (the same
        ``candidate`` entries it was given), so the card names the Scalp run
        and its time without re-posting the Scalp's own 30-min card.
        """
        from arc.slack.digests import scalp_context_card

        entries = ctx.snapshot.of_kind("candidate")
        card = scalp_context_card(entries, chain_run_id=ctx.chain_run_id)
        return self.heartbeats.summary(now, "scalp", card.text, blocks=card.blocks)

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
            if exc.notice:
                trace.notifications.append(self.heartbeats.notice(now, run.job, exc.notice))
            return self._outcome(
                run,
                "skipped",
                str(exc),
                metrics={"continue_chain": True} if exc.continue_chain else None,
            )
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
        for card in result.extra_cards:
            posts.append(self.heartbeats.card(now, card.text, card.blocks or None))
        in_loop_thread = bool(self._loop_root_ts)
        if in_loop_thread and (self._loop_no_change or result.metrics.get("no_change")):
            # D36: a no_change loop gets only the [Routines] metadata reply in its thread;
            # the step's own summary is folded into that reply (``execute=…ms``).
            log.info("routines.ok", job=run.job, run_id=run.run_id, outputs=len(ctx.outputs))
            return self._outcome(run, "ok", summary, metrics=result.metrics)
        if in_loop_thread and run.step_index == 0:
            # D36 thread item 1: the Scalp context Research read, before its own card.
            posts.append(self._post_scalp_context(now, ctx))
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
                metrics={**trace.metrics, **({"lane": self._lane} if self._lane else {})},
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

    def _reclaim_stranded(self, now: _dt.datetime) -> set[str]:
        """E6.2e: release dispatched events whose Broker never claimed a run.

        ``broker.execute`` claims an approval event, then spawns the Broker; a child that
        dies before :meth:`run_event` claims its run would strand the event forever
        (invisible to the drain, never consumed). After ``tick.dispatch_grace`` with
        no ``routine_runs.event_id`` row the claim is undone, so this tick's drain
        handles the event on the normal path. Returns the released event ids.
        """
        cutoff = now - self.routines.tick.dispatch_grace
        released: set[str] = set()
        for ev in self.events.stranded(dispatched_before=cutoff):
            if ev.dispatched_at is None or not self.events.reclaim(
                ev.id, dispatched_at=ev.dispatched_at
            ):
                continue  # the run started (or it was consumed) in the meantime
            released.add(ev.id)
            log.warning(
                "routines.event_reclaimed",
                event_id=ev.id,
                event_name=ev.name,
                dispatched_at=ev.dispatched_at.isoformat(),
                dispatched_by=ev.dispatched_by,
                age_s=int((now - ev.dispatched_at).total_seconds()),
            )
        return released

    def _drain_events(
        self, now: _dt.datetime, *, reclaimed: set[str] | frozenset[str] = frozenset()
    ) -> list[Outcome]:
        """Fire every pending external event once (E6.2d dispatch-once semantics).

        - An event the D34 ``broker.execute`` step dispatched is never pending here: its
          Broker subprocess owns it, until E6.2e reclaims it (*reclaimed*: its
          Broker never started within ``tick.dispatch_grace``). A reclaimed
          approval past its proposal's TTL lapses on the record instead of running.
        - An ``approval`` that arrives (or is still waiting) while halted is
          ``deferred`` until its proposal's ``expires_at``; after ``!resume`` inside
          that TTL the Broker runs. Past it the event is consumed with a journal
          row (``order:refused`` "approval lapsed under halt") and the card updated.
        - Each event-triggered run is keyed by the event id, so two events with the
          same ``created_at`` both run (never ``duplicate``).
        """
        out: list[Outcome] = []
        for ev in self.events.pending(until=now):
            if ev.id in reclaimed and (late := self._reclaimed_lapse(ev, now)) is not None:
                out.extend(late)
                continue
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
        from arc.broker.ladder_job import approval_deadline

        if ev.name != "approval":
            return None
        targets = self._halt_targets(ev)
        if not targets:
            return None
        phash = str(ev.payload.get("proposal_hash") or "")
        deadline = approval_deadline(self.conn, phash) if phash else None
        if deadline is None:
            return None  # unknown proposal: let the Broker refuse it on the record
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

    def _reclaimed_lapse(self, ev: RoutineEvent, now: _dt.datetime) -> list[Outcome] | None:
        """E6.2e: a reclaimed approval past its proposal TTL lapses; ``None`` = handle normally."""
        from arc.broker.ladder_job import LAPSED_NOT_STARTED, approval_deadline

        if ev.name != "approval":
            return None
        targets = self._halt_targets(ev)
        phash = str(ev.payload.get("proposal_hash") or "")
        deadline = approval_deadline(self.conn, phash) if phash else None
        if not targets or deadline is None or now < deadline:
            return None
        return self._lapse(ev, phash, targets, now, reason=LAPSED_NOT_STARTED)

    def _lapse(
        self,
        ev: RoutineEvent,
        phash: str,
        targets: list[str],
        now: _dt.datetime,
        *,
        reason: str | None = None,
    ) -> list[Outcome]:
        """The approval's TTL passed (under a halt, or E6.2e never started): no order.

        Journal row, card update, event consumed.
        """
        from arc.broker.ladder_job import LAPSED_UNDER_HALT, lapse_approval

        why = reason or LAPSED_UNDER_HALT
        out: list[Outcome] = []
        run_ids: list[str] = []
        for job in targets:
            run = self.runs.claim(
                job=job,
                scheduled_for=ev.created_at,
                reason=f"event:{ev.name}",
                status=RunStatus.SKIPPED,
                summary=why,
                now=now,
                event_id=ev.id,
            )
            if run is None:
                continue
            run_ids.append(run.run_id)
            self._write_manifest(
                run, _RunTrace(event_id=ev.id), now=now, started=now, t0=time.monotonic()
            )
            out.append(Outcome(job, ev.created_at, "skipped", why, run_id=run.run_id))
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
            reason=why,
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

        Used by the in-chain ``broker.execute`` step to hand an auto-approved proposal to
        a Broker subprocess. The run joins the chain (``chain_run_id``, next step
        index) so ``arc context trace <chain>`` shows it, holds a per-event lock
        (``<job>:<event id>``) rather than the job's lock, and never the LLM lock, so
        ladders run in parallel with the next loop.

        E6.2d: the event is normally already ``dispatched`` (claimed by ``broker.execute``
        before the spawn); that is accepted. The run is keyed by the event id, so a
        second invocation for the same event is a ``duplicate`` and never a second
        ladder. A consumed event is refused. While halted (and *job* is not
        ``halt_exempt``) nothing runs: the event is released back to the tick's
        drain, which defers it until ``!resume`` or its TTL. Otherwise the event is
        consumed by this run whatever the outcome.
        """
        if self.routines.job(job) is None:
            job = current_job_name(job)  # D56: `investor` / `auditor` load as aliases
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
        if self.routines.job(job) is None:
            job = current_job_name(job)  # D56: `investor` / `auditor` load as aliases
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


def _child_env() -> dict[str, str]:
    """Environment of a D39 background child: this process's plus its correlation ids.

    A tick run by hand mints its ``tick_id`` in-process (no ``ARC_TICK_ID`` in the
    environment), so the bound ids are exported, and the child's log lines and run
    manifest join the tick's (``arc health trace <tick_id>``).
    """
    from arc.monitoring.correlation import ENV_KEYS

    env = dict(os.environ)
    bound = structlog.contextvars.get_contextvars()
    for var, key in ENV_KEYS.items():
        if bound.get(key):
            env[var] = str(bound[key])
    return env


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
