"""The experiment arm runner (E10.2, PLAN D44).

An arm is a second execution context of the unchanged trading loop: its own store
(``runner.arms.<arm>.db``), its own paper account (``keys_env``) through a virtual
account, and the spec arm's config overlay (:mod:`arc.control.effective` applies it
for every consumer). Three entry points:

* :func:`start_arms` (``arc experiment start``): t0. Creates each arm's fresh store
  with its ``arm_identity``, opens the virtual account at control's equity with the
  legacy book reserved, then marks the experiment ``running``.
* :func:`pair_chain`: the arm's copy of one control loop chain. Upstream of the fork
  step (the first step the arm's overlay changes, and never later than the
  account-dependent tail ``propose``) the arm reuses control's outputs verbatim
  (context entries, journal decisions, ``ok`` run rows); from the fork it runs the
  steps itself on control's market tape, under its own locks. Every arm manifest
  carries ``paired_chain_run_id`` and ``fork_step``.
* :func:`arms_tick` (``arc experiment arms-tick``, spawned detached by the routines
  tick so it never lengthens control's tick): pairs every recent control loop chain
  not paired yet (``max_lag_seconds``), then runs the arm's own ``arm_jobs``
  (position management, reconcile, Broker ladders) on its store.

Shared inputs: sources and the Scalp run once, in control. Before each paired
chain, the active context entries the chain reads are synced from control's store,
except the kinds the arm's own jobs produce (e.g. ``position_review``).
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import from_db, to_db
from arc.experiments.arms import (
    ArmIdentity,
    arm_stores,
    read_identity,
    write_identity,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from decimal import Decimal

    from arc.config import ArcSettings
    from arc.experiments.config import ArmRunner, RunnerConfig
    from arc.experiments.models import ExperimentState
    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Outcome
    from arc.routines.handlers import Handler

__all__ = [
    "ACCOUNT_STEPS",
    "STEP_TARGETS",
    "ArmStartError",
    "PairResult",
    "arm_routines",
    "arm_stores",
    "arms_tick",
    "fork_step",
    "pair_chain",
    "start_arms",
]

log = structlog.get_logger(__name__)

_QUANT_RISK_LOOP = "routines.personas.quant_risk_loop"

# Which overlay targets (config file stems) each loop step's behaviour depends on,
# from what the step reads through arc.control.effective (exit_config / cost_model /
# ranking_config / the profile spec / routines options). A step whose targets meet
# the arm's overlay is re-run by the arm, and so is everything after it.
STEP_TARGETS: dict[str, frozenset[str]] = {
    "research": frozenset({"account_profiles", "routines"}),
    # E13.17: exit cases read the exit policy + realloc settings; shadow only (no orders).
    "quant.exit": frozenset({"account_profiles", "exits", "routines"}),
    "quant.open": frozenset({"account_profiles", "exits", "costs"}),
    # E13.9: the quant_risk_loop flag changes Risk's prompt (verdicts), so an arm that
    # flips it forks at risk.open, not later: control's review carries no verdicts.
    "risk.open": frozenset({"account_profiles", _QUANT_RISK_LOOP}),
    "quant.revise": frozenset({"account_profiles", "exits", "costs", _QUANT_RISK_LOOP}),
    "quant.propose": frozenset({"account_profiles", "exits", "costs", "ranking"}),
    "broker.execute": frozenset(),
}
# Steps that size, gate or trade against the arm's own account: always the arm's.
ACCOUNT_STEPS: frozenset[str] = frozenset({"quant.propose", "broker.execute"})
# E13.9: routines overlay keys narrower than "routines" (a persona flag that only the
# open path reads); an overlay touching only these does not re-run Research.
_NARROW_ROUTINES_KEYS = {("personas", "quant_risk_loop"): _QUANT_RISK_LOOP}

_STATE_ARM = "experiment_arm:{arm}"  # control routine_state -> arm store path


class ArmStartError(RuntimeError):
    """t0 refused (arm store exists, keys missing, arm account not flat, ...)."""


@dataclass
class PairResult:
    arm: str
    arm_id: str
    control_chain_run_id: str
    status: str  # ok | skipped | failed | duplicate
    reason: str = ""
    arm_chain_run_id: str | None = None
    fork_step: str | None = None
    outcomes: list[Outcome] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "arm_id": self.arm_id,
            "control_chain_run_id": self.control_chain_run_id,
            "arm_chain_run_id": self.arm_chain_run_id,
            "fork_step": self.fork_step,
            "status": self.status,
            "reason": self.reason,
            "steps": [(o.job, o.status) for o in self.outcomes],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _connect(path: Path | str) -> sqlite3.Connection:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(path)
    migrate(conn)
    return conn


def _db_file(conn: sqlite3.Connection) -> Path | None:
    r = conn.execute("PRAGMA database_list").fetchone()
    return Path(r[2]).resolve() if r is not None and r[2] else None


def runner_config(conn: sqlite3.Connection) -> RunnerConfig:
    from arc.control.effective import effective_settings, experiments_config

    return experiments_config(effective_settings(conn)).runner


def fork_step(chain: list[str], overlay: Mapping[str, Any]) -> str:
    """The first chain step the arm runs itself (see the module doc).

    Pre-D56 step names (``quant``, ``risk``, ``propose``) resolve to the current ones.
    """
    from arc.routines.config import LEGACY_JOB_NAMES

    targets = _overlay_targets(overlay)
    chain = [LEGACY_JOB_NAMES.get(s, s) for s in chain]
    for step in chain:
        if step in ACCOUNT_STEPS or STEP_TARGETS.get(step, frozenset()) & targets:
            return step
        if step not in STEP_TARGETS:  # an unknown step: never assume it is unaffected
            return step
    return chain[-1]


def _overlay_targets(overlay: Mapping[str, Any]) -> set[str]:
    """The overlay's targets; a routines overlay of only narrow keys names those keys."""
    targets = {t for t, data in overlay.items() if data}
    routines = overlay.get("routines")
    if isinstance(routines, dict) and routines:
        paths = {
            (top, key)
            for top, sub in routines.items()
            for key in (sub if isinstance(sub, dict) else {None: None})
        }
        if paths and paths <= set(_NARROW_ROUTINES_KEYS):
            targets.discard("routines")
            targets |= {_NARROW_ROUTINES_KEYS[p] for p in paths}
    return targets


