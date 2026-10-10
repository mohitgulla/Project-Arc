"""Health checks (E8.2). Each returns :class:`Finding` objects; nothing here posts.

- :func:`missed_windows`  a scheduled routine slot whose catch-up window closed
  without the slot running (no attempt, or recorded as ``skipped`` because it
  was missed). Halt skips and "no handler yet" skips are intended, not misses;
  failures are already alerted by the dispatcher. E8.2a: only for jobs whose
  slots are at least ``monitoring.per_slot_min_interval`` apart.
- :func:`slot_coverage`   E8.2a: a fast job (5-min loop, monitor, ...) that ran
  fewer than ``coverage_min`` of its slots judged in ``coverage_window``: one
  ``coverage:<job>`` condition naming the likely cause from the tick heartbeats.
- :func:`tick_slow`       E8.2a: ticks taking > ``tick_slow_after`` (E5.10
  ``tick_duration_ms``) or p90 spacing > 1.5 x ``tick.interval``.
- :func:`tick_staleness`  no ``tick`` heartbeat for ``tick_stale_after``.
- :func:`stuck_runs`      a ``routine_runs`` row still ``running`` after ``stuck_after``.
- :func:`earnings_coverage` E4.1d: ``coverage:earnings`` while no earnings doc is
  newer than ``earnings_stale_after`` and the universe has a non-ETF ticker.
- :func:`scout_coverage` E13.7: ``coverage:scout`` while the Scout's latest discovery
  fill is below ``funnel.scout.min_discovery_alert``.
- :func:`momentum_coverage` E12.2: ``coverage:universe.momentum`` while the momentum
  tier's SPMO holdings as-of date is older than ``momentum.stale_after_days``.
- :func:`iv_crosscheck`  E4.12: the latest recorded 30-DTE IV differs from Cboe's
  ``iv30`` by more than ``iv_crosscheck_max_pts`` for some name.
- :func:`stranded_events` E6.2e: a dispatched event with no run after ``tick.dispatch_grace``.
- :func:`approvals_unposted` E6.1b: a pending approval request older than one tick
  with no Slack card (its post failed and keeps failing).
- :func:`gateway_health`  ``hermes gateway status`` and ``hermes cron status``.
- :func:`remote_access`   E8.6: Hermes dashboard (gated) + tower on the tailnet, not the LAN.

The watchdog only judges slots after the first tick heartbeat, so installing
monitoring never back-fills alerts for the time before routines ran.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from arc.context.ttl import from_db, to_db
from arc.journal.legacy import cutovers as legacy_cutovers
from arc.journal.legacy import legacy_names
from arc.monitoring.store import HeartbeatRepo
from arc.routines.schedule import catchup_deadline, slots_between
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.monitoring.config import GatewayCheck, MonitoringSettings, RemoteAccessCheck
    from arc.monitoring.store import Heartbeat
    from arc.routines.config import JobSpec, RoutinesConfig

Severity = Literal["ok", "degraded", "failed"]

# One-off findings (a missed slot happened) vs. conditions (open until they pass).
ONE_OFF = "one_off"
CONDITION = "condition"


@dataclass(frozen=True)
class Finding:
    key: str  # dedupe key, e.g. missed:research:2026-09-28T13:30:00.000000Z
    kind: str  # missed_window | coverage | tick_slow | tick_stale | stuck_run | gateway_*
    severity: Severity
    message: str
    mode: str = CONDITION
    alert: bool = True  # False: recorded in the health heartbeat only
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    summary: str
    findings: tuple[Finding, ...] = ()
    # Check-wide facts (E8.2a: per-job coverage), used for resolve lines.
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Routine windows
# ---------------------------------------------------------------------------


def _is_missed_row(row: sqlite3.Row) -> bool:
    return row["status"] == "skipped" and str(row["summary"] or "").startswith("missed")


HALTED = "halted"


def _is_halted_row(row: sqlite3.Row) -> bool:
    return row["status"] == "skipped" and str(row["summary"] or "").startswith(HALTED)


def _slot_attempted(
    conn: sqlite3.Connection, job: str, slot: _dt.datetime, deadline: _dt.datetime
) -> str | None:
    """How (job, slot) was handled: its row's status, ``"missed"``, or ``None``.

    The dispatcher collapses several due slots into the latest one, so a slot
    with no row of its own counts as covered when a later slot of the same job
    was attempted inside this slot's catch-up window. A slot skipped because the
    persona was halted is ``"halted"``: intended, never a miss, and not counted
    by the coverage check (E8.2a).
    """
    # D54/D56: slots before a rename were recorded under the old job name ('scout*' then
    # 'sweep*' for the Scalp, 'director' for Research); after the D54 cutover 'scout' is
    # the slow-feed persona, never a Scalp slot.
    names = [job]
    cuts = legacy_cutovers(conn)
    for old, key in legacy_names(job):
        cut = cuts.get(key)
        if cut is None or slot < cut:
            names.append(old)
    marks = ",".join("?" * len(names))
    rows = conn.execute(
        f"""SELECT scheduled_for, status, summary FROM routine_runs
           WHERE job IN ({marks}) AND step_index = 0
             AND scheduled_for >= ? AND scheduled_for <= ?
           ORDER BY scheduled_for""",  # noqa: S608 - placeholders only
        (*names, to_db(slot), to_db(deadline)),
    ).fetchall()
    own = [r for r in rows if r["scheduled_for"] == to_db(slot)]
    if own and not _is_missed_row(own[0]):
        return HALTED if _is_halted_row(own[0]) else str(own[0]["status"])
    live = [r for r in rows if not _is_missed_row(r)]
    if live:
        return HALTED if all(_is_halted_row(r) for r in live) else "collapsed"
    return "missed" if own or rows else None


def slot_interval(spec: JobSpec) -> _dt.timedelta:
    """How far apart a job's slots are: ``every``, the closest two ``schedule`` times, or 1 day."""
    if spec.every is not None:
        return spec.every
    times = sorted(spec.schedule)
    gaps = [
        _dt.datetime.combine(_dt.date.min, b) - _dt.datetime.combine(_dt.date.min, a)
        for a, b in zip(times, times[1:], strict=False)
    ]
    return min(gaps) if gaps else _dt.timedelta(days=1)


