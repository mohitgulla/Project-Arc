"""``arc health check|status|trace`` (E8.2).

- ``check``   run every check, record a ``health`` heartbeat, open/resolve ops
              alerts and post new/resolved ones to Slack. Exit 1 while any
              check is failing. Meant to run unattended (launchd, see
              ``hermes/monitoring/``), so it does not depend on the Hermes
              gateway it is checking.
- ``status``  last tick/health heartbeats and open alerts (read-only).
- ``trace``   everything recorded for one id (tick_id, run_id, chain_run_id,
              alert id, Slack ts, kanban task): heartbeats, alerts, routine runs
              and matching lines of the JSON log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import argparse
    import datetime as _dt
    import sqlite3

    from arc.monitoring.checks import CheckResult
    from arc.monitoring.config import MonitoringSettings

log = structlog.get_logger(__name__)


def add_health_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("health", help="Ops monitoring: heartbeats, gateway, missed windows (E8.2)")
    hsub = p.add_subparsers(dest="health_command", required=True)

    c = hsub.add_parser("check", help="Run all checks, record a heartbeat, alert on problems")
    _common(c)
    c.add_argument("--now", default=None, help="ISO time (default: now ET)")
    c.add_argument("--no-slack", action="store_true", help="Alerts to the log only")
    c.add_argument("--no-gateway", action="store_true", help="Skip the Hermes gateway check")
    c.add_argument("--json", action="store_true")

    s = hsub.add_parser("status", help="Last heartbeats and open alerts")
    _common(s)
    s.add_argument("--json", action="store_true")

    t = hsub.add_parser("trace", help="Everything recorded for one id (tick/run/chain/alert/ts)")
    _common(t)
    t.add_argument("id")
    t.add_argument("--json", action="store_true")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="routines YAML (default: config/routines.yaml)")
    p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def db_dir(db: str | None) -> Path:
    """Directory holding the DB: where relative log paths resolve."""
    from arc.store.db import DEFAULT_DB_PATH

    return Path(db).resolve().parent if db else DEFAULT_DB_PATH.parent


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def _conn(args: argparse.Namespace) -> sqlite3.Connection:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(args.db)
    migrate(conn)
    return conn


def _settings(args: argparse.Namespace) -> MonitoringSettings:
    from arc.routines.config import load_routines

    return load_routines(args.config).monitoring


def run_checks(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    now: _dt.datetime,
    *,
    gateway_runner: Any = None,
) -> list[CheckResult]:
    from arc.monitoring import checks
    from arc.routines.config import load_routines

    routines = load_routines(args.config)
    ms = routines.monitoring
    results = [
        checks.tick_staleness(conn, ms, now),
        checks.missed_windows(conn, routines, ms, now),
        checks.stuck_runs(conn, ms, now),
    ]
    if ms.gateway.enabled and not args.no_gateway:
        if gateway_runner is None:
            results.append(checks.gateway_health(ms.gateway))
        else:
            results.append(checks.gateway_health(ms.gateway, gateway_runner))
    return results


def _check(args: argparse.Namespace) -> int:
    from arc.monitoring import alerts, correlation
    from arc.monitoring.alerts import LogOpsNotifier, OpsNotifier, SlackOpsNotifier
    from arc.monitoring.logs import configure
    from arc.monitoring.store import HeartbeatRepo
    from arc.routines.cli import _parse_now

    ms = _settings(args)
    configure(ms.log, base_dir=db_dir(args.db))
    now = _parse_now(args.now)
    corr = correlation.from_env()
    corr.setdefault("check_id", correlation.new_tick_id().replace("tick-", "health-"))
    with correlation.bind(**corr):
        conn = _conn(args)
        results = run_checks(conn, args, now)
        notifier: OpsNotifier = (
            LogOpsNotifier() if args.no_slack else SlackOpsNotifier(ms.alert_channel)
        )
        outcome = alerts.apply(conn, results, now=now, correlation=corr, notifier=notifier)
        worst = _worst(results)
        HeartbeatRepo(conn).record(
            "health",
            worst,
            at=now,
            correlation=corr,
            detail={
                "checks": {r.name: {"severity": r.severity, "summary": r.summary} for r in results},
                "opened": [a.id for a in outcome.opened],
                "resolved": [a.id for a in outcome.resolved],
                "posted_ts": outcome.posted_ts,
            },
        )
        log.info("health.checked", status=worst, opened=len(outcome.opened))
    if args.json:
        _write(
            json.dumps(
                {
                    "now": now.isoformat(),
                    "status": worst,
                    "correlation": corr,
                    "checks": [
                        {
                            "name": r.name,
                            "severity": r.severity,
                            "summary": r.summary,
                            "findings": [f.message for f in r.findings],
                        }
                        for r in results
                    ],
                    "opened": [a.model_dump(mode="json") for a in outcome.opened],
                    "resolved": [a.model_dump(mode="json") for a in outcome.resolved],
                    "posted_ts": outcome.posted_ts,
                },
                indent=2,
            )
        )
    else:
        _write(f"health @ {now:%Y-%m-%d %H:%M %Z}: {worst}  ({corr['check_id']})")
        for r in results:
            _write(f"  {r.name:<16} {r.severity:<9} {r.summary}")
        if outcome.text:
            _write(outcome.text)
    return 1 if worst == "failed" else 0


def _worst(results: list[CheckResult]) -> Any:
    order = {"ok": 0, "degraded": 1, "failed": 2}
    return max((r.severity for r in results), key=order.__getitem__, default="ok")


def _status(args: argparse.Namespace) -> int:
    from arc.monitoring.store import AlertRepo, HeartbeatRepo

    conn = _conn(args)
    hb = HeartbeatRepo(conn)
    tick, health = hb.latest("tick"), hb.latest("health")
    open_alerts = AlertRepo(conn).open_alerts()
    if args.json:
        _write(
            json.dumps(
                {
                    "tick": tick.model_dump(mode="json") if tick else None,
                    "health": health.model_dump(mode="json") if health else None,
                    "open_alerts": [a.model_dump(mode="json") for a in open_alerts],
                },
                indent=2,
            )
        )
        return 0
    for name, h in (("tick", tick), ("health", health)):
        if h is None:
            _write(f"{name:<7} never")
            continue
        ids = " ".join(f"{k}={v}" for k, v in sorted(h.correlation.items()))
        _write(f"{name:<7} {h.at:%Y-%m-%d %H:%M %Z} {h.status:<9} {ids}")
    _write(f"open alerts: {len(open_alerts)}")
    for a in open_alerts:
        _write(f"  {a.opened_at:%m-%d %H:%M} {a.kind:<16} {a.id} {a.message}")
    return 0


def _log_lines(path: Path, needle: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """JSON log lines (current + rotated files) mentioning *needle*, oldest first."""
    files = sorted(path.parent.glob(path.name + ".*"), reverse=True) + [path]
    out: list[dict[str, Any]] = []
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if needle in line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out[-limit:]


def _trace(args: argparse.Namespace) -> int:
    from arc.monitoring.store import AlertRepo, HeartbeatRepo
    from arc.routines.runs import RoutineRunRepo

    conn = _conn(args)
    needle = args.id
    beats = HeartbeatRepo(conn).find(needle)
    found_alerts = AlertRepo(conn).find(needle)
    # Runs: direct id match, or every run a matching tick heartbeat lists.
    run_ids = {needle}
    for b in beats:
        run_ids.update(
            str(o.get("run_id")) for o in b.detail.get("outcomes", []) if o.get("run_id")
        )
    runs_repo = RoutineRunRepo(conn)
    runs = [r for rid in sorted(run_ids) if (r := runs_repo.get(rid)) is not None]
    runs += [r for r in runs_repo.chain(needle) if r.run_id not in run_ids]
    log_path = _settings(args).log.path
    if not log_path.is_absolute():
        log_path = db_dir(args.db) / log_path
    lines = _log_lines(log_path, needle)
    if args.json:
        _write(
            json.dumps(
                {
                    "id": needle,
                    "heartbeats": [b.model_dump(mode="json") for b in beats],
                    "alerts": [a.model_dump(mode="json") for a in found_alerts],
                    "runs": [r.model_dump(mode="json") for r in runs],
                    "log": lines,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if (beats or found_alerts or runs or lines) else 1
    _write(f"trace {needle}")
    for b in beats:
        ids = " ".join(f"{k}={v}" for k, v in sorted(b.correlation.items()))
        _write(f"  heartbeat {b.at:%m-%d %H:%M:%S} {b.component:<6} {b.status:<8} {ids}")
    for r in runs:
        chain = f" {r.chain_run_id}#{r.step_index}" if r.chain_run_id else ""
        _write(
            f"  run       {r.scheduled_for:%m-%d %H:%M} {r.job:<18} {r.status.value:<8}"
            f" {r.run_id}{chain} {r.error or r.summary or ''}".rstrip()
        )
    for a in found_alerts:
        state = "resolved" if a.resolved_at else "OPEN"
        _write(f"  alert     {a.opened_at:%m-%d %H:%M} {a.kind:<16} {state:<8} {a.id} {a.message}")
    for line in lines:
        _write(f"  log       {line.get('ts', '')} {line.get('level', '')} {line.get('event', '')}")
    if not (beats or found_alerts or runs or lines):
        _write("  (nothing recorded for this id)")
        return 1
    return 0


def run_health(args: argparse.Namespace) -> int:
    from arc.monitoring.logs import configure

    cmd = args.health_command
    if cmd == "check":
        return _check(args)
    configure(None, base_dir=None)
    if cmd == "status":
        return _status(args)
    return _trace(args)
