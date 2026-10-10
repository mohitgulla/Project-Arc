"""``arc experiment start | pair | arms-tick`` (E10.2, PLAN D44).

* ``start <id>``: t0 of a registered experiment (:func:`arc.experiments.runner.start_arms`).
  Live: t0 equity is control's broker equity, and every arm's paper account must be
  flat (positions and open orders; the owner closes them in the Alpaca dashboard).
  ``--fixtures``: t0 equity from the fixture account (or ``--t0-equity``), no broker call.
* ``pair <chain>``: run each arm's copy of one control loop chain now. ``--fixtures``
  uses the canned personas and the recorded market, like ``arc propose --fixtures``.
* ``arms-tick``: what the routines tick spawns (detached) after control's tick:
  pair recent control chains, then each arm's own jobs. Holds its own lock, so two
  overlapping ticks never run the arms twice.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import argparse
    import sqlite3
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.experiments.runner import ArmAccount

__all__ = ["ARMS_TICK_LOCK", "add_arm_parsers", "run_arm_command", "spawn_arms_tick"]

ARMS_TICK_LOCK = "experiment-arms"


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def add_arm_parsers(esub: Any, common: Any, local_actor: str) -> None:
    sa = common(esub.add_parser("start", help="t0: create the arm stores, mark running (E10.2)"))
    sa.add_argument("experiment_id")
    sa.add_argument("--actor", default=local_actor)
    sa.add_argument(
        "--fixtures",
        action="store_true",
        help="Offline: t0 equity from the fixture account, no broker call (no arm keys needed)",
    )
    sa.add_argument("--t0-equity", default=None, help="With --fixtures: control's t0 equity ($)")
    sa.add_argument("--arm-dir", default=None, help="Put the arm stores in this dir (scratch)")
    sa.add_argument("--aa-override", action="store_true", help="Owner: ab start with no A/A")
    sa.add_argument("--now", default=None, help="t0 as this ISO time (default: now)")
    sa.add_argument(
        "--experiments-config",
        default=None,
        help="experiments.yaml for the runner arms (default: config/experiments.yaml); "
        "e.g. a scratch copy with account_mode: shared (D69)",
    )
    sa.add_argument(
        "--dry-run",
        action="store_true",
        help="E13.12: print each arm's plan (fork step, own personas, shared kinds); "
        "no store, no broker call, nothing written",
    )
    sa.add_argument("--config", default=None, help="routines.yaml (default: config/routines.yaml)")
    pr = common(esub.add_parser("pair", help="Run each arm's copy of one control chain (E10.2)"))
    pr.add_argument("chain_run_id", help="The control loop chain to pair")
    pr.add_argument(
        "--fixtures",
        action="store_true",
        help="Offline: canned personas + recorded market (as `arc propose --fixtures`)",
    )
    pr.add_argument("--fixture-set", default="neutral", help="Canned persona replies (--fixtures)")
    pr.add_argument("--profile", default=None, help="Account profile override (--fixtures)")
    pr.add_argument("--now", default=None, help="Pair as of this ISO time (default: now)")
    pr.add_argument("--config", default=None, help="routines.yaml (default: config/routines.yaml)")
    at = common(esub.add_parser("arms-tick", help="Pair recent control chains + arm jobs (E10.2)"))
    at.add_argument("--config", default=None, help="routines.yaml (default: config/routines.yaml)")
    at.add_argument("--lock-dir", default="data/locks")
    at.add_argument("--now", default=None)
    at.add_argument("--no-slack", action="store_true", help="No [Ops] notice (D69 account guard)")
    at.add_argument(
        "--dry-run",
        action="store_true",
        help="E13.12: each arm's plan + the jobs its tick would run at --now; writes nothing",
    )
    at.add_argument(
        "--experiment",
        default=None,
        help="With --dry-run: preview this (draft/registered) experiment's arms instead",
    )
    at.add_argument("--since", default=None, help="With --dry-run: tick window start (ISO)")
    rl = common(
        esub.add_parser(
            "repair-ledger",
            help="E10.2c: net out double-booked fills in the arms' virtual ledgers (append-only)",
        )
    )
    rl.add_argument("experiment_id")
    rl.add_argument(
        "--arm-db",
        action="append",
        default=None,
        help="Arm store to repair (repeatable; default: the stores control recorded at t0)",
    )
    rl.add_argument("--dry-run", action="store_true", help="Print the repair; write nothing")
    rl.add_argument("--now", default=None, help="Re-evaluate as of this ISO time (default: now)")


def _parse_now(text: str | None, default: _dt.datetime | None = None) -> _dt.datetime:
    from arc.utils.calendar import ET, now_et

    if text is None:
        return default if default is not None else now_et()
    t = _dt.datetime.fromisoformat(text)
    return t.replace(tzinfo=ET) if t.tzinfo is None else t.astimezone(ET)


def _preview(args: argparse.Namespace, conn: sqlite3.Connection, experiment_id: str | None) -> int:
    from arc.experiments.runner import arms_preview

    now = _parse_now(args.now)
    since = _parse_now(args.since) if getattr(args, "since", None) else None
    report = arms_preview(conn, experiment_id, routines_path=args.config, now=now, since=since)
    if args.json:
        _out(json.dumps(report, indent=2, default=str))
        return 0
    if "skipped" in report:
        _out(f"dry-run: {report['skipped']}")
        return 0
    _out(
        f"dry-run · {report['experiment_id']} ({report['status']}) · plans "
        f"{report['plans_from']} · nothing written"
    )
    for name, arm in report["arms"].items():
        plan = arm["plan"]
        _out(f"arm {name} (spec arm {arm['spec_arm']}):")
        _out(f"  fork step:     {plan['fork_step']}")
        personas = ", ".join(plan["arm_personas"]) or "none (control's Scout / Scalp)"
        _out(f"  arm personas:  {personas}")
        _out(f"  arm jobs:      {', '.join(plan['arm_jobs'])}")
        _out(f"  shared kinds:  {', '.join(plan['shared_kinds'])}")
        for line in arm["tick"]:
            _out(f"  {line}")
    return 0


def _start(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from arc.experiments.runner import ArmAccount, live_account_probe, runner_config, start_arms

    if args.dry_run:
        return _preview(args, conn, args.experiment_id)
    now = _parse_now(args.now)
    if args.fixtures:
        from arc.pipeline.env import fixture_account

        fx = fixture_account()
        try:
            t0_equity = Decimal(args.t0_equity) if args.t0_equity else fx.equity
        except InvalidOperation:
            _err(f"arc experiment start: --t0-equity {args.t0_equity!r} is not a number")
            return 2
        # D69: every fixture arm account is the (flat) fixture paper account
        flat = ArmAccount(
            account_number=fx.account_id, equity=fx.equity, positions=0, open_orders=0
        )
        probe = cast("Callable[[str], ArmAccount]", lambda _keys: flat)
    else:
        if args.t0_equity:
            _err("arc experiment start: --t0-equity is only for --fixtures (live reads control)")
            return 2
        from arc.experiments.broker import trading_broker

        t0_equity = trading_broker(conn).account().equity
        probe = live_account_probe()
    st = start_arms(
        conn,
        args.experiment_id,
        actor=args.actor,
        now=now,
        t0_equity=t0_equity,
        runner=runner_config(conn, args.experiments_config),
        arm_dir=Path(args.arm_dir).resolve() if args.arm_dir else None,
        aa_override=args.aa_override,
        routines_path=args.config,
        probe=probe,
    )
    from arc.experiments.runner import arm_stores

    payload = {
        "experiment_id": st.experiment_id,
        "status": st.status.value,
        "t0": None if st.running is None else st.running.t0.isoformat(),
        "t0_equity": str(t0_equity),
        "legacy_book": [] if st.running is None else st.running.legacy_book,
        "arms": {n: str(p) for n, p in arm_stores(conn, st.experiment_id).items()},
        "plans": {}
        if st.running is None
        else {n: pl.model_dump(mode="json") for n, pl in st.running.arm_plans.items()},
    }
    if args.json:
        _out(json.dumps(payload, indent=2))
    else:
        _out(
            f"{st.experiment_id} running from {payload['t0']} at t0 equity ${t0_equity} "
            f"({len(payload['legacy_book'])} legacy structure(s))"
        )
        for n, p in payload["arms"].items():
            plan = payload["plans"].get(n) or {}
            personas = ", ".join(plan.get("arm_personas") or []) or "none"
            _out(f"  arm {n}: {p} (fork {plan.get('fork_step')}; own personas: {personas})")
    return 0


def _fixture_arm_settings(arm: sqlite3.Connection, profile: str | None) -> ArcSettings:
    from arc.control.effective import effective_settings

    s = effective_settings(arm)
    return s.with_profile(profile) if profile else s


def _pair(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from arc.control.effective import effective_routines
    from arc.experiments.arms import read_identity
    from arc.experiments.runner import _connect, arm_stores, pair_chain, runner_config
    from arc.experiments.tape import running_experiments

    # D69: every running experiment's arms (keyed by arm id)
    stores = {
        f"{st.experiment_id}:{name}": path
        for st in running_experiments(conn)
        for name, path in arm_stores(conn, st.experiment_id).items()
    }
    if not stores:
        _err("arc experiment pair: no arm stores (run `arc experiment start` first)")
        return 2
    runner = runner_config(conn)
    results: list[dict[str, Any]] = []
    for name, path in stores.items():
        arm = _connect(path)
        try:
            ident = read_identity(arm)
            if ident is None:
                results.append({"arm": name, "status": "failed", "reason": "no arm_identity"})
                continue
            routines = effective_routines(arm, args.config)
            kwargs: dict[str, Any] = {}
            if args.fixtures:
                from arc.experiments.broker import virtual_broker
                from arc.pipeline.env import FIXTURE_NOW, FIXTURE_SETS, PipelineEnv
                from arc.pipeline.steps import pipeline_handlers

                now = _parse_now(args.now, FIXTURE_NOW)
                settings = _fixture_arm_settings(arm, args.profile)
                env = PipelineEnv.fixtures(FIXTURE_SETS[args.fixture_set])
                env.positions = list

                def _bound(
                    name: str,
                    env: PipelineEnv = env,
                    ident: Any = ident,
                    t: Any = now,
                    settings: Any = settings,
                ) -> Any:
                    # D63: a parallel branch runs on its own connection, so the arm's
                    # virtual account is built on the step's connection, per step.
                    def run(ctx: Any) -> Any:
                        vb = virtual_broker(
                            ctx.conn,
                            ident,
                            cast("BrokerAdapter", _FixtureBroker()),
                            settings=settings,
                            now=lambda: t,
                        )
                        step_env = dataclasses.replace(env, account=vb.account)
                        return pipeline_handlers(step_env)[name](ctx)

                    return run

                kwargs = {
                    "handlers": {name: _bound(name) for name in pipeline_handlers(env)},
                    "settings_factory": lambda s=settings: s,
                    "check_lag": False,
                }
            else:
                now = _parse_now(args.now)
            res = pair_chain(
                conn, arm, args.chain_run_id, routines=routines, runner=runner, now=now, **kwargs
            )
            results.append(res.as_json())
        finally:
            arm.close()
    if args.json:
        _out(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            _out(
                f"arm {r['arm']}: {r['status']} fork={r.get('fork_step')} "
                f"chain={r.get('arm_chain_run_id')} ({r.get('reason', '')})"
            )
            for job, status in r.get("steps", []):
                _out(f"  {job}: {status}")
    return 0 if all(r["status"] in ("ok", "duplicate") for r in results) else 1


class _FixtureBroker:
    """The fixture account as the arm's paper broker (offline; never trades)."""

    def account(self) -> Any:
        from arc.pipeline.env import fixture_account

        return fixture_account()

    def positions(self) -> list[Any]:
        return []

    def fills(self, since: _dt.datetime) -> list[Any]:
        _ = since
        return []