def per_slot(spec: JobSpec, settings: MonitoringSettings) -> bool:
    """E8.2a: True when a missed slot of this job is alerted on its own (slow cadence)."""
    return slot_interval(spec) >= settings.per_slot_min_interval


def missed_windows(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: MonitoringSettings,
    now: _dt.datetime,
) -> CheckResult:
    """One ``missed_window`` finding per missed slot of a slow-cadence job.

    E8.2a: jobs faster than ``monitoring.per_slot_min_interval`` are judged by
    :func:`slot_coverage` instead (one condition per job, not one alert per slot).
    """
    first = HeartbeatRepo(conn).first("tick")
    if first is None:
        return CheckResult("routine_windows", "ok", "not judged: no tick heartbeat yet")
    start = max(first.at, now - settings.miss_lookback)
    findings: list[Finding] = []
    judged = 0
    for name, (_kind, spec) in routines.jobs().items():
        if not per_slot(spec, settings):
            continue
        for slot in slots_between(spec, start, now):
            deadline = catchup_deadline(spec, slot)
            if now <= deadline + settings.miss_grace:
                continue
            judged += 1
            status = _slot_attempted(conn, name, slot, deadline)
            if status not in (None, "missed"):
                continue
            why = "recorded as missed" if status == "missed" else "never ran"
            findings.append(
                Finding(
                    key=f"missed:{name}:{to_db(slot)}",
                    kind="missed_window",
                    severity="failed",
                    mode=ONE_OFF,
                    message=(
                        f"{name} {slot:%a %m-%d %H:%M %Z} missed its window "
                        f"(closed {deadline:%H:%M}; {why})"
                    ),
                    detail={
                        "job": name,
                        "slot": slot.isoformat(),
                        "deadline": deadline.isoformat(),
                    },
                )
            )
    summary = f"{judged} slot(s) judged since {start:%m-%d %H:%M}, {len(findings)} missed"
    return CheckResult("routine_windows", "failed" if findings else "ok", summary, tuple(findings))


# ---------------------------------------------------------------------------
# Slot coverage + slow ticks (E8.2a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    """How many of a job's judged slots ran. Halted slots count in neither number."""

    job: str
    ran: int = 0
    judged: int = 0
    halted: int = 0
    missed: tuple[_dt.datetime, ...] = ()

    @property
    def ratio(self) -> float:
        return self.ran / self.judged if self.judged else 1.0

    @property
    def text(self) -> str:
        return f"{self.ran}/{self.judged}"


def job_coverage(
    conn: sqlite3.Connection, job: str, spec: JobSpec, slots: list[_dt.datetime]
) -> Coverage:
    """Judge *slots* of *job* the way :func:`missed_windows` does (collapse aware)."""
    ran = halted = 0
    missed: list[_dt.datetime] = []
    for slot in slots:
        status = _slot_attempted(conn, job, slot, catchup_deadline(spec, slot))
        if status == HALTED:
            halted += 1
        elif status in (None, "missed"):
            missed.append(slot)
        else:
            ran += 1
    return Coverage(job, ran, ran + len(missed), halted, tuple(missed))


def fmt_duration(td: _dt.timedelta) -> str:
    """``8m03s`` (whole seconds)."""
    secs = max(0, int(td.total_seconds()))
    return f"{secs // 60}m{secs % 60:02d}s"


@dataclass(frozen=True)
class TickStats:
    """``tick`` heartbeats in a window: wall times (E5.10), slowest job, spacing."""

    ticks: int
    durations: tuple[_dt.timedelta, ...]
    top_job: tuple[str, _dt.timedelta] | None
    spacings: tuple[_dt.timedelta, ...]

    @property
    def max_duration(self) -> _dt.timedelta | None:
        return max(self.durations) if self.durations else None

    @property
    def max_spacing(self) -> _dt.timedelta | None:
        return max(self.spacings) if self.spacings else None

    @property
    def p90_spacing(self) -> _dt.timedelta | None:
        if not self.spacings:
            return None
        ranked = sorted(self.spacings)
        return ranked[math.ceil(0.9 * len(ranked)) - 1]  # nearest rank

    def slow(self, after: _dt.timedelta) -> int:
        return sum(1 for d in self.durations if d > after)


def tick_stats(beats: list[Heartbeat]) -> TickStats:
    durations: list[_dt.timedelta] = []
    top: tuple[str, _dt.timedelta] | None = None
    for b in beats:
        ms = b.detail.get("tick_duration_ms")
        if isinstance(ms, int | float):
            durations.append(_dt.timedelta(milliseconds=ms))
        for item in b.detail.get("slowest_jobs") or []:
            job, jms = item.get("job"), item.get("ms")
            if isinstance(job, str) and isinstance(jms, int | float):
                d = _dt.timedelta(milliseconds=jms)
                if top is None or d > top[1]:
                    top = (job, d)
    ats = [b.at for b in beats]
    spacings = tuple(b - a for a, b in zip(ats, ats[1:], strict=False))
    return TickStats(len(beats), tuple(durations), top, spacings)


