"""Health checks (E8.2). Each returns :class:`Finding` objects; nothing here posts.

- :func:`missed_windows`  a scheduled routine slot whose catch-up window closed
  without the slot running (no attempt, or recorded as ``skipped`` because it
  was missed). Halt skips and "no handler yet" skips are intended, not misses;
  failures are already alerted by the dispatcher.
- :func:`tick_staleness`  no ``tick`` heartbeat for ``tick_stale_after``.
- :func:`stuck_runs`      a ``routine_runs`` row still ``running`` after ``stuck_after``.
- :func:`gateway_health`  ``hermes gateway status`` and ``hermes cron status``.

The watchdog only judges slots after the first tick heartbeat, so installing
monitoring never back-fills alerts for the time before routines ran.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from arc.context.ttl import from_db, to_db
from arc.monitoring.store import HeartbeatRepo
from arc.routines.schedule import catchup_deadline, slots_between

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.monitoring.config import GatewayCheck, MonitoringSettings
    from arc.routines.config import RoutinesConfig

Severity = Literal["ok", "degraded", "failed"]

# One-off findings (a missed slot happened) vs. conditions (open until they pass).
ONE_OFF = "one_off"
CONDITION = "condition"


@dataclass(frozen=True)
class Finding:
    key: str  # dedupe key, e.g. missed:director:2026-09-28T13:30:00.000000Z
    kind: str  # missed_window | tick_stale | stuck_run | gateway_down | gateway_degraded
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


# ---------------------------------------------------------------------------
# Routine windows
# ---------------------------------------------------------------------------


def _is_missed_row(row: sqlite3.Row) -> bool:
    return row["status"] == "skipped" and str(row["summary"] or "").startswith("missed")


def _slot_attempted(
    conn: sqlite3.Connection, job: str, slot: _dt.datetime, deadline: _dt.datetime
) -> str | None:
    """How (job, slot) was handled: its row's status, ``"missed"``, or ``None``.

    The dispatcher collapses several due slots into the latest one, so a slot
    with no row of its own counts as covered when a later slot of the same job
    was attempted inside this slot's catch-up window.
    """
    rows = conn.execute(
        """SELECT scheduled_for, status, summary FROM routine_runs
           WHERE job = ? AND step_index = 0 AND scheduled_for >= ? AND scheduled_for <= ?
           ORDER BY scheduled_for""",
        (job, to_db(slot), to_db(deadline)),
    ).fetchall()
    own = [r for r in rows if r["scheduled_for"] == to_db(slot)]
    if own and not _is_missed_row(own[0]):
        return str(own[0]["status"])
    if any(not _is_missed_row(r) for r in rows):
        return "collapsed"
    return "missed" if own or rows else None


def missed_windows(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    settings: MonitoringSettings,
    now: _dt.datetime,
) -> CheckResult:
    first = HeartbeatRepo(conn).first("tick")
    if first is None:
        return CheckResult("routine_windows", "ok", "not judged: no tick heartbeat yet")
    start = max(first.at, now - settings.miss_lookback)
    findings: list[Finding] = []
    judged = 0
    for name, (_kind, spec) in routines.jobs().items():
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
    rows = conn.execute(
        """SELECT run_id, job, chain_run_id, started_at FROM routine_runs
           WHERE status = 'running' AND started_at IS NOT NULL AND started_at < ?""",
        (to_db(now - settings.stuck_after),),
    ).fetchall()
    findings = tuple(
        Finding(
            key=f"stuck:{r['run_id']}",
            kind="stuck_run",
            severity="failed",
            message=(
                f"{r['job']} run {r['run_id']} still running since "
                f"{from_db(r['started_at']):%m-%d %H:%M %Z}"
            ),
            detail={"run_id": r["run_id"], "chain_run_id": r["chain_run_id"]},
        )
        for r in rows
    )
    return CheckResult(
        "stuck_runs", "failed" if findings else "ok", f"{len(findings)} stuck", findings
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