def arm_routines(routines: RoutinesConfig, arm_jobs: list[str]) -> RoutinesConfig:
    """*routines* with every scheduled job disabled except *arm_jobs* (the arm's tick)."""
    keep = set(arm_jobs)

    def only(specs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            n: s if (n in keep or not s.enabled) else s.model_copy(update={"enabled": False})
            for n, s in specs.items()
        }

    return routines.model_copy(
        update={"sources": only(routines.sources), "personas": only(routines.personas)}
    )


# ---------------------------------------------------------------------------
# t0
# ---------------------------------------------------------------------------


def start_arms(
    control: sqlite3.Connection,
    experiment_id: str,
    *,
    actor: str,
    now: _dt.datetime,
    t0_equity: Decimal,
    runner: RunnerConfig,
    arm_dir: Path | None = None,
    aa_override: bool = False,
    control_sha: str | None = None,
    check_flat: Callable[[ArmRunner], None] | None = None,
) -> ExperimentState:
    """t0 of a registered experiment: create every arm store, then mark it ``running``.

    *check_flat* (live) raises :class:`ArmStartError` when the arm's paper account
    holds positions or open orders; fixtures pass ``None``. A failure leaves no arm
    store behind and the experiment ``registered``.
    """
    from arc.experiments.models import ExperimentStatus, RunningDetail
    from arc.experiments.store import ExperimentStore
    from arc.experiments.virtual import legacy_reservations, open_account
    from arc.routines.manifest import config_hashes
    from arc.routines.runs import RoutineStateRepo

    store = ExperimentStore(control, now=lambda: now)
    st = store.require(experiment_id)
    if st.status is not ExperimentStatus.REGISTERED:
        msg = f"{experiment_id} is {st.status.value}; only a registered experiment starts"
        raise ArmStartError(msg)
    if not runner.arms:
        msg = "experiments.runner.arms is empty: no arm to run the treatment"
        raise ArmStartError(msg)
    control_db = _db_file(control)
    if control_db is None:
        msg = "the control store must be a file (arms read it by path)"
        raise ArmStartError(msg)
    legacy = legacy_reservations(control)
    paths: dict[str, Path] = {}
    for name, arm in runner.arms.items():
        p = Path(arm.db_path(experiment_id))
        if arm_dir is not None:
            p = arm_dir / p.name
        p = p.resolve()
        if p == control_db:
            msg = f"arm {name} store is the control store"
            raise ArmStartError(msg)
        if p.exists():
            msg = f"arm {name} store {p} already exists; every experiment starts fresh"
            raise ArmStartError(msg)
        if check_flat is not None:
            check_flat(arm)
        paths[name] = p
    created: list[Path] = []
    try:
        for name, arm in runner.arms.items():
            p = paths[name]
            p.parent.mkdir(parents=True, exist_ok=True)
            created.append(p)
            conn = _connect(p)
            try:
                aid = f"{experiment_id}:{name}"
                write_identity(
                    conn,
                    ArmIdentity(
                        arm_id=aid,
                        experiment_id=experiment_id,
                        arm=name,
                        spec_arm=arm.spec_arm,
                        keys_env=arm.keys_env,
                        control_db=str(control_db),
                        overlay=getattr(st.spec.arms, arm.spec_arm).overlay,
                        created_at=now,
                    ),
                )
                open_account(conn, aid, t0_equity=t0_equity, legacy=legacy, at=now)
            finally:
                conn.close()
        sha = control_sha
        if sha is None:
            from arc.routines.manifest import _git

            sha = _git()[0] or "unknown"
        detail = RunningDetail(
            t0=now,
            t0_equity=float(t0_equity),
            legacy_book=sorted(legacy),
            control_sha=sha if len(sha) >= 7 else sha.ljust(7, "0"),
            config_hashes=config_hashes(),
        )
        state = RoutineStateRepo(control)
        for name, p in paths.items():
            state.set(_STATE_ARM.format(arm=name), str(p), now=now)
        out = store.start(experiment_id, detail, actor=actor, aa_override=aa_override)
    except BaseException:
        for p in created:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{p}{suffix}").unlink(missing_ok=True)
        for name in paths:
            RoutineStateRepo(control).delete(_STATE_ARM.format(arm=name))
        raise
    log.info(
        "experiments.started",
        experiment_id=experiment_id,
        arms={n: str(p) for n, p in paths.items()},
        t0_equity=str(t0_equity),
        legacy=len(legacy),
    )
    return out


