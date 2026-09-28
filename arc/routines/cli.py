"""``arc routines ...`` and ``arc context ...`` CLI (E5.4).

stdout carries the human/JSON report; structured logs go to stderr.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Dispatcher, Outcome

DEFAULT_LOCK_DIR = Path("data") / "locks"


def _parse_now(text: str | None) -> _dt.datetime:
    from arc.utils.calendar import ET, now_et

    if not text:
        return now_et()
    dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _common(p: argparse.ArgumentParser, *, db: bool = True) -> None:
    p.add_argument("--config", default=None, help="routines YAML (default: config/routines.yaml)")
    if db:
        p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def add_routines_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("routines", help="Routine dispatcher: cadences, chains, triggers (D16)")
    rsub = p.add_subparsers(dest="routines_command", required=True)

    v = rsub.add_parser("validate", help="Validate config/routines.yaml")
    _common(v, db=False)

    ls = rsub.add_parser("list", help="Jobs with cadence and next due time (ET)")
    _common(ls, db=False)
    ls.add_argument("--now", default=None, help="ISO time (default: now ET)")

    t = rsub.add_parser("tick", help="Run everything due since the last tick")
    _common(t)
    t.add_argument("--dry-run", action="store_true", help="Print what would run; change nothing")
    t.add_argument("--now", default=None, help="ISO time, e.g. 2026-09-28T12:00-04:00")
    t.add_argument(
        "--since", default=None, help="Dry-run window start (default: last tick / 1 interval)"
    )
    t.add_argument("--json", action="store_true", help="JSON output")
    t.add_argument("--no-slack", action="store_true", help="Heartbeats to the log only")
    t.add_argument("--lock-dir", default=str(DEFAULT_LOCK_DIR))

    r = rsub.add_parser("run", help="Run one job now (resumes today's failed chain with --chain)")
    _common(r)
    r.add_argument("job")
    r.add_argument("--chain", action="store_true", help="Also run the job's chain")
    r.add_argument("--fresh", action="store_true", help="Start a new chain instead of resuming")
    r.add_argument("--now", default=None)
    r.add_argument("--no-slack", action="store_true")
    r.add_argument("--lock-dir", default=str(DEFAULT_LOCK_DIR))

    h = rsub.add_parser("history", help="Recent routine runs")
    _common(h)
    h.add_argument("--job", default=None)
    h.add_argument("--limit", type=int, default=30)

    e = rsub.add_parser("emit", help="Queue an event (approval, halt, ...) for the next tick")
    _common(e)
    e.add_argument("event")
    e.add_argument("--payload", default="{}", help="JSON object")


def add_context_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("context", help="Shared context store (D16)")
    csub = p.add_subparsers(dest="context_command", required=True)
    s = csub.add_parser("show", help="Active, unexpired context entries")
    s.add_argument("--db", default=None)
    s.add_argument("--kind", action="append", default=[], help="Filter by kind (repeatable)")
    s.add_argument("--subject", action="append", default=[], help="Filter by subject (repeatable)")
    s.add_argument("--as-of", default=None, help="ISO time (default: now ET)")
    s.add_argument("--snapshot", default=None, help="Show a recorded snapshot by id")
    s.add_argument("--json", action="store_true")


# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace) -> RoutinesConfig:
    from arc.routines.config import load_routines

    return load_routines(args.config)


def _conn(args: argparse.Namespace, *, memory: bool = False) -> sqlite3.Connection:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(":memory:" if memory else args.db)
    migrate(conn)
    return conn


def _dispatcher(
    args: argparse.Namespace, conn: sqlite3.Connection, *, dry: bool = False
) -> Dispatcher:
    from arc.routines.dispatcher import Dispatcher as _Dispatcher
    from arc.routines.heartbeat import LogNotifier, Notifier, SlackDayThreadNotifier
    from arc.routines.locks import LockManager, NullLocks

    notifier: Notifier
    if dry or getattr(args, "no_slack", False):
        notifier = LogNotifier()
    else:
        notifier = SlackDayThreadNotifier(conn)
    locks = NullLocks() if dry else LockManager(args.lock_dir)
    return _Dispatcher(conn, _load(args), locks=locks, notifier=notifier)


def _approval_sweep(
    args: argparse.Namespace, conn: sqlite3.Connection, now: _dt.datetime
) -> dict[str, list[str]] | None:
    """Post new proposal cards and expire overdue ones (E6.1). Never fails the tick.

    With ``--no-slack`` only the TTL is enforced; cards wait for a tick that can post.
    """
    from arc.approvals.cli import make_service
    from arc.approvals.service import SweepReport
    from arc.config import get_settings

    try:
        no_slack = bool(getattr(args, "no_slack", False))
        svc = make_service(conn, get_settings(), slack=not no_slack)
        if no_slack:
            return SweepReport([], [], svc.expire_due(now)).as_json()
        return svc.sweep(now).as_json()
    except Exception as exc:  # noqa: BLE001 - logged; the next tick retries
        structlog.get_logger(__name__).error("approvals.sweep_failed", error=str(exc))
        return None


def _outcome_json(o: Outcome) -> dict[str, object]:
    return {
        "job": o.job,
        "scheduled_for": o.scheduled_for.isoformat(),
        "status": o.status,
        "reason": o.reason,
        "run_id": o.run_id,
        "chain_run_id": o.chain_run_id,
        "step": o.step_index,
        "summary": o.summary,
    }


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    # Resolve sys.stderr at log time, not configure time, so a replaced/closed
    # stream (e.g. under test capture) is never written to.
    return structlog.PrintLogger(file=sys.stderr)


def _log_to_stderr() -> None:
    """stdout carries the report; structured logs go to stderr."""
    structlog.configure(logger_factory=_stderr_logger)


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def run_routines(args: argparse.Namespace) -> int:
    _log_to_stderr()
    cmd = args.routines_command

    if cmd == "validate":
        from pydantic import ValidationError

        try:
            cfg = _load(args)
        except (ValidationError, ValueError, OSError) as exc:
            _write(f"INVALID: {exc}")
            return 1
        jobs = cfg.jobs()
        _write(
            f"OK: {len(cfg.sources)} sources, {len(cfg.personas)} personas, "
            f"{len(cfg.all_triggers())} triggers, {len(jobs)} enabled jobs"
        )
        return 0

    if cmd == "list":
        from arc.routines.dispatcher import next_due

        cfg = _load(args)
        now = _parse_now(args.now)
        _write(f"now: {now:%a %Y-%m-%d %H:%M %Z}")
        for name, spec, nxt in next_due(cfg, now):
            kind = "source " if name in cfg.sources else "persona"
            when = f"{nxt:%a %Y-%m-%d %H:%M %Z}" if nxt else "-"
            extra = f"  chain→ {' → '.join(spec.chain)}" if spec.chain else ""
            _write(f"  {kind} {name:<20} next {when:<24} {spec.cadence}{extra}")
        for rule in cfg.all_triggers():
            cond = f" if {rule.condition}" if rule.condition else ""
            _write(f"  trigger on {rule.on}{cond} → run {rule.run}")
        return 0

    if cmd == "tick":
        now = _parse_now(args.now)
        since = _parse_now(args.since) if args.since else None
        conn = _conn(args, memory=args.dry_run and args.db is None)
        disp = _dispatcher(args, conn, dry=args.dry_run)
        report = disp.tick(now, dry_run=args.dry_run, since=since)
        approvals = None if args.dry_run else _approval_sweep(args, conn, report.now)
        if args.json:
            _write(
                json.dumps(
                    {
                        "now": report.now.isoformat(),
                        "dry_run": report.dry_run,
                        "halted": report.halted,
                        "expired": report.expired,
                        "outcomes": [_outcome_json(o) for o in report.outcomes],
                        "approvals": approvals,
                    },
                    indent=2,
                )
            )
        else:
            _write("\n".join(report.lines()))
            if approvals:
                _write(
                    f"approvals: {len(approvals['published'])} card(s) posted, "
                    f"{len(approvals['auto_approved'])} auto-approved, "
                    f"{len(approvals['expired'])} expired"
                )
        return 1 if any(o.status == "failed" for o in report.outcomes) else 0

    if cmd == "run":
        conn = _conn(args)
        disp = _dispatcher(args, conn)
        try:
            outcomes = disp.run_manual(
                args.job, now=_parse_now(args.now), chain=args.chain, fresh=args.fresh
            )
        except KeyError as exc:
            _write(f"error: {exc.args[0]}")
            return 2
        for o in outcomes:
            _write(json.dumps(_outcome_json(o)))
        return 1 if any(o.status == "failed" for o in outcomes) else 0

    if cmd == "history":
        from arc.routines.runs import RoutineRunRepo

        conn = _conn(args)
        for run in RoutineRunRepo(conn).history(job=args.job, limit=args.limit):
            step = f" step{run.step_index}" if run.chain_run_id else ""
            chain = f" {run.chain_run_id}{step}" if run.chain_run_id else ""
            detail = run.error or run.summary or ""
            _write(
                f"{run.scheduled_for:%Y-%m-%d %H:%M} {run.job:<20} {run.status.value:<8}"
                f" {run.reason:<22} {run.run_id}{chain} snap={','.join(run.inputs_snapshot) or '-'}"
                f" out={len(run.outputs)} {detail}".rstrip()
            )
        return 0

    if cmd == "emit":
        from arc.routines.runs import RoutineEventRepo

        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            _write("error: --payload must be a JSON object")
            return 2
        ev = RoutineEventRepo(_conn(args)).emit(args.event, payload)
        _write(json.dumps({"event_id": ev.id, "name": ev.name}))
        return 0

    return 2  # pragma: no cover - argparse enforces the choices


def run_context(args: argparse.Namespace) -> int:
    from arc.context.store import ContextStore

    _log_to_stderr()
    conn = _conn(args)
    store = ContextStore(conn)
    if args.snapshot:
        entries = store.load_snapshot(args.snapshot).entries
    else:
        entries = store.query(as_of=_parse_now(args.as_of), kinds=args.kind, subjects=args.subject)
    if args.json:
        _write(json.dumps([e.model_dump(mode="json") for e in entries], indent=2))
        return 0
    if not entries:
        _write("(no active context entries)")
    for e in entries:
        expires = f"{e.expires_at:%m-%d %H:%M}" if e.expires_at else "never"
        payload = json.dumps(e.payload, sort_keys=True)
        if len(payload) > 100:
            payload = payload[:97] + "..."
        _write(
            f"{e.valid_from:%Y-%m-%d %H:%M} {e.kind:<13} {e.subject:<18} by={e.produced_by:<18}"
            f" exp={expires:<11} {e.id} {payload}"
        )
    return 0