def _arms_tick(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from arc.experiments.runner import arms_tick
    from arc.routines.locks import LockBusyError, LockManager
    from arc.routines.spawn import spawn_detached
    from arc.utils.calendar import now_et

    if args.dry_run:
        return _preview(args, conn, args.experiment)
    if args.experiment or args.since:
        _err("arc experiment arms-tick: --experiment / --since are only for --dry-run")
        return 2
    now = _parse_now(args.now)
    lock_dir = Path(args.lock_dir)
    report: dict[str, Any]
    from arc.experiments.runner import live_account_number
    from arc.monitoring.alerts import LogOpsNotifier, OpsNotifier, SlackOpsNotifier
    from arc.routines.config import load_routines

    # D69: an arm whose keys reach another account than at t0 is halted + [Ops] notice
    notifier: OpsNotifier = (
        LogOpsNotifier()
        if args.no_slack
        else SlackOpsNotifier(load_routines(args.config).monitoring.alert_channel)
    )
    try:
        with LockManager(lock_dir).hold(ARMS_TICK_LOCK):
            report = arms_tick(
                conn,
                routines_path=args.config,
                now=now,
                lock_dir=lock_dir,
                clock=None if args.now else now_et,
                spawner=spawn_detached,
                account_number=live_account_number(),
                notifier=notifier,
            )
    except LockBusyError as exc:
        report = {"skipped": f"another arms-tick is running ({exc})"}
    _out(
        json.dumps(report, indent=2, default=str) if args.json else json.dumps(report, default=str)
    )
    arms = report.get("arms") or {}
    failed = any("error" in a for a in arms.values())
    return 1 if failed else 0


def _repair_ledger(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """``repair-ledger`` (E10.2c): adjust rows + restated snapshots, then a fresh report."""
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.arms import read_identity
    from arc.experiments.evaluate import build_report, store_report
    from arc.experiments.paired import paired_view
    from arc.experiments.repair import repair_ledger
    from arc.experiments.store import ExperimentStore
    from arc.store.db import connect_ro

    store = ExperimentStore(conn)
    st = store.require(args.experiment_id)
    paths = (
        [Path(p).resolve() for p in args.arm_db]
        if args.arm_db
        else list(dict(sorted(_arm_stores(conn, st.experiment_id).items())).values())
    )
    if not paths:
        _err("arc experiment repair-ledger: no arm stores (pass --arm-db)")
        return 2
    payload: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            _err(f"arc experiment repair-ledger: arm store {path} not found")
            return 2
        from arc.experiments.runner import _connect

        arm = connect_ro(path) if args.dry_run else _connect(path)
        try:
            ident = read_identity(arm)
            if ident is None or ident.experiment_id != st.experiment_id:
                found = "no arm_identity" if ident is None else ident.experiment_id
                _err(f"arc experiment repair-ledger: {path} is not an arm of {st.experiment_id} "
                     f"({found})")  # fmt: skip
                return 2
            rep = repair_ledger(arm, ident.arm_id, dry_run=args.dry_run)
        finally:
            arm.close()
        lines = rep.lines()
        payload.append({"arm_store": str(path), "lines": lines})
        if not args.json:
            _out("\n".join(lines))
    if not args.dry_run and st.running is not None:
        from arc.experiments.cli import report_lines

        now = _parse_now(args.now)
        cfg = experiments_config(effective_settings(conn))
        with paired_view(conn, paths) as view:
            report = build_report(view, st, cfg, now=now, aa_sigma=store.aa_sigma())
        with conn:
            rid = store_report(conn, report, run_id="repair-ledger")
        payload.append({"report_id": rid, "series": [r.model_dump(mode="json")
                                                     for r in report.series]})  # fmt: skip
        if not args.json:
            _out(f"stored report #{rid} on the restated snapshots:")
            _out("\n".join(report_lines(report)))
    if args.json:
        _out(json.dumps(payload, indent=2, default=str))
    return 0


def _arm_stores(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Path]:
    from arc.experiments.arms import arm_stores

    return arm_stores(conn, experiment_id)


def run_arm_command(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from arc.experiments.runner import ArmStartError

    cmd = args.experiment_command
    try:
        if cmd == "start":
            return _start(args, conn)
        if cmd == "pair":
            return _pair(args, conn)
        if cmd == "repair-ledger":
            return _repair_ledger(args, conn)
        return _arms_tick(args, conn)
    except (ArmStartError, ValueError) as exc:
        _err(f"arc experiment {cmd}: {exc}")
        return 2


def spawn_arms_tick(conn: sqlite3.Connection, run_env: Any) -> int | None:
    """Spawn ``arc experiment arms-tick`` detached when an experiment is running.

    Called by the live routines tick after control's tick, so the arms never
    lengthen control's slot nor hold its locks. Returns the pid, or None.
    """
    from arc.experiments.runner import runner_config
    from arc.experiments.tape import running_experiment
    from arc.routines.spawn import arc_command, spawn_detached

    if running_experiment(conn) is None or not runner_config(conn).enabled:
        return None
    return spawn_detached(arc_command(run_env, ["experiment", "arms-tick"]))