def live_flat_check(environ: Mapping[str, str] | None = None) -> Callable[[ArmRunner], None]:
    """The live t0 check: arm keys valid (never ALPACA/ALPACA_TEST) and the account flat."""

    def check(arm: ArmRunner) -> None:
        from arc.broker.alpaca_paper import AlpacaPaperBroker
        from arc.broker.registry import resolve_broker
        from arc.config import get_settings

        # the arm's own keys (arm_keys refuses ALPACA / ALPACA_TEST) via the registry
        broker = resolve_broker(get_settings(), keys_env=arm.keys_env, environ=environ)
        if not isinstance(broker, AlpacaPaperBroker):  # only alpaca/paper/rest constructs
            msg = f"arm t0 check needs the Alpaca paper broker, got {type(broker).__name__}"
            raise ArmStartError(msg)
        held = broker.positions()
        if held:
            msg = (
                f"{arm.keys_env} account holds {len(held)} position(s); close them in the "
                "Alpaca dashboard before t0 (the arm must start flat)"
            )
            raise ArmStartError(msg)
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        open_orders = broker._client.get_orders(  # noqa: SLF001 - read-only listing
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50)
        )
        if open_orders:
            msg = (
                f"{arm.keys_env} account has {len(open_orders)} open order(s); cancel them "
                "in the Alpaca dashboard before t0 (the arm must start flat)"
            )
            raise ArmStartError(msg)

    return check


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------


def _shared_kinds(routines: RoutinesConfig, chain: list[str], arm_jobs: list[str]) -> set[str]:
    reads: set[str] = set()
    for step in chain:
        spec = routines.step(step)[1]
        reads.update(spec.reads or [])
    own: set[str] = set()
    for job in arm_jobs:
        found = routines.job(job)
        if found is None:
            continue
        own.update(found[1].writes or [])
        for step in getattr(found[1], "chain", []) or []:
            own.update(routines.step(step)[1].writes or [])
    return reads - own


def _copy_entries(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    rows: list[sqlite3.Row],
    routines: RoutinesConfig,
    *,
    chain_run_id: str | None = None,
    run_ids: Mapping[str, str] | None = None,
) -> int:
    """Insert control's context rows into the arm store (same ids), superseding like a write."""
    from arc.context.store import Supersede

    n = 0
    with arm:
        for r in rows:
            if arm.execute("SELECT 1 FROM context_entries WHERE id = ?", (r["id"],)).fetchone():
                continue
            policy = routines.context_ttl.get(r["kind"])
            if policy is None or policy.supersede is Supersede.LATEST:
                arm.execute(
                    """UPDATE context_entries SET status = 'superseded'
                       WHERE kind = ? AND subject = ? AND status = 'active'""",
                    (r["kind"], r["subject"]),
                )
            run_id = (run_ids or {}).get(r["run_id"], r["run_id"])
            arm.execute(
                """INSERT INTO context_entries
                   (id, kind, subject, payload, schema_version, produced_by, run_id,
                    chain_run_id, created_at, valid_from, expires_at, supersedes_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active')""",
                (
                    r["id"],
                    r["kind"],
                    r["subject"],
                    r["payload"],
                    r["schema_version"],
                    r["produced_by"],
                    run_id,
                    chain_run_id or r["chain_run_id"],
                    r["created_at"],
                    r["valid_from"],
                    r["expires_at"],
                ),
            )
            n += 1
    _ = control
    return n