def _spacing_limit(routines: RoutinesConfig) -> _dt.timedelta:
    return routines.tick.interval * 1.5


def likely_cause(stats: TickStats, routines: RoutinesConfig, settings: MonitoringSettings) -> str:
    """Why slots were missed, from the window's tick heartbeats (deterministic).

    Slow ticks (E5.10 ``tick_duration_ms``) first; else a tick gap longer than
    1.5 x ``tick.interval`` (also the fallback when durations were not recorded).
    """
    if stats.ticks == 0:
        return "no tick ran in the window"
    longest = stats.max_duration
    if longest is not None and longest > settings.tick_slow_after:
        parts = [f"max {fmt_duration(longest)}"]
        if stats.top_job is not None:
            parts.append(f"{stats.top_job[0]} {fmt_duration(stats.top_job[1])}")
        return f"slow ticks ({', '.join(parts)})"
    gap = stats.max_spacing
    if gap is not None and gap > _spacing_limit(routines):
        return f"tick gaps up to {round(gap.total_seconds() / 60)} min"
    if longest is None:  # pre-E5.10 heartbeats: spacing is all there is
        mins = round(gap.total_seconds() / 60) if gap is not None else None
        ran = f"gaps up to {mins} min" if mins is not None else "1 tick"
    else:
        ran = f"max {fmt_duration(longest)}"
    return f"ticks on time ({ran}); check the job's runs (`arc health trace <run_id>`)"


def _window_slots(
    spec: JobSpec,
    settings: MonitoringSettings,
    *,
    first_tick: _dt.datetime,
    lo: _dt.datetime,
    hi: _dt.datetime,
) -> list[_dt.datetime]:
    """Slots after the first tick whose judge time (deadline + grace) is in ``(lo, hi]``."""
    start = max(first_tick, lo - settings.miss_lookback)
    return [
        s
        for s in slots_between(spec, start, hi)
        if lo < catchup_deadline(spec, s) + settings.miss_grace <= hi
    ]


def slot_coverage(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: MonitoringSettings,
    now: _dt.datetime,
) -> CheckResult:
    """E8.2a: one ``coverage:<job>`` condition per fast job below ``coverage_min``.

    Slots judged in the last ``coverage_window`` (by judge time, so a 5-min job
    has 12 per hour); halted slots are excluded from both counts.
    """
    hb = HeartbeatRepo(conn)
    first = hb.first("tick")
    if first is None:
        return CheckResult("slot_coverage", "ok", "not judged: no tick heartbeat yet")
    lo = now - settings.coverage_window
    stats = tick_stats(hb.between("tick", lo, now))
    mins = round(settings.coverage_window.total_seconds() / 60)
    findings: list[Finding] = []
    per_job: dict[str, dict[str, Any]] = {}
    for name, (_kind, spec) in routines.jobs().items():
        # E4.1d: `coverage:earnings` is the calendar-freshness condition, not a slot ratio.
        if per_slot(spec, settings) or name == EARNINGS_JOB:
            continue
        cov = job_coverage(
            conn, name, spec, _window_slots(spec, settings, first_tick=first.at, lo=lo, hi=now)
        )
        if not cov.judged:
            continue
        per_job[name] = {"ran": cov.ran, "judged": cov.judged, "halted": cov.halted}
        if cov.ratio >= settings.coverage_min:
            continue
        findings.append(
            Finding(
                key=f"coverage:{name}",
                kind="coverage",
                severity="failed",
                message=(
                    f"{name} ran {cov.text} slots in the last {mins} min "
                    f"({cov.ratio:.0%}) · likely cause: "
                    f"{likely_cause(stats, routines, settings)}"
                ),
                detail={
                    "job": name,
                    "ran": cov.ran,
                    "judged": cov.judged,
                    "missed": [s.isoformat() for s in cov.missed],
                },
            )
        )
    summary = (
        ", ".join(f"{j} {d['ran']}/{d['judged']}" for j, d in per_job.items())
        or "no fast-cadence slots judged"
    ) + f" (last {mins} min)"
    return CheckResult(
        "slot_coverage",
        "failed" if findings else "ok",
        summary,
        tuple(findings),
        detail={"window_min": mins, "jobs": per_job},
    )


