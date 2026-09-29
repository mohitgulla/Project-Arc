"""``arc routines ...`` and ``arc context ...`` CLI (E5.4).

stdout carries the human/JSON report; structured logs go to stderr.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Dispatcher, Outcome, TickReport

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
    t.add_argument(
        "--step",
        default=None,
        help="Dry-run only: simulate one tick every STEP (e.g. 5m) from --since to --now",
    )
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
    r.add_argument(
        "--event",
        default=None,
        metavar="ID",
        help="D34: run the job for one queued routine_events row (own lock, not the LLM lock)",
    )
    r.add_argument("--chain-run-id", default=None, help="with --event: join this chain run")
    r.add_argument("--parent-run-id", default=None, help="with --event: the dispatching run")

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
    sc = csub.add_parser(
        "schemas", help="Committed JSON Schemas of every context kind (schemas/context/)"
    )
    mode = sc.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate schemas/context/*.json")
    mode.add_argument("--check", action="store_true", help="Exit 1 if any schema is stale")
    sc.add_argument("--dir", default=None, help="Registry dir (default: <repo>/schemas/context)")
    tr = csub.add_parser(
        "trace", help="Run manifests (D27): what a run or chain read, wrote and used"
    )
    tr.add_argument("id", help="run_id (run-...) or chain_run_id (chain-...)")
    tr.add_argument("--db", default=None)
    tr.add_argument("--json", action="store_true")


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
    from arc.routines.handlers import RunEnv
    from arc.routines.heartbeat import LogNotifier, Notifier, SlackDayThreadNotifier
    from arc.routines.locks import LockManager, NullLocks
    from arc.utils.calendar import now_et

    notifier: Notifier
    if dry or getattr(args, "no_slack", False):
        notifier = LogNotifier()
    else:
        notifier = SlackDayThreadNotifier(conn)
    locks = NullLocks() if dry else LockManager(args.lock_dir)
    # E5.2b: a real (un-pinned) run hands steps the wall clock, so data age and
    # proposal expiry are judged when the step runs, not when the tick started.
    # ``--now`` replays keep the frozen time for every step.
    clock = now_et if not dry and not getattr(args, "now", None) else None
    routines = _effective_load(args, conn)
    run_env = RunEnv(
        db_path=args.db,
        config_path=args.config,
        lock_dir=None if dry else str(getattr(args, "lock_dir", "") or "") or None,
        slack=not (dry or getattr(args, "no_slack", False)),
    )
    return _Dispatcher(conn, routines, locks=locks, notifier=notifier, clock=clock, run_env=run_env)


def _effective_load(args: argparse.Namespace, conn: sqlite3.Connection) -> RoutinesConfig:
    """routines.yaml with D26 routine overrides (enable/cadence) from *conn*."""
    from arc.control.effective import effective_routines

    return effective_routines(conn, args.config)


def _approval_sweep(
    args: argparse.Namespace, conn: sqlite3.Connection, now: _dt.datetime
) -> dict[str, list[str]] | None:
    """Post new proposal cards and expire overdue ones (E6.1). Never fails the tick.

    With ``--no-slack`` only the TTL is enforced; cards wait for a tick that can post.
    """
    from arc.approvals.cli import make_service
    from arc.approvals.service import SweepReport
    from arc.control import effective_settings

    try:
        no_slack = bool(getattr(args, "no_slack", False))
        svc = make_service(conn, effective_settings(conn), slack=not no_slack)
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


def _db_dir(args: argparse.Namespace) -> Path:
    from arc.monitoring.cli import db_dir

    return db_dir(args.db)


def _tick(
    args: argparse.Namespace,
    now: _dt.datetime,
    since: _dt.datetime | None,
    conn: sqlite3.Connection | None = None,
    correlation: dict[str, str] | None = None,
) -> int:
    if conn is None:
        conn = _conn(args, memory=args.dry_run and args.db is None)
    disp = _dispatcher(args, conn, dry=args.dry_run)
    report = disp.tick(now, dry_run=args.dry_run, since=since)
    approvals = None if args.dry_run else _approval_sweep(args, conn, report.now)
    if correlation is not None:
        _record_tick(conn, report, correlation)
    if args.json:
        payload: dict[str, object] = {
            "now": report.now.isoformat(),
            "dry_run": report.dry_run,
            "halted": report.halted,
            "expired": report.expired,
            "outcomes": [_outcome_json(o) for o in report.outcomes],
            "approvals": approvals,
        }
        if correlation is not None:
            payload["correlation"] = correlation
        _write(json.dumps(payload, indent=2))
    else:
        _write("\n".join(report.lines()))
        if approvals:
            _write(
                f"approvals: {len(approvals['published'])} card(s) posted, "
                f"{len(approvals['auto_approved'])} auto-approved, "
                f"{len(approvals['expired'])} expired"
            )
        if correlation is not None:
            _write(f"tick_id: {correlation['tick_id']}")
    return 1 if any(o.status == "failed" for o in report.outcomes) else 0


def _record_tick(conn: sqlite3.Connection, report: TickReport, correlation: dict[str, str]) -> None:
    """E8.2 liveness: one ``tick`` heartbeat per live tick (read by ``arc health check``)."""
    from collections import Counter

    from arc.monitoring.store import HeartbeatRepo

    counts = Counter(o.status for o in report.outcomes)
    HeartbeatRepo(conn).record(
        "tick",
        "failed" if counts.get("failed") else "ok",
        at=report.now,
        correlation=correlation,
        detail={
            "counts": dict(counts),
            "halted": report.halted,
            "expired": report.expired,
            "outcomes": [
                {"job": o.job, "status": o.status, "run_id": o.run_id}
                for o in report.outcomes
                if o.run_id
            ],
        },
    )


def _live_tick(args: argparse.Namespace, now: _dt.datetime, since: _dt.datetime | None) -> int:
    """A real tick: rotated JSON log, correlation ids bound, tick heartbeat recorded.

    A crash still records a ``failed`` heartbeat before propagating, so the
    watchdog sees "ticking but broken" rather than "not ticking".
    """
    from arc.monitoring import correlation as corr_mod
    from arc.monitoring.logs import configure
    from arc.monitoring.store import HeartbeatRepo

    configure(_load(args).monitoring.log, base_dir=_db_dir(args))
    ids = corr_mod.tick_correlation()
    with corr_mod.bind(**ids):
        conn = _conn(args)
        try:
            return _tick(args, now, since, conn=conn, correlation=ids)
        except Exception as exc:
            structlog.get_logger(__name__).exception("routines.tick_crashed")
            HeartbeatRepo(conn).record(
                "tick",
                "failed",
                at=now,
                correlation=ids,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise


def _simulate(args: argparse.Namespace, now: _dt.datetime, since: _dt.datetime | None) -> int:
    """``tick --dry-run --step 5m``: what each cron tick in ``(since, now]`` would run.

    Pure planning over an in-memory DB (nothing runs, nothing is written);
    each simulated tick's window is the previous simulated tick.
    """
    from arc.context.ttl import parse_duration

    if not args.dry_run:
        _write("error: --step is only valid with --dry-run")
        return 2
    try:
        step = parse_duration(args.step)
    except ValueError as exc:
        _write(f"error: --step: {exc}")
        return 2
    start = since or now - _dt.timedelta(days=1)
    disp = _dispatcher(args, _conn(args, memory=True), dry=True)
    ticks: list[dict[str, object]] = []
    prev, cur = start, start + step
    while cur <= now:
        report = disp.tick(cur, dry_run=True, since=prev)
        if report.outcomes:
            ticks.append(
                {"tick": cur.isoformat(), "outcomes": [_outcome_json(o) for o in report.outcomes]}
            )
            if not args.json:
                _write(f"tick {cur:%a %Y-%m-%d %H:%M %Z}")
                _write("\n".join(report.lines()[1:]))
        prev, cur = cur, cur + step
    if args.json:
        _write(json.dumps({"since": start.isoformat(), "now": now.isoformat(), "ticks": ticks}))
    else:
        _write(
            f"{len(ticks)} of the ticks every {args.step} in ({start:%a %Y-%m-%d %H:%M}"
            f" → {now:%a %H:%M %Z}] had work"
        )
    return 0


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
        if args.step:
            return _simulate(args, now, since)
        if args.dry_run:
            return _tick(args, now, since)
        return _live_tick(args, now, since)

    if cmd == "run":
        from arc.monitoring import correlation
        from arc.monitoring.logs import configure

        configure(_load(args).monitoring.log, base_dir=_db_dir(args))
        conn = _conn(args)
        disp = _dispatcher(args, conn)
        try:
            with correlation.bind(**correlation.from_env()):
                if args.event:
                    from arc.routines.runs import RoutineEventRepo

                    ev = RoutineEventRepo(conn).get(args.event)
                    if ev is None:
                        _write(f"error: unknown event {args.event!r}")
                        return 2
                    outcomes = disp.run_event(
                        args.job,
                        ev,
                        now=_parse_now(args.now),
                        chain_run_id=args.chain_run_id,
                        parent_run_id=args.parent_run_id,
                    )
                else:
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
    if getattr(args, "context_command", "show") == "schemas":
        return _run_schemas(args)
    conn = _conn(args)
    if getattr(args, "context_command", "show") == "trace":
        return _run_trace(conn, args)
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


def trace_runs(conn: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
    """One element per run (a chain in step order): contract, entries read and written,
    persona calls and the full run manifest (latest attempt). Raises LookupError."""
    from arc.context.store import ContextStore
    from arc.pipeline.store import PersonaCallRepo
    from arc.routines.manifest import ManifestRepo
    from arc.routines.runs import RoutineRunRepo

    rows = conn.execute(
        "SELECT run_id FROM routine_runs WHERE run_id = ? OR chain_run_id = ?"
        " ORDER BY step_index, scheduled_for, rowid",
        (ref, ref),
    ).fetchall()
    if not rows:
        msg = f"no routine run or chain {ref!r}"
        raise LookupError(msg)
    store, calls, manifests = ContextStore(conn), PersonaCallRepo(conn), ManifestRepo(conn)
    runs = RoutineRunRepo(conn)
    out: list[dict[str, Any]] = []
    for r in rows:
        run = runs.get(r["run_id"])
        assert run is not None
        m = manifests.latest(run.run_id)
        read: dict[str, dict[str, Any]] = {}
        for sid in run.inputs_snapshot:
            try:
                snap = store.load_snapshot(sid)
            except KeyError:
                continue
            for e in snap.entries:
                read.setdefault(
                    e.id,
                    {
                        "id": e.id,
                        "kind": e.kind,
                        "subject": e.subject,
                        "produced_by": e.produced_by,
                    },
                )
        wrote = [
            {"id": e.id, "kind": e.kind, "subject": e.subject}
            for e in (store.get(i) for i in run.outputs)
            if e is not None
        ]
        out.append(
            {
                "job": run.job,
                "run_id": run.run_id,
                "chain_run_id": run.chain_run_id,
                "step_index": run.step_index,
                "status": run.status.value,
                "declared": {
                    "reads": m.declared_reads if m else None,
                    "writes": m.declared_writes if m else None,
                },
                "read": list(read.values()),
                "wrote": wrote,
                "persona_calls": [
                    {
                        "id": c["id"],
                        "model": c["model"],
                        "status": c["status"],
                        "prompt_sha256": c["prompt_sha256"],
                    }
                    for c in calls.for_run(run.run_id)
                ],
                "manifest": m.model_dump(mode="json") if m else None,
            }
        )
    return out


def _run_trace(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    try:
        steps = trace_runs(conn, args.id)
    except LookupError as exc:
        _write(f"error: {exc.args[0]}")
        return 1
    if args.json:
        _write(json.dumps(steps, indent=2, default=str))
        return 0
    for st in steps:
        m = st["manifest"] or {}
        cfg = m.get("config_hashes", {}).get("routines.yaml", "?")[:8]
        tokens = f"{m.get('input_tokens')}/{m.get('output_tokens')}"
        served = ",".join(m.get("models_served") or []) or "-"
        _write(
            f"== {st['job']} #{st['step_index']} {st['status']} {m.get('duration_ms', '?')}ms"
            f" model={served} tokens={tokens} git={(m.get('git_sha') or '?')[:10]}"
            f"{'+dirty' if m.get('git_dirty') else ''} routines.yaml={cfg}"
            f" session={m.get('market_session', '?')} {st['run_id']}"
        )
        if m.get("error_class"):
            _write(f"   error: {m['error_class']}: {m.get('error')}")
        _write(f"   declared reads={st['declared']['reads']} writes={st['declared']['writes']}")
        for e in st["read"]:
            _write(
                f"   read  {e['kind']:<13} {e['subject']:<10} by={e['produced_by']:<9} {e['id']}"
            )
        for e in st["wrote"]:
            _write(f"   wrote {e['kind']:<13} {e['subject']:<10} {e['id']}")
        for c in st["persona_calls"]:
            _write(f"   call  {c['model']} {c['status']} sha={c['prompt_sha256'][:12]} {c['id']}")
        ext = ", ".join(x["name"] for x in m.get("external_inputs", [])) or "-"
        _write(f"   external: {ext}")
    return 0


def _run_schemas(args: argparse.Namespace) -> int:
    from pathlib import Path

    from arc.context.kinds import SCHEMA_DIR, render_schemas

    root = Path(args.dir) if args.dir else SCHEMA_DIR
    wanted = render_schemas()
    existing = {p.name for p in root.glob("*.json")} if root.is_dir() else set()
    stale = sorted(
        name
        for name, text in wanted.items()
        if not (root / name).is_file() or (root / name).read_text() != text
    )
    extra = sorted(existing - set(wanted))
    if args.check:
        for name in stale:
            _write(f"stale: {name}")
        for name in extra:
            _write(f"orphan: {name}")
        if stale or extra:
            _write("run `arc context schemas --write` (and bump schema_version on a change)")
            return 1
        _write(f"OK: {len(wanted)} context schemas up to date")
        return 0
    root.mkdir(parents=True, exist_ok=True)
    for name in stale:
        (root / name).write_text(wanted[name])
    for name in extra:
        (root / name).unlink()
    _write(f"wrote {len(stale)}, removed {len(extra)}, {len(wanted)} total in {root}")
    return 0