def sync_shared_context(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    routines: RoutinesConfig,
    chain: list[str],
    arm_jobs: list[str],
    *,
    as_of: _dt.datetime,
) -> int:
    """Copy control's active entries of the kinds the chain reads (not the arm's own)."""
    kinds = sorted(_shared_kinds(routines, chain, arm_jobs))
    if not kinds:
        return 0
    marks = ",".join("?" * len(kinds))
    rows = control.execute(
        f"""SELECT * FROM context_entries
            WHERE status = 'active' AND kind IN ({marks}) AND valid_from <= ?
            ORDER BY valid_from, created_at, rowid""",  # noqa: S608 - placeholders only
        (*kinds, to_db(as_of)),
    ).fetchall()
    return _copy_entries(control, arm, rows, routines)


def _reuse_upstream(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    routines: RoutinesConfig,
    control_rows: list[Any],
    upstream: list[str],
    *,
    arm_chain_run_id: str,
    now: _dt.datetime,
) -> dict[str, Any]:
    """Record control's upstream steps as ``ok`` arm runs and import what they wrote."""
    from arc.routines.runs import RoutineRunRepo, RunStatus

    runs = RoutineRunRepo(arm)
    from arc.routines.config import LEGACY_JOB_NAMES

    by_job = {LEGACY_JOB_NAMES.get(r.job, r.job): r for r in control_rows}
    reused: dict[str, Any] = {}
    root = control_rows[0]
    for index, job in enumerate(upstream):
        src = by_job[job]
        run = runs.claim(
            job=job,
            scheduled_for=root.scheduled_for,
            reason=root.reason if index == 0 else f"chain:{upstream[0]}",
            chain_run_id=arm_chain_run_id,
            step_index=index,
            status=RunStatus.OK,
            summary=f"paired: reused {src.run_id} from control ({src.summary or ''})"[:500],
            now=now,
        )
        if run is None:
            msg = f"arm already ran {job} for {root.scheduled_for.isoformat()}"
            raise _DuplicateError(msg)
        reused[job] = run
        entries = control.execute(
            "SELECT * FROM context_entries WHERE run_id = ? ORDER BY created_at, rowid",
            (src.run_id,),
        ).fetchall()
        _copy_entries(
            control,
            arm,
            entries,
            routines,
            chain_run_id=arm_chain_run_id,
            run_ids={src.run_id: run.run_id},
        )
        cols = [r[1] for r in control.execute("PRAGMA table_info(decisions)")]
        keep = [c for c in cols if c != "arm_id"]
        with arm:
            for d in control.execute(
                "SELECT * FROM decisions WHERE run_id = ? ORDER BY at, rowid", (src.run_id,)
            ):
                row = {c: d[c] for c in keep}
                row["chain_run_id"] = arm_chain_run_id
                row["run_id"] = run.run_id
                row["supersedes_id"] = None
                arm.execute(
                    f"INSERT OR IGNORE INTO decisions ({', '.join(keep)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' * len(keep))})",
                    tuple(row[c] for c in keep),
                )
    return reused


class _DuplicateError(RuntimeError):
    pass


def _record_pair(
    arm: sqlite3.Connection,
    res: PairResult,
    *,
    lag: float | None,
    now: _dt.datetime,
) -> None:
    with arm:
        arm.execute(
            """INSERT INTO arm_pairs (arm_id, control_chain_run_id, arm_chain_run_id, fork_step,
                                      status, reason, lag_seconds, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (arm_id, control_chain_run_id) DO UPDATE SET
                 arm_chain_run_id = excluded.arm_chain_run_id,
                 fork_step = excluded.fork_step, status = excluded.status,
                 reason = excluded.reason, lag_seconds = excluded.lag_seconds,
                 at = excluded.at""",
            (
                res.arm_id,
                res.control_chain_run_id,
                res.arm_chain_run_id,
                res.fork_step,
                "skipped" if res.status == "duplicate" else res.status,
                res.reason[:500],
                lag,
                to_db(now),
            ),
        )