def tick_slow(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: MonitoringSettings,
    now: _dt.datetime,
) -> CheckResult:
    """E8.2a: ``tick_slow`` when >= ``tick_slow_count`` ticks in the window took longer
    than ``tick_slow_after``, or the p90 tick spacing exceeds 1.5 x ``tick.interval``.

    A tick that stopped altogether is ``tick_stale``'s job, not this one.
    """
    stats = tick_stats(HeartbeatRepo(conn).between("tick", now - settings.coverage_window, now))
    mins = round(settings.coverage_window.total_seconds() / 60)
    slow = stats.slow(settings.tick_slow_after)
    p90, limit = stats.p90_spacing, _spacing_limit(routines)
    too_slow = slow >= settings.tick_slow_count
    too_sparse = p90 is not None and p90 > limit
    facts: list[str] = []
    if stats.max_duration is not None:
        facts.append(f"max {fmt_duration(stats.max_duration)}")
    if stats.top_job is not None:
        facts.append(f"top job {stats.top_job[0]} {fmt_duration(stats.top_job[1])}")
    if p90 is not None:
        facts.append(f"spacing p90 {fmt_duration(p90)}")
    summary = f"{stats.ticks} tick(s) in the last {mins} min" + (
        f": {', '.join(facts)}" if facts else ""
    )
    if not (too_slow or too_sparse):
        return CheckResult("tick_slow", "ok", summary)
    why: list[str] = []
    if too_slow:
        why.append(
            f"{slow} of {stats.ticks} ticks in the last {mins} min took > "
            f"{fmt_duration(settings.tick_slow_after)}"
        )
    if too_sparse and p90 is not None:
        why.append(f"tick spacing p90 {fmt_duration(p90)} > {fmt_duration(limit)}")
    f = Finding(
        key="tick_slow",
        kind="tick_slow",
        severity="failed",
        message=f"routines ticks are slow: {'; '.join(why)} ({', '.join(facts)})",
        detail={
            "slow_ticks": slow,
            "ticks": stats.ticks,
            "max_ms": int(stats.max_duration.total_seconds() * 1000)
            if stats.max_duration is not None
            else None,
            "top_job": stats.top_job[0] if stats.top_job else None,
        },
    )
    return CheckResult("tick_slow", "failed", summary, (f,))


# ---------------------------------------------------------------------------
# Daily slot rollup (E8.2a: the Broker reconcile card's Ops line)
# ---------------------------------------------------------------------------


def slot_rollup(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    now: _dt.datetime,
) -> list[Coverage]:
    """Coverage of every job's slots today (ET) whose catch-up window closed by *now*."""
    from arc.utils.calendar import ET

    day_start = _dt.datetime.combine(now.astimezone(ET).date(), _dt.time.min, tzinfo=ET)
    out: list[Coverage] = []
    for name, (_kind, spec) in routines.jobs().items():
        slots = [
            s
            for s in slots_between(spec, day_start - _dt.timedelta(microseconds=1), now)
            if catchup_deadline(spec, s) <= now
        ]
        cov = job_coverage(conn, name, spec, slots)
        if cov.judged:
            out.append(cov)
    return out


def rollup_line(covs: list[Coverage], routines: RoutinesConfig, *, max_jobs: int = 6) -> str | None:
    """``Slots: research 71/75, monitor 77/78, scalp 15/15 · missed 6 (list in tower Ops)``.

    Personas first (most slots first), then any source that missed a slot; the
    missed total covers every job.
    """
    if not covs:
        return None
    personas = [c for c in covs if c.job in routines.personas]
    sources = [c for c in covs if c.job not in routines.personas and c.missed]
    shown = sorted(personas, key=lambda c: (-c.judged, c.job))
    shown += sorted(sources, key=lambda c: (-len(c.missed), c.job))
    shown = shown[:max_jobs]
    missed = sum(len(c.missed) for c in covs)
    jobs = ", ".join(f"{c.job} {c.text}" for c in shown)
    tail = f"missed {missed} (list in tower Ops)" if missed else "missed 0"
    return f"Slots: {jobs} · {tail}" if jobs else f"Slots: {tail}"


def approvals_line(conn: sqlite3.Connection, now: _dt.datetime) -> str | None:
    """E6.1b: ``Approvals: sweep failed 2 · card posts failed 3 · re-posted 3 · unposted 0``.

    Sums today's (ET) ``tick`` heartbeats' ``approvals`` detail and counts the
    pending requests that have no card right now. ``None`` when all are zero.
    """
    from arc.utils.calendar import ET

    day_start = _dt.datetime.combine(now.astimezone(ET).date(), _dt.time.min, tzinfo=ET)
    totals = {"sweep_failed": 0, "post_failed": 0, "reposted": 0}
    for hb in HeartbeatRepo(conn).between("tick", day_start - _dt.timedelta(microseconds=1), now):
        counts = hb.detail.get("approvals") or {}
        for k in totals:
            totals[k] += int(counts.get(k) or 0)
    row = conn.execute(
        """SELECT COUNT(*) FROM approval_requests
           WHERE status = 'pending' AND message_ts IS NULL AND channel != 'log'
             AND expires_at > ?""",
        (to_db(now),),
    ).fetchone()
    unposted = int(row[0])
    if not any(totals.values()) and not unposted:
        return None
    return (
        f"Approvals: sweep failed {totals['sweep_failed']} · card posts failed "
        f"{totals['post_failed']} · re-posted {totals['reposted']} · unposted {unposted}"
    )