def pair_chain(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    control_chain_run_id: str,
    *,
    routines: RoutinesConfig,
    runner: RunnerConfig,
    now: _dt.datetime,
    handlers: Mapping[str, Handler] | None = None,
    lock_dir: Path | None = None,
    settings_factory: Callable[[], ArcSettings] | None = None,
    clock: Callable[[], _dt.datetime] | None = None,
    run_env: Any = None,
    check_lag: bool = True,
) -> PairResult:
    """Run the arm's copy of control's chain *control_chain_run_id* (see the module doc)."""
    from arc.routines.config import LEGACY_JOB_NAMES
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.locks import LockManager, NullLocks
    from arc.routines.runs import RoutineRunRepo

    ident = read_identity(arm)
    if ident is None:
        msg = "pair_chain needs an arm store (no arm_identity)"
        raise ValueError(msg)
    rows = RoutineRunRepo(control).chain(control_chain_run_id)
    res = PairResult(ident.arm, ident.arm_id, control_chain_run_id, "skipped")
    if not rows:
        res.reason = "unknown control chain"
        return res
    root = rows[0]
    found = routines.job(root.job)
    if found is None or not found[1].chain:
        res.reason = f"{root.job} has no chain"
        return res
    chain = [LEGACY_JOB_NAMES.get(s, s) for s in (root.job, *found[1].chain)]
    done = arm.execute(
        "SELECT status FROM arm_pairs WHERE arm_id = ? AND control_chain_run_id = ?",
        (ident.arm_id, control_chain_run_id),
    ).fetchone()
    if done is not None and done[0] in ("ok", "running", "skipped"):
        res.status, res.reason = "duplicate", f"already {done[0]}"
        return res
    lag = (now - root.scheduled_for).total_seconds()
    if check_lag and lag > runner.max_lag_seconds:
        res.reason = f"control chain is {lag:.0f}s old (> max_lag_seconds {runner.max_lag_seconds})"
        _record_pair(arm, res, lag=lag, now=now)
        log.info("experiments.pair_skipped", **res.as_json())
        return res
    status = {LEGACY_JOB_NAMES.get(r.job, r.job): r.status.value for r in rows}
    fork = fork_step(chain, ident.overlay)
    upstream = chain[: chain.index(fork)]
    not_ok = [j for j in upstream if status.get(j) != "ok"]
    if not_ok:
        res.reason = f"control did not finish {not_ok[0]} ({status.get(not_ok[0], 'missing')})"
        _record_pair(arm, res, lag=lag, now=now)
        log.info("experiments.pair_skipped", **res.as_json())
        return res
    res.arm_chain_run_id = f"{control_chain_run_id}.{ident.arm}"
    res.fork_step = fork
    res.status = "running"
    _record_pair(arm, res, lag=lag, now=now)
    try:
        synced = sync_shared_context(
            control, arm, routines, chain, runner.arm_jobs, as_of=root.scheduled_for
        )
        reused = _reuse_upstream(
            control,
            arm,
            routines,
            rows,
            upstream,
            arm_chain_run_id=res.arm_chain_run_id,
            now=now,
        )
        disp = Dispatcher(
            arm,
            routines,
            handlers=handlers,
            locks=LockManager(lock_dir) if lock_dir is not None else NullLocks(),
            settings_factory=settings_factory,
            clock=clock,
            run_env=run_env,
        )
        res.outcomes = disp.run_paired(
            chain,
            root.scheduled_for,
            now=now,
            chain_run_id=res.arm_chain_run_id,
            reused=reused,
        )
        bad = [o for o in res.outcomes if o.status in ("failed", "deferred", "duplicate")]
        res.status = "failed" if bad else "ok"
        res.reason = (
            f"{bad[0].job}: {bad[0].status} {bad[0].summary}".strip()
            if bad
            else f"forked at {fork}; reused {len(upstream)} step(s), synced {synced} entries"
        )
    except _DuplicateError as exc:
        res.status, res.reason = "duplicate", str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded; the next tick pairs the next slot
        res.status, res.reason = "failed", f"{type(exc).__name__}: {exc}"
        log.exception("experiments.pair_failed", arm_id=ident.arm_id)
    _record_pair(arm, res, lag=lag, now=now)
    log.info("experiments.paired", **res.as_json())
    return res


# ---------------------------------------------------------------------------
# the arms' tick
# ---------------------------------------------------------------------------


def _recent_loop_chains(
    control: sqlite3.Connection, loop_job: str, *, since: _dt.datetime
) -> list[str]:
    from arc.journal.legacy import job_clause  # noqa: PLC0415 - D56: pre-rename 'director'

    clause, args = job_clause(control, loop_job)
    rows = control.execute(
        f"""SELECT chain_run_id FROM routine_runs
           WHERE {clause} AND step_index = 0 AND chain_run_id IS NOT NULL
             AND scheduled_for >= ? AND status IN ('ok', 'failed')
           ORDER BY scheduled_for""",  # noqa: S608 - placeholders only
        (*args, to_db(since)),
    ).fetchall()
    return [r[0] for r in rows]


def arms_tick(
    control: sqlite3.Connection,
    *,
    routines_path: str | None,
    now: _dt.datetime,
    lock_dir: Path | None,
    clock: Callable[[], _dt.datetime] | None = None,
    handlers: Mapping[str, Handler] | None = None,
    spawner: Any = None,
) -> dict[str, Any]:
    """Pair recent control loop chains, then run each arm's own jobs (see the module doc).

    Never raises for one arm's failure; the report says what each arm did.
    """
    from arc.approvals.cli import make_service
    from arc.control.effective import effective_routines
    from arc.experiments.tape import prune_tape, running_experiment
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.handlers import RunEnv
    from arc.routines.heartbeat import LogNotifier
    from arc.routines.locks import LockManager, NullLocks

    report: dict[str, Any] = {"now": now.isoformat(), "arms": {}}
    st = running_experiment(control)
    if st is None:
        report["skipped"] = "no running experiment"
        return report
    runner = runner_config(control)
    report["experiment_id"] = st.experiment_id
    if not runner.enabled:
        report["skipped"] = "experiments.runner.enabled is off"
        return report
    report["tape_pruned"] = prune_tape(control, keep_days=runner.tape_keep_days, now=now)
    since = max(
        now - _dt.timedelta(seconds=runner.max_lag_seconds),
        st.running.t0 if st.running is not None else now,
    )
    for name, path in arm_stores(control).items():
        out: dict[str, Any] = {"store": str(path)}
        report["arms"][name] = out
        if not path.is_file():
            out["error"] = "arm store missing"
            continue
        arm = _connect(path)
        try:
            ident = read_identity(arm)
            if ident is None or ident.experiment_id != st.experiment_id:
                out["error"] = "arm store belongs to another experiment"
                continue
            arm_lock = lock_dir / f"arm-{name}" if lock_dir is not None else None
            routines = effective_routines(arm, routines_path)
            env = RunEnv(
                db_path=str(path),
                config_path=routines_path,
                lock_dir=str(arm_lock) if arm_lock is not None else None,
                slack=False,
            )
            pairs = []
            for chain_id in _recent_loop_chains(control, routines.loop.job, since=since):
                pairs.append(
                    pair_chain(
                        control,
                        arm,
                        chain_id,
                        routines=routines,
                        runner=runner,
                        now=now,
                        handlers=handlers,
                        lock_dir=arm_lock,
                        clock=clock,
                        run_env=env,
                    ).as_json()
                )
            out["pairs"] = pairs
            disp = Dispatcher(
                arm,
                arm_routines(routines, runner.arm_jobs),
                handlers=handlers,
                locks=LockManager(arm_lock) if arm_lock is not None else NullLocks(),
                notifier=LogNotifier(),
                clock=clock,
                run_env=env,
                spawner=spawner,
            )
            tick = disp.tick(now)
            out["jobs"] = [(o.job, o.status) for o in tick.outcomes]
            from arc.control.effective import effective_settings

            out["expired_approvals"] = len(
                make_service(arm, effective_settings(arm), slack=False).expire_due(now)
            )
        except Exception as exc:  # noqa: BLE001 - one arm's failure never stops another
            out["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("experiments.arm_tick_failed", arm=name)
        finally:
            arm.close()
    log.info("experiments.arms_tick", **{k: v for k, v in report.items() if k != "arms"})
    return report


def arm_t0_iso(conn: sqlite3.Connection) -> str | None:
    """The arm store's t0 (``open`` ledger row) as ISO text, for reports."""
    r = conn.execute("SELECT at FROM virtual_ledger WHERE kind = 'open' LIMIT 1").fetchone()
    return from_db(r[0]).isoformat() if r is not None else None


def _dump(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=2)