def tick_staleness(
    conn: sqlite3.Connection, settings: MonitoringSettings, now: _dt.datetime
) -> CheckResult:
    last = HeartbeatRepo(conn).latest("tick")
    if last is None:
        f = Finding(
            key="tick_stale",
            kind="tick_stale",
            severity="failed",
            message="no `arc routines tick` heartbeat recorded yet (is the arc-routines-tick "
            "cron installed? `hermes cron list`)",
        )
        return CheckResult("tick", "failed", "no tick heartbeat yet", (f,))
    age = now - last.at
    minutes = int(age.total_seconds() // 60)
    tick_id = last.correlation.get("tick_id", "?")
    if age > settings.tick_stale_after:
        f = Finding(
            key="tick_stale",
            kind="tick_stale",
            severity="failed",
            message=(
                f"last routines tick was {minutes} min ago ({last.at:%m-%d %H:%M %Z}, "
                f"{tick_id}); limit {int(settings.tick_stale_after.total_seconds() // 60)} min"
            ),
            detail={"last_tick": last.at.isoformat(), "tick_id": tick_id},
        )
        return CheckResult("tick", "failed", f"stale: {minutes} min", (f,))
    note = f"last tick {minutes} min ago ({tick_id}, {last.status})"
    return CheckResult("tick", "ok", note)


def stuck_runs(
    conn: sqlite3.Connection, settings: MonitoringSettings, now: _dt.datetime
) -> CheckResult:
    # Per-job overrides (``stuck_after_jobs``) can only shorten or lengthen the
    # limit for their job: select with the shortest limit, then filter per job.
    shortest = min([settings.stuck_after, *settings.stuck_after_jobs.values()])
    rows = [
        r
        for r in conn.execute(
            """SELECT * FROM routine_runs
               WHERE status = 'running' AND started_at IS NOT NULL AND started_at < ?""",
            (to_db(now - shortest),),
        ).fetchall()
        if from_db(r["started_at"]) < now - settings.stuck_after_for(r["job"])
    ]
    findings = tuple(
        Finding(
            key=f"stuck:{r['run_id']}",
            kind="stuck_run",
            severity="failed",
            message=(
                f"{r['job']} run {r['run_id']} still running since "
                f"{from_db(r['started_at']):%m-%d %H:%M %Z}"
            ),
            detail={"run_id": r["run_id"], "chain_run_id": r["chain_run_id"], **_liveness(r, now)},
        )
        for r in rows
    )
    return CheckResult(
        "stuck_runs", "failed" if findings else "ok", f"{len(findings)} stuck", findings
    )


def _liveness(r: sqlite3.Row, now: _dt.datetime) -> dict[str, Any]:
    """E11.2 (D72): the run's recorded pid / heartbeat, read-only (tower + Ops page)."""
    from arc.routines.runs import pid_alive

    keys = r.keys()
    pid = r["pid"] if "pid" in keys else None
    beat = r["heartbeat_at"] if "heartbeat_at" in keys else None
    age = round((now - from_db(beat)).total_seconds(), 1) if beat else None
    return {"pid": pid, "pid_alive": pid_alive(pid), "heartbeat_age_s": age}


EARNINGS_JOB = "earnings"


def earnings_coverage(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: MonitoringSettings,
    now: _dt.datetime,
    universe: list[str],
) -> CheckResult:
    """E4.1d: ``coverage:earnings`` while no earnings doc was ingested within
    ``earnings_stale_after`` and the universe holds a non-ETF ticker.

    Without a fresh calendar ``next_earnings`` is empty, so every stock is
    ``earnings_unknown`` and the gate's earnings blackout has no dates. Only judged
    while the ``earnings`` source job is enabled.
    """
    from arc.pipeline.market import ETF_UNDERLYINGS

    if EARNINGS_JOB not in routines.jobs():
        return CheckResult("earnings_coverage", "ok", "not judged: earnings job disabled")
    stocks = sorted({t.upper() for t in universe} - ETF_UNDERLYINGS)
    if not stocks:
        return CheckResult("earnings_coverage", "ok", "not judged: ETF-only universe")
    row = conn.execute(
        "SELECT MAX(ingested_at) AS last, COUNT(*) AS n FROM raw_docs WHERE source = ?",
        (EARNINGS_JOB,),
    ).fetchone()
    try:
        last = from_db(row["last"]) if row and row["last"] else None
    except ValueError:  # a hand-written ingested_at: judge as never stored
        last = None
    days = settings.earnings_stale_after.total_seconds() / 86_400
    if last is not None and now - last <= settings.earnings_stale_after:
        age_h = (now - last).total_seconds() / 3600
        return CheckResult(
            "earnings_coverage", "ok", f"last earnings doc {age_h:.0f} h ago ({row['n']} stored)"
        )
    run = conn.execute(
        """SELECT status, summary, error FROM routine_runs WHERE job = ?
           ORDER BY scheduled_for DESC LIMIT 1""",
        (EARNINGS_JOB,),
    ).fetchone()
    last_run = (
        f"last run {run['status']}: {run['error'] or run['summary'] or ''}".rstrip(": ")
        if run
        else "no earnings run recorded"
    )
    seen = f"last stored {last:%m-%d %H:%M %Z}" if last else "none ever stored"
    f = Finding(
        key=f"coverage:{EARNINGS_JOB}",
        kind="coverage",
        severity="failed",
        message=(
            f"earnings calendar stale: no earnings doc in the last {days:g} d ({seen}) "
            f"while the universe has {len(stocks)} stock(s), e.g. {', '.join(stocks[:3])}; "
            f"the gate's earnings blackout has no dates · {last_run}"
        ),
        detail={"job": EARNINGS_JOB, "last": last.isoformat() if last else None},
    )
    return CheckResult("earnings_coverage", "failed", f"stale ({seen})", (f,))


MOMENTUM_JOB = "universe.momentum"


def momentum_coverage(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    now: _dt.datetime,
    *,
    stale_after_days: int,
) -> CheckResult:
    """E12.2: ``coverage:universe.momentum`` while the latest momentum tier entry's
    source as-of date (the SPMO holdings page) is older than *stale_after_days*.

    The entry is kept (it is still the best list there is); this only flags it. No
    entry at all is not judged here: the job's failed runs are already alerted, and
    the dispatcher's catch-up retries it every session.
    """
    from arc.universe.tiers import UniverseTierPayload

    if MOMENTUM_JOB not in routines.jobs():
        return CheckResult("momentum_coverage", "ok", "not judged: universe.momentum disabled")
    try:
        row = conn.execute(
            "SELECT payload, valid_from FROM context_entries WHERE kind = 'universe_tier' "
            "AND subject = 'momentum' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return CheckResult("momentum_coverage", "ok", "not judged: no momentum entry yet")
    payload = UniverseTierPayload.model_validate_json(row[0])
    as_of = payload.source_as_of
    today = now.astimezone(ET).date()
    age = (today - as_of).days if as_of else None
    if age is not None and age <= stale_after_days:
        return CheckResult("momentum_coverage", "ok", f"SPMO holdings as of {as_of} ({age} d old)")
    seen = f"as of {as_of} ({age} d old)" if as_of else "with no as-of date"
    f = Finding(
        key=f"coverage:{MOMENTUM_JOB}",
        kind="coverage",
        severity="failed",
        message=(
            f"momentum tier stale: the latest SPMO holdings list ({payload.source}) is {seen}, "
            f"older than {stale_after_days} d; it is kept until a fresher list is fetched"
        ),
        detail={"job": MOMENTUM_JOB, "as_of": as_of.isoformat() if as_of else None},
    )
    return CheckResult("momentum_coverage", "failed", f"stale ({seen})", (f,))


SCOUT_JOB = "scout"


def scout_coverage(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: _dt.datetime
) -> CheckResult:
    """E13.7 (D56, owner decision 1): ``coverage:scout`` while the latest ``scout_read``
    wrote fewer discovery names than ``funnel.scout.min_discovery_alert``.

    Under-fill is accepted (discovery comes from the YouTube calls only) and measured:
    the condition clears on the next Scout run that meets the threshold. Not judged
    while the ``scout`` job is disabled or before the first read.
    """
    import json

    if SCOUT_JOB not in routines.jobs():
        return CheckResult("scout_coverage", "ok", "not judged: scout job disabled")
    try:
        row = conn.execute(
            "SELECT payload FROM context_entries WHERE kind = 'scout_read' "
            "ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return CheckResult("scout_coverage", "ok", "not judged: no scout_read yet")
    payload = json.loads(row[0])
    fill = int(payload.get("discovery_fill") or 0)
    need = routines.funnel.scout.min_discovery_alert
    cap = routines.funnel.scout.max_discovery
    session = payload.get("session")
    if fill >= need:
        return CheckResult("scout_coverage", "ok", f"discovery {fill}/{cap} ({session})")
    inp = payload.get("inputs") or {}
    briefs = " · ".join(
        f"{c.removeprefix('youtube_')} {(inp.get(c) or {}).get('present', 0)}/"
        f"{(inp.get(c) or {}).get('configured', 0)}"
        for c in ("youtube_macro", "youtube_micro")
    )
    f = Finding(
        key=f"coverage:{SCOUT_JOB}",
        kind="coverage",
        severity="failed",
        message=(
            f"discovery tier under-filled: the Scout's {session} read kept {fill}/{cap} "
            f"names, below {need} (briefs {briefs}; "
            f"{len(payload.get('screened_out') or {})} screened out)"
        ),
        detail={"job": SCOUT_JOB, "fill": fill, "min": need, "session": session},
    )
    return CheckResult("scout_coverage", "failed", f"discovery {fill}/{cap} < {need}", (f,))


IV_RECORD_JOB = "iv.record"
IV_CROSSCHECK = "iv_crosscheck"


def iv_crosscheck(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: _dt.datetime
) -> CheckResult:
    """E4.12 (D55): ``iv_crosscheck`` while the latest ``alpaca_cm30`` day has a row whose
    30-DTE IV differs from Cboe's ``iv30`` by more than ``iv_crosscheck_max_pts``.

    ``iv.record`` flags the row (``detail.breach``); the alert resolves when the next
    recorded day is back within the threshold. Context data only: nothing is halted.
    """
    import json

    if IV_RECORD_JOB not in routines.jobs():
        return CheckResult(IV_CROSSCHECK, "ok", "not judged: iv.record disabled")
    try:
        row = conn.execute("SELECT MAX(day) FROM iv_daily WHERE source = 'alpaca_cm30'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or row[0] is None:
        return CheckResult(IV_CROSSCHECK, "ok", "not judged: no recorded IV yet")
    day = str(row[0])
    rows = conn.execute(
        "SELECT ticker, iv30, detail FROM iv_daily WHERE source = 'alpaca_cm30' AND day = ?",
        (day,),
    ).fetchall()
    checked: list[tuple[str, float, float, float]] = []
    for t, iv, raw in rows:
        d = json.loads(raw or "{}")
        if d.get("cboe_iv30") is None:
            continue
        checked.append(
            (str(t), float(iv), float(d["cboe_iv30"]), float(d.get("crosscheck_max_pts") or 0))
        )
    bad = [c for c in checked if c[3] > 0 and abs(c[1] - c[2]) * 100 > c[3]]
    summary = f"{day}: {len(checked)} names vs Cboe iv30" + (
        f", {len(bad)} beyond threshold" if bad else ", all within threshold"
    )
    if not bad:
        return CheckResult(IV_CROSSCHECK, "ok", summary)
    worst = ", ".join(
        f"{t} ours {a * 100:.1f} vs Cboe {b * 100:.1f} ({(a - b) * 100:+.1f} pts)"
        for t, a, b, _ in sorted(bad, key=lambda c: -abs(c[1] - c[2]))[:4]
    )
    f = Finding(
        key=IV_CROSSCHECK,
        kind="iv_crosscheck",
        severity="degraded",
        message=(
            f"IV cross-check on {day}: {worst}; limit {bad[0][3]:g} vol pts. IV rank in "
            "the regime context may be off (context only, never a gate input)"
        ),
        detail={"day": day, "tickers": [c[0] for c in bad]},
    )
    return CheckResult(IV_CROSSCHECK, "degraded", summary, (f,))


def stranded_events(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: _dt.datetime
) -> CheckResult:
    """E6.2e: dispatched events whose run never started within ``tick.dispatch_grace``.

    The tick reclaims these itself; a finding here means the tick did not (it is
    not running, or the event keeps getting stranded). Posted once per event.
    """
    from arc.routines.runs import RoutineEventRepo

    grace = routines.tick.dispatch_grace
    findings: list[Finding] = []
    for ev in RoutineEventRepo(conn).stranded(dispatched_before=now - grace):
        at = ev.dispatched_at or ev.created_at  # always set: selected on dispatched_at
        minutes = int((now - at).total_seconds() // 60)
        phash = str(ev.payload.get("proposal_hash") or "")
        what = f" (proposal {phash[:12]})" if phash else ""
        findings.append(
            Finding(
                key=f"stranded:{ev.id}",
                kind="stranded_event",
                severity="failed",
                mode=ONE_OFF,
                message=(
                    f"{ev.name} event {ev.id}{what} dispatched {minutes} min ago by "
                    f"{ev.dispatched_by or '?'} but no run started "
                    f"(grace {int(grace.total_seconds() // 60)} min)"
                ),
                detail={
                    "event_id": ev.id,
                    "dispatched_at": at.isoformat(),
                    "dispatched_by": ev.dispatched_by,
                },
            )
        )
    return CheckResult(
        "stranded_events",
        "failed" if findings else "ok",
        f"{len(findings)} stranded",
        tuple(findings),
    )


def approvals_unposted(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: _dt.datetime
) -> CheckResult:
    """E6.1b: a pending approval request older than one tick whose card never posted.

    The tick's sweep re-posts such a card on every tick (``approvals.reposted``); a
    finding here means it keeps failing, so the owner has nothing to click and the
    proposal will expire at its TTL unseen. One condition, resolved once every
    pending request has a card (or expired).
    """
    older = now - routines.tick.interval
    rows = conn.execute(
        """SELECT proposal_hash, ticker, created_at, expires_at FROM approval_requests
           WHERE status = 'pending' AND message_ts IS NULL AND channel != 'log'
             AND created_at <= ? AND expires_at > ?
           ORDER BY created_at""",
        (to_db(older), to_db(now)),
    ).fetchall()
    if not rows:
        return CheckResult("approvals_unposted", "ok", "0 pending without a card")
    items = [
        f"{r['ticker']} {str(r['proposal_hash'])[:12]} "
        f"(since {from_db(r['created_at']):%H:%M %Z}, expires {from_db(r['expires_at']):%H:%M})"
        for r in rows
    ]
    f = Finding(
        key="approvals_unposted",
        kind="approvals_unposted",
        severity="failed",
        message=(
            f"{len(rows)} pending approval request(s) have no Slack card (post failed; "
            f"retried every tick): {', '.join(items)}"
        ),
        detail={"proposal_hashes": [str(r["proposal_hash"]) for r in rows]},
    )
    return CheckResult("approvals_unposted", "failed", f"{len(rows)} pending without a card", (f,))


# ---------------------------------------------------------------------------
# Remote access (E8.6)
# ---------------------------------------------------------------------------


def http_get(url: str, timeout: float) -> tuple[int, str]:
    """GET *url*: ``(status, body)``; status 0 + error text when unreachable. Never raises."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - http to our own host
            return int(resp.status), resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except (OSError, ValueError) as exc:
        return 0, str(exc)


def _port_open(host: str, port: int, timeout: float) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dashboard_problem(status: int, body: str) -> str | None:
    """Why ``/api/status`` is not an authenticated Hermes dashboard, or None when it is."""
    import json

    if status != 200:
        return f"no answer ({body[:120] or f'HTTP {status}'})" if status == 0 else f"HTTP {status}"
    try:
        data = json.loads(body)
    except ValueError:
        return "/api/status did not return JSON"
    if not isinstance(data, dict):
        return "/api/status did not return a JSON object"
    if data.get("auth_required") is not True:
        return "answers WITHOUT authentication (auth_required is not true)"
    providers = data.get("auth_providers") or []
    if "basic" not in providers:
        return f"auth provider 'basic' not registered (providers: {providers})"
    return None


def _tower_problem(status: int, body: str) -> str | None:
    """Why ``/api/health`` is not a healthy tower, or None when it is."""
    import json

    if status != 200:
        return f"no answer ({body[:120]})" if status == 0 else f"HTTP {status}"
    try:
        data = json.loads(body)
    except ValueError:
        return "/api/health did not return JSON (not the Arc tower?)"
    if not isinstance(data, dict) or data.get("status") != "ok":
        return "/api/health status is not ok"
    return None


def remote_access(
    ra: RemoteAccessCheck,
    *,
    resolve: Callable[[], str] | None = None,
    get: Callable[[str, float], tuple[int, str]] = http_get,
    lan: Callable[[], list[str]] | None = None,
    probe: Callable[[str, int, float], bool] = _port_open,
) -> CheckResult:
    """Hermes dashboard + tower answer on the Tailscale IP, gated, and nowhere on the LAN.

    - ``remote_hermes``  ``GET /api/status`` on :1994 must answer with
      ``auth_required: true`` and ``basic`` in ``auth_providers``.
    - ``remote_tower``   ``GET /api/health`` on :4174 must answer 200 with
      ``status: ok`` (the tower opened the audit store read-only, D35).
    - ``remote_exposed`` either port accepting a connection on a LAN address.
    """
    from arc.tower.net import NoTailscaleAddressError, host_lan_addresses, resolve_bind_address

    timeout = ra.timeout.total_seconds()
    findings: list[Finding] = []
    try:
        address = (resolve or resolve_bind_address)()
    except NoTailscaleAddressError:
        address = None
    if address is None:
        why = "no Tailscale address on this host (is Tailscale installed and up?)"
        for key, what in (("remote_hermes", "Hermes dashboard"), ("remote_tower", "tower")):
            findings.append(
                Finding(key=key, kind=key, severity="failed", message=f"remote {what}: {why}")
            )
    else:
        dash = f"http://{address}:{ra.dashboard_port}/api/status"
        if problem := _dashboard_problem(*get(dash, timeout)):
            findings.append(
                Finding(
                    key="remote_hermes",
                    kind="remote_hermes",
                    severity="failed",
                    message=f"remote Hermes dashboard {address}:{ra.dashboard_port}: {problem}",
                    detail={"url": dash},
                )
            )
        tower = f"http://{address}:{ra.tower_port}/api/health"
        if why := _tower_problem(*get(tower, timeout)):
            findings.append(
                Finding(
                    key="remote_tower",
                    kind="remote_tower",
                    severity="failed",
                    message=f"remote tower {address}:{ra.tower_port}: {why}",
                    detail={"url": tower},
                )
            )
    exposed = [
        f"{host}:{port}"
        for host in (lan or host_lan_addresses)()
        for port in (ra.dashboard_port, ra.tower_port)
        if probe(host, port, timeout)
    ]
    if exposed:
        findings.append(
            Finding(
                key="remote_exposed",
                kind="remote_exposed",
                severity="failed",
                message="remote access EXPOSED off the tailnet: answering on " + ", ".join(exposed),
                detail={"exposed": exposed},
            )
        )
    if findings:
        summary = "; ".join(f.key for f in findings)
        return CheckResult("remote_access", "failed", summary, tuple(findings))
    return CheckResult(
        "remote_access",
        "ok",
        f"dashboard :{ra.dashboard_port} gated (basic), tower :{ra.tower_port} up on {address}",
    )


# ---------------------------------------------------------------------------
# Hermes gateway
# ---------------------------------------------------------------------------


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run *argv*; ``(returncode, stdout+stderr)``. Never raises."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv from config, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def resolve_hermes(configured: str) -> str | None:
    """Absolute path of the ``hermes`` CLI (cron/launchd PATHs rarely include ~/.local/bin)."""
    if os.sep in configured:
        return configured if Path(configured).exists() else None
    found = shutil.which(configured)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / configured
    return str(local) if local.exists() else None


def parse_status(text: str) -> tuple[Severity, list[str], list[str]]:
    """Classify Hermes status output by its ✓/⚠/✗ markers: (severity, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("✗"):
            errors.append(line.lstrip("✗ ").strip())
        elif line.startswith("⚠"):
            warnings.append(line.lstrip("⚠ ").strip())
    if errors:
        return "failed", errors, warnings
    if warnings:
        return "degraded", errors, warnings
    return "ok", errors, warnings


def _first_ok(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("✓"):
            return line.lstrip("✓ ").strip()
    return ""


def gateway_health(
    gw: GatewayCheck, run: Callable[[list[str], float], tuple[int, str]] = run_command
) -> CheckResult:
    """``hermes gateway status`` (service/process) + ``hermes cron status`` (cron ticker)."""
    hermes = resolve_hermes(gw.hermes_bin)
    if hermes is None:
        f = Finding(
            key="gateway",
            kind="gateway_down",
            severity="failed",
            message=f"hermes CLI not found ({gw.hermes_bin!r}); cannot check the gateway",
        )
        return CheckResult("gateway", "failed", "hermes CLI not found", (f,))
    timeout = gw.timeout.total_seconds()
    severity: Severity = "ok"
    errors: list[str] = []
    warnings: list[str] = []
    oks: list[str] = []
    for argv in ([hermes, "gateway", "status"], [hermes, "cron", "status"]):
        code, out = run(argv, timeout)
        sev, errs, warns = parse_status(out)
        label = " ".join(argv[1:])
        if code != 0:
            errs = [f"`hermes {label}` exited {code}: {out.strip()[-200:]}", *errs]
            sev = "failed"
        errors += [f"{label}: {e}" for e in errs]
        warnings += [f"{label}: {w}" for w in warns]
        if ok := _first_ok(out):
            oks.append(ok)
        if sev == "failed" or (sev == "degraded" and severity == "ok"):
            severity = sev
    findings: list[Finding] = []
    if errors:
        findings.append(
            Finding(
                key="gateway",
                kind="gateway_down",
                severity="failed",
                message="Hermes gateway unhealthy: " + "; ".join(errors),
                detail={"errors": errors, "warnings": warnings},
            )
        )
    elif warnings:
        findings.append(
            Finding(
                key="gateway_degraded",
                kind="gateway_degraded",
                severity="degraded",
                message="Hermes gateway warnings: " + "; ".join(warnings),
                alert=gw.alert_on_degraded,
                detail={"warnings": warnings},
            )
        )
    summary = "; ".join(errors or warnings or oks) or severity
    return CheckResult("gateway", severity, summary, tuple(findings))
