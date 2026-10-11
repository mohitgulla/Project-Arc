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

Shared inputs: sources run once, in control, and so do the Scout and the Scalp
unless the arm owns them. Before each paired chain, the active context entries the
chain reads are synced from control's store, except the kinds the arm's own jobs
produce (e.g. ``position_review``) and the book kinds (:data:`BOOK_KINDS`).

E13.12 (D56): experiments fork at any persona. Each arm's :class:`ArmPlan` is
computed at t0 (:func:`arm_plan`) and stored on its identity: the loop's fork step
(any of ``research``, ``exits.mandatory``, ``quant.exit``, ``risk.exit``,
``quant.open``, ``risk.open``, ``quant.revise``, ``quant.propose``), and the
non-loop personas the arm runs on its own store (``arm_personas``: ``scout`` /
``scalp``, from the overlay's keys via :data:`PERSONA_OVERLAY_PREFIXES` or
``runner.arm_personas``). An arm-owned persona runs at control's slots in the arms'
tick, on control's synced inputs (briefs, options stats, raw docs), and its rows are
never synced from control; an arm that owns one forks its loop at ``research``.
Broker code is shared; each arm trades its own account (``keys_env``).

E10.2b (D75): Research reviews the book it sees, so an arm that holds any open
structure of its own forks each paired chain at ``research`` (:func:`book_fork`):
its own Research call writes its own ``exit_watchlist`` and its exit cases follow.
With an empty book the planned fork stands (A/A: reuse control's Research).
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import from_db, to_db
from arc.experiments.arms import (
    STATE_ARM,
    ArmAccountChangedError,
    ArmIdentity,
    account_fingerprint,
    arm_stores,
    read_identity,
    write_identity,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping

    from arc.config import ArcSettings
    from arc.experiments.config import ArmRunner, RunnerConfig
    from arc.experiments.models import ArmPlan, ExperimentState
    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Outcome
    from arc.routines.handlers import Handler

__all__ = [
    "ACCOUNT_CHANGED",
    "ACCOUNT_STEPS",
    "BOOK_KINDS",
    "PERSONA_JOBS",
    "PERSONA_OVERLAY_PREFIXES",
    "STEP_TARGETS",
    "ArmAccount",
    "ArmAccountChangedError",
    "ArmStartError",
    "PairResult",
    "arm_owned_personas",
    "arm_plan",
    "arm_routines",
    "arm_stores",
    "arms_preview",
    "arms_tick",
    "book_fork",
    "check_arm_account",
    "fork_step",
    "live_account_number",
    "live_account_probe",
    "live_arms",
    "pair_chain",
    "persona_jobs",
    "plan_of",
    "plans_at_start",
    "start_arms",
    "sync_persona_inputs",
]

log = structlog.get_logger(__name__)

_RESEARCH = "research"

# Which overlay targets (config file stems) each loop step's behaviour depends on,
# from what the step reads through arc.control.effective (exit_config / cost_model /
# ranking_config / the profile spec / routines options). A step whose targets meet
# the arm's overlay is re-run by the arm, and so is everything after it.
STEP_TARGETS: dict[str, frozenset[str]] = {
    # E13.12: routines = funnel.research and the research job settings; universe =
    # the market_reference regime reads and the pool's tier column.
    "research": frozenset({"account_profiles", "routines", "universe"}),
    # E13.17: exit cases read the exit policy + realloc settings (no orders).
    "quant.exit": frozenset({"account_profiles", "exits", "costs", "routines"}),
    # E13.18: the mandatory floor closes against the arm's own book (an ACCOUNT_STEP);
    # Risk's exit review reads the exit policy and the account profile.
    "exits.mandatory": frozenset({"exits"}),
    "risk.exit": frozenset({"account_profiles", "exits"}),
    "quant.open": frozenset({"account_profiles", "exits", "costs"}),
    "risk.open": frozenset({"account_profiles"}),
    "quant.revise": frozenset({"account_profiles", "exits", "costs"}),
    "quant.propose": frozenset({"account_profiles", "exits", "costs", "ranking"}),
    "broker.execute": frozenset(),
}
# Steps that size, gate or trade against the arm's own account: always the arm's.
ACCOUNT_STEPS: frozenset[str] = frozenset({"exits.mandatory", "quant.propose", "broker.execute"})
# E13.12 (D56): overlay leaves (dotted ``<target>.<path>`` globs) that change a
# non-loop persona, so the arm runs that persona itself (arm_owned_personas).
PERSONA_OVERLAY_PREFIXES: dict[str, tuple[str, ...]] = {
    "scout": (
        "routines.personas.scout",
        "routines.personas.scout_*",  # E14.5: personas.scout_buzz_velocity
        "routines.personas.retail_sentiment_context",  # E14.6: also the Scout's prompt
        "routines.personas.scout.*",
        "routines.funnel.scout.*",
        "universe.*",
    ),
    "scalp": (
        "routines.personas.scalp_*",
        "routines.personas.scalp",
        "routines.personas.scalp.*",
        "routines.funnel.scalp.*",
        "routines.categories.*",
    ),
    # E14.5 (D60): a trending-ranker overlay (e.g. `scoring: velocity`) runs the
    # arm's own trending tier job; it also owns the Scout (TIER_COMPANIONS), which
    # re-resolves the active list after it.
    "trending": ("routines.sources.universe.trending.*",),
}
# The routine jobs each arm persona runs (the 30-min Scalp and its 22:00 run).
PERSONA_JOBS: dict[str, tuple[str, ...]] = {
    "scout": ("scout",),
    "scalp": ("scalp", "scalp.overnight"),
    "trending": ("universe.trending",),
}
# E14.5: an arm-owned tier job brings the persona that re-resolves the active list
# after it (05:50 trending -> 06:00 Scout), so the arm's list carries its own tier.
TIER_COMPANIONS: dict[str, frozenset[str]] = {"trending": frozenset({"scout"})}
# Book-specific kinds: written per arm against its own positions, never synced.
BOOK_KINDS: frozenset[str] = frozenset(
    {"position_review", "exit_watchlist", "exit_case", "risk_exit_review"}
)

_INGEST_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # raw_docs.ingested_at (arc.ingest.store)
#: D69: the halt reason prefix of an arm whose broker account changed under it.
ACCOUNT_CHANGED = "account_changed"
_ACTOR = "arc.experiments"


class ArmStartError(RuntimeError):
    """t0 refused (arm store exists, keys missing, arm account not flat, ...)."""


@dataclass(frozen=True)
class ArmAccount:
    """What t0 reads from one arm broker account (D69): never stored raw."""

    account_number: str
    equity: Decimal
    positions: int
    open_orders: int

    @property
    def flat(self) -> bool:
        return self.positions == 0 and self.open_orders == 0


@dataclass(frozen=True)
class _LiveArm:
    """An arm of a running experiment, read from its store (start's account checks)."""

    experiment_id: str
    arm: str
    keys_env: str
    account_mode: str
    account_sha256: str | None
    t0_equity: Decimal


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
    """Open + migrate an arm store and bind it to the running env (D70).

    Arm stores are paper by construction (experiment account); a live process
    refuses them like any paper store.
    """
    from arc.config import get_settings
    from arc.store.db import connect
    from arc.store.identity import bind_store_env
    from arc.store.migrate import migrate

    conn = connect(path)
    migrate(conn)
    try:
        bind_store_env(conn, get_settings().env.value, path=str(path))
    except Exception:
        conn.close()
        raise
    return conn


def _db_file(conn: sqlite3.Connection) -> Path | None:
    r = conn.execute("PRAGMA database_list").fetchone()
    return Path(r[2]).resolve() if r is not None and r[2] else None


def runner_config(conn: sqlite3.Connection, path: Path | str | None = None) -> RunnerConfig:
    """``experiments.runner`` with the D26 overrides; *path* = another experiments.yaml."""
    from arc.control.effective import effective_settings, experiments_config

    return experiments_config(effective_settings(conn), path).runner


def fork_step(chain: list[str], overlay: Mapping[str, Any], personas: Iterable[str] = ()) -> str:
    """The first chain step the arm runs itself (see the module doc).

    E13.12: an arm that runs its own Scout / Scalp (*personas*) ranks a different idea
    pool, so it forks at ``research`` at the latest (by chain order).
    """
    targets = _overlay_targets(overlay)
    fork = chain[-1]
    for step in chain:
        if step in ACCOUNT_STEPS or STEP_TARGETS.get(step, frozenset()) & targets:
            fork = step
            break
        if step not in STEP_TARGETS:  # an unknown step: never assume it is unaffected
            fork = step
            break
    if set(personas) and _RESEARCH in chain and chain.index(_RESEARCH) < chain.index(fork):
        return _RESEARCH
    return fork


def book_fork(chain: list[str], fork: str, open_ids: Iterable[str]) -> str:
    """E10.2b: the fork for one paired chain, given the arm's own open book.

    Research writes the exit watchlist against the book it sees, so control's Research
    reviews control's book only (``exit_watchlist`` is a :data:`BOOK_KINDS` kind, never
    synced). An arm holding any open structure of its own (its store never holds the
    legacy book) therefore runs ``research`` itself, with its own ``portfolio_view``;
    with an empty book it keeps reusing control's Research (*fork* unchanged).
    """
    if (
        any(True for _ in open_ids)
        and _RESEARCH in chain
        and fork in chain
        and chain.index(_RESEARCH) < chain.index(fork)
    ):
        return _RESEARCH
    return fork


def _open_book(arm: sqlite3.Connection) -> list[str]:
    """The arm's own open structure ids (the arm store never holds the legacy book)."""
    return [str(r[0]) for r in arm.execute("SELECT id FROM open_structures WHERE status = 'open'")]


def _overlay_paths(overlay: Mapping[str, Any]) -> list[str]:
    """Every leaf of *overlay* as a dotted ``<target>.<key>…`` path."""
    from arc.control.effective import overlay_overrides

    return sorted(
        ".".join((target, *path))
        for target, pairs in overlay_overrides(dict(overlay)).items()
        for path in pairs
    )


def arm_owned_personas(overlay: Mapping[str, Any]) -> set[str]:
    """E13.12: the non-loop personas an arm must run itself because *overlay* changes them.

    A persona is the arm's own when any overlay leaf matches one of its
    :data:`PERSONA_OVERLAY_PREFIXES` globs (deterministic, no config read).
    """
    from fnmatch import fnmatchcase

    paths = _overlay_paths(overlay)
    return _with_companions(
        {
            persona
            for persona, globs in PERSONA_OVERLAY_PREFIXES.items()
            if any(fnmatchcase(p, g) for p in paths for g in globs)
        }
    )


def _with_companions(personas: set[str]) -> set[str]:
    """*personas* plus each one's :data:`TIER_COMPANIONS` (E14.5)."""
    return personas.union(*(TIER_COMPANIONS.get(p, frozenset()) for p in personas))


def persona_jobs(personas: Iterable[str]) -> list[str]:
    """The routine jobs of the arm's own non-loop personas (``scalp`` -> both Scalp jobs)."""
    return [job for p in sorted(set(personas)) for job in PERSONA_JOBS[p]]


def arm_plan(routines: RoutinesConfig, overlay: Mapping[str, Any], runner: RunnerConfig) -> ArmPlan:
    """The arm's :class:`~arc.experiments.models.ArmPlan` (pure; computed at t0).

    *routines* is the arm's effective routines config (control's + the overlay), so
    ``chain: auto`` resolves to the fixed :data:`~arc.routines.config.AUTO_CHAINS`.
    """
    from arc.experiments.models import ArmPlan

    personas = sorted(_with_companions(arm_owned_personas(overlay) | set(runner.arm_personas)))
    loop = routines.loop.job
    found = routines.job(loop)
    chain = [loop, *(found[1].chain if found else [])]
    return ArmPlan(
        fork_step=fork_step(chain, overlay, personas),
        arm_personas=personas,  # type: ignore[arg-type] - validated Literal
        arm_jobs=list(runner.arm_jobs),
        shared_kinds=sorted(_shared_kinds(routines, chain, runner.arm_jobs, personas)),
        own_producers=persona_jobs(personas),
    )


def plan_of(ident: ArmIdentity, routines: RoutinesConfig, runner: RunnerConfig) -> ArmPlan:
    """The arm's stored plan; a pre-E13.12 store (no plan) is recomputed from its overlay
    alone (``runner.arm_personas`` applies at t0 only, so it loads with none)."""
    from arc.experiments.models import ArmPlan

    if ident.plan is not None:
        return ArmPlan.model_validate(ident.plan)
    return arm_plan(routines, ident.overlay, runner.model_copy(update={"arm_personas": []}))


def plans_at_start(
    control: sqlite3.Connection,
    st: ExperimentState,
    runner: RunnerConfig,
    *,
    routines_path: str | None = None,
) -> dict[str, ArmPlan]:
    """Runner arm -> its plan for experiment *st*, from control's effective routines plus
    the spec arm's overlay (what the arm store will resolve once it exists)."""
    from arc.control.effective import routines_for_overlay

    out: dict[str, ArmPlan] = {}
    for name, arm in runner.arms_for(st.spec).items():
        overlay = st.spec.arms.arm(arm.spec_arm).overlay
        routines = routines_for_overlay(control, overlay, routines_path)
        out[name] = arm_plan(routines, overlay, runner)
    return out


def _overlay_targets(overlay: Mapping[str, Any]) -> set[str]:
    """The overlay's targets (config file stems with a non-empty overlay)."""
    return {t for t, data in overlay.items() if data}


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


def _ro(path: Path) -> sqlite3.Connection | None:
    import sqlite3 as _sqlite3

    if not path.is_file():
        return None
    try:
        return _sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except _sqlite3.Error:
        return None


def live_arms(control: sqlite3.Connection, *, exclude: str | None = None) -> list[_LiveArm]:
    """Every arm of every running experiment (except *exclude*), read from its store."""
    import sqlite3 as _sqlite3

    from arc.experiments.tape import running_experiments

    out: list[_LiveArm] = []
    for st in running_experiments(control):
        if st.experiment_id == exclude:
            continue
        for name, path in arm_stores(control, st.experiment_id).items():
            conn = _ro(path)
            if conn is None:
                continue
            try:
                ident = read_identity(conn)
                r = conn.execute(
                    "SELECT amount FROM virtual_ledger WHERE kind = 'open' LIMIT 1"
                ).fetchone()
            except _sqlite3.Error:
                continue
            finally:
                conn.close()
            if ident is None:
                continue
            out.append(
                _LiveArm(
                    experiment_id=st.experiment_id,
                    arm=name,
                    keys_env=ident.keys_env,
                    account_mode=ident.account_mode,
                    account_sha256=ident.account_sha256,
                    t0_equity=Decimal(str(r[0])) if r is not None else Decimal(0),
                )
            )
    return out


def _check_accounts(
    runner: RunnerConfig,
    arms: Mapping[str, ArmRunner],
    live: list[_LiveArm],
    *,
    experiment_id: str,
    t0_equity: Decimal,
    probe: Callable[[str], ArmAccount] | None,
) -> dict[str, ArmAccount]:
    """D69 t0 account rules per ``keys_env``; returns what *probe* read (keys -> account).

    * dedicated and shared arms never share one account (either direction);
    * a dedicated arm owns its account: no running arm may already use it, and it
      starts flat;
    * a shared account is flat at its FIRST arm's t0; later experiments join it busy;
    * a running arm recorded on a different account (the keys were replaced) blocks
      ``start`` until that experiment is stopped;
    * shared capacity: running arms' t0 + the new arms' t0 <= equity x frac.
    """

    mode = runner.account_mode
    new_by_keys: dict[str, list[str]] = {}
    for name, arm in arms.items():
        new_by_keys.setdefault(arm.keys_env, []).append(name)
    seen: dict[str, ArmAccount] = {}
    for keys, names in new_by_keys.items():
        on = [a for a in live if a.keys_env == keys]
        other_mode = sorted({a.experiment_id for a in on if a.account_mode != mode})
        if other_mode:
            msg = (
                f"{experiment_id} ({mode} mode) would trade {keys}_*, which running "
                f"experiment(s) {', '.join(other_mode)} use in "
                f"{'shared' if mode == 'dedicated' else 'dedicated'} mode: dedicated and "
                "shared arms never share one account (D69); stop "
                f"{', '.join(other_mode)} first (`arc experiment stop <id>`)"
            )
            raise ArmStartError(msg)
        if mode == "dedicated" and (on or len(names) > 1):
            users = sorted({a.experiment_id for a in on}) or [experiment_id]
            msg = (
                f"{keys}_* is a dedicated arm account already used by {', '.join(users)}; "
                "each dedicated arm needs its own paper account"
            )
            raise ArmStartError(msg)
        if probe is None:
            continue
        acct = probe(keys)
        seen[keys] = acct
        sha, last4 = account_fingerprint(acct.account_number)
        moved = sorted({a.experiment_id for a in on if a.account_sha256 not in (None, sha)})
        if moved:
            msg = (
                f"{keys}_* now reaches account …{last4}, but running experiment(s) "
                f"{', '.join(moved)} started on a different account: stop them first "
                "(`arc experiment stop <id>`), then start on the new account"
            )
            raise ArmStartError(msg)
        if not on and not acct.flat:
            msg = (
                f"{keys} account holds {acct.positions} position(s) and {acct.open_orders} "
                "open order(s); close them in the Alpaca dashboard before t0 (the "
                f"{'first arm on a shared' if mode == 'shared' else 'arm'} account must "
                "start flat)"
            )
            raise ArmStartError(msg)
        if mode == "shared":
            frac = Decimal(str(runner.shared_capacity_frac))
            running = sum((a.t0_equity for a in on), Decimal(0))
            need = running + t0_equity * len(names)
            cap = acct.equity * frac
            if need > cap:
                msg = (
                    f"shared account {keys} …{last4} too small: {len(on)} running arm(s) "
                    f"${running:,.2f} + {len(names)} new x ${t0_equity:,.2f} = "
                    f"${need:,.2f} > equity ${acct.equity:,.2f} x {runner.shared_capacity_frac}"
                    f" = ${cap:,.2f} (short ${need - cap:,.2f})"
                )
                raise ArmStartError(msg)
    return seen


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
    routines_path: str | None = None,
    probe: Callable[[str], ArmAccount] | None = None,
) -> ExperimentState:
    """t0 of a registered experiment: create every arm store, then mark it ``running``.

    E13.12: each arm's :class:`~arc.experiments.models.ArmPlan` (fork step, own
    personas, shared kinds) is computed here and stored on its identity and in the
    ``running`` event (``arm_plans``).

    D69: the runner arms are :meth:`RunnerConfig.arms_for` the spec (one per treatment
    with a ``treatments`` template). *probe* ``(keys_env) -> ArmAccount`` reads each
    arm account once (live; fixtures pass the fixture account or ``None``); the
    account rules are :func:`_check_accounts`, and each arm records its account's
    sha256 + last 4. *check_flat* is the pre-D69 per-arm hook (tests). A failure
    leaves no arm store behind and the experiment ``registered``.
    """
    from arc.experiments.models import ExperimentStatus, RunningDetail
    from arc.experiments.store import ExperimentStore
    from arc.experiments.virtual import legacy_reservations, open_account
    from arc.routines.manifest import config_hashes
    from arc.routines.runs import RoutineStateRepo

    store = ExperimentStore.for_runner(control, runner, now=lambda: now)
    st = store.require(experiment_id)
    if st.status is not ExperimentStatus.REGISTERED:
        msg = f"{experiment_id} is {st.status.value}; only a registered experiment starts"
        raise ArmStartError(msg)
    if not runner.arms:
        msg = "experiments.runner.arms is empty: no arm to run the treatment"
        raise ArmStartError(msg)
    try:
        arms = runner.arms_for(st.spec)
    except ValueError as exc:
        raise ArmStartError(str(exc)) from exc
    control_db = _db_file(control)
    if control_db is None:
        msg = "the control store must be a file (arms read it by path)"
        raise ArmStartError(msg)
    live = live_arms(control, exclude=experiment_id)
    if len(live) + len(arms) > runner.max_parallel_arms:
        msg = (
            f"max_parallel_arms {runner.max_parallel_arms}: {len(live)} arm(s) running + "
            f"{len(arms)} of {experiment_id}; stop an experiment first"
        )
        raise ArmStartError(msg)
    taken = {p.resolve() for p in arm_stores(control).values()}
    legacy = legacy_reservations(control)
    paths: dict[str, Path] = {}
    for name, arm in arms.items():
        p = Path(arm.db_path(experiment_id, name))
        if arm_dir is not None:
            p = arm_dir / p.name
        p = p.resolve()
        if p == control_db:
            msg = f"arm {name} store is the control store"
            raise ArmStartError(msg)
        if p in taken or p in paths.values():
            msg = f"arm {name} store {p} is another arm's store; every arm needs its own"
            raise ArmStartError(msg)
        if p.exists():
            msg = f"arm {name} store {p} already exists; every experiment starts fresh"
            raise ArmStartError(msg)
        if check_flat is not None:
            check_flat(arm)
        paths[name] = p
    accounts = _check_accounts(
        runner, arms, live, experiment_id=experiment_id, t0_equity=t0_equity, probe=probe
    )
    plans = plans_at_start(control, st, runner, routines_path=routines_path)
    created: list[Path] = []
    state = RoutineStateRepo(control)
    try:
        for name, arm in arms.items():
            p = paths[name]
            p.parent.mkdir(parents=True, exist_ok=True)
            created.append(p)
            acct = accounts.get(arm.keys_env)
            sha, last4 = (
                account_fingerprint(acct.account_number) if acct is not None else (None, None)
            )
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
                        overlay=st.spec.arms.arm(arm.spec_arm).overlay,
                        created_at=now,
                        plan=plans[name].model_dump(mode="json"),
                        account_mode=runner.account_mode,
                        account_sha256=sha,
                        account_last4=last4,
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
            arm_plans=plans,
        )
        for name, p in paths.items():
            state.set(STATE_ARM.format(experiment_id=experiment_id, arm=name), str(p), now=now)
        out = store.start(experiment_id, detail, actor=actor, aa_override=aa_override)
    except BaseException:
        for p in created:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{p}{suffix}").unlink(missing_ok=True)
        for name in paths:
            state.delete(STATE_ARM.format(experiment_id=experiment_id, arm=name))
        raise
    log.info(
        "experiments.started",
        experiment_id=experiment_id,
        account_mode=runner.account_mode,
        arms={n: str(p) for n, p in paths.items()},
        plans={n: pl.model_dump(mode="json") for n, pl in plans.items()},
        t0_equity=str(t0_equity),
        legacy=len(legacy),
    )
    return out


def _arm_broker(keys_env: str, environ: Mapping[str, str] | None = None) -> Any:
    from arc.broker.alpaca_paper import AlpacaPaperBroker
    from arc.broker.registry import resolve_broker
    from arc.config import get_settings

    # the arm's own keys (arm_keys refuses ALPACA / ALPACA_TEST) via the registry
    broker = resolve_broker(get_settings(), keys_env=keys_env, environ=environ)
    if not isinstance(broker, AlpacaPaperBroker):  # only alpaca/paper/rest constructs
        msg = f"arm t0 check needs the Alpaca paper broker, got {type(broker).__name__}"
        raise ArmStartError(msg)
    return broker


def live_account_probe(environ: Mapping[str, str] | None = None) -> Callable[[str], ArmAccount]:
    """The live t0 probe (D69): the arm account's number, equity, positions, open orders."""

    def probe(keys_env: str) -> ArmAccount:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        broker = _arm_broker(keys_env, environ)
        info = broker.account()
        open_orders = broker._client.get_orders(  # noqa: SLF001 - read-only listing
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50)
        )
        return ArmAccount(
            account_number=info.account_id,
            equity=info.equity,
            positions=len(broker.positions()),
            open_orders=len(open_orders or []),
        )

    return probe


def live_account_number(environ: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """``keys_env -> account_number`` (D69 account identity guard, every arms tick)."""

    def number(keys_env: str) -> str:
        return str(_arm_broker(keys_env, environ).account().account_id)

    return number


def check_arm_account(
    arm: sqlite3.Connection,
    ident: ArmIdentity,
    account_number: str,
    *,
    now: _dt.datetime,
    notifier: Any = None,
) -> bool:
    """D69: True when *account_number* is the account the arm started on (or none was
    recorded). On a mismatch the arm store gets an ``account_changed`` halt (once) and
    an ``[Ops]`` notice names the experiment; the caller runs nothing for the arm.
    """
    from arc.store.repos import HaltRepo

    if ident.account_sha256 is None:
        return True
    sha, last4 = account_fingerprint(account_number)
    if sha == ident.account_sha256:
        return True
    reason = (
        f"{ACCOUNT_CHANGED}: {ident.keys_env}_* now reach account …{last4}, not "
        f"…{ident.account_last4} (t0); stop {ident.experiment_id}"
    )
    repo = HaltRepo(arm)
    if not any(str(h.get("reason", "")).startswith(ACCOUNT_CHANGED) for h in repo.active()):
        repo.halt(reason=reason, actor=_ACTOR, kind="manual", at=to_db(now))
        if notifier is not None:
            notifier.post(
                f"[Ops] Experiment {ident.experiment_id} arm {ident.arm} halted "
                f"({ACCOUNT_CHANGED}): its keys {ident.keys_env}_* now reach account "
                f"…{last4}, not the t0 account …{ident.account_last4}. Nothing runs on "
                f"the arm; stop {ident.experiment_id} (`arc experiment stop "
                f"{ident.experiment_id}`)."
            )
    log.error(
        "experiments.arm_account_changed",
        arm_id=ident.arm_id,
        keys_env=ident.keys_env,
        recorded=ident.account_last4,
        live=last4,
    )
    return False


def live_flat_check(environ: Mapping[str, str] | None = None) -> Callable[[ArmRunner], None]:
    """Pre-D69 live t0 check, kept for callers: the arm's account must be flat."""
    probe = live_account_probe(environ)

    def check(arm: ArmRunner) -> None:
        acct = probe(arm.keys_env)
        if acct.positions:
            msg = (
                f"{arm.keys_env} account holds {acct.positions} position(s); close them in "
                "the Alpaca dashboard before t0 (the arm must start flat)"
            )
            raise ArmStartError(msg)
        if acct.open_orders:
            msg = (
                f"{arm.keys_env} account has {acct.open_orders} open order(s); cancel them "
                "in the Alpaca dashboard before t0 (the arm must start flat)"
            )
            raise ArmStartError(msg)

    return check


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------


def _shared_kinds(
    routines: RoutinesConfig,
    chain: list[str],
    arm_jobs: list[str],
    personas: Iterable[str] = (),
) -> set[str]:
    """Kinds synced from control: what the chain and the arm's own personas read, minus
    what the arm's own jobs write and the book kinds (E13.12: never synced).

    Kinds the arm's own Scout / Scalp write (``candidate``, ``note``, ``story``,
    ``scout_read``, ``universe_tier``) stay shared *by producer*: control's rows from
    other jobs (momentum tier, the Scalp's ideas for a Scout-only arm) still sync, its
    rows from the arm's persona jobs never do (:func:`sync_shared_context`). The active
    list is re-resolved by those personas, so an arm owning one never syncs it.
    """
    personas = sorted(set(personas))
    reads: set[str] = set()
    for step in chain:
        spec = routines.step(step)[1]
        reads.update(spec.reads or [])
    for job in persona_jobs(personas):
        found = routines.job(job)
        if found is not None:
            reads.update(found[1].reads or [])
    if "scalp" in personas:
        reads.add("raw_doc_ref")  # E13.12: the arm's Scalp reads control's raw docs
    own: set[str] = set(BOOK_KINDS)
    for job in arm_jobs:
        found = routines.job(job)
        if found is None:
            continue
        own.update(found[1].writes or [])
        for step in getattr(found[1], "chain", []) or []:
            own.update(routines.step(step)[1].writes or [])
    if personas:
        own.add("active_universe")
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
    personas: Iterable[str] = (),
) -> int:
    """Copy control's active entries of the kinds the chain reads (not the arm's own).

    E13.12: rows written by the arm's own persona jobs (*personas*: ``scout`` /
    ``scalp``) are never copied; the ``candidates`` rows behind copied ``candidate``
    entries and the ``raw_docs`` behind copied ``raw_doc_ref`` entries come along
    (``proposals.candidate_id`` / the Scalp's doc queue read them by id).
    """
    personas = sorted(set(personas))
    kinds = sorted(_shared_kinds(routines, chain, arm_jobs, personas))
    if not kinds:
        return 0
    own = persona_jobs(personas)
    marks = ",".join("?" * len(kinds))
    rows = [
        r
        for r in control.execute(
            f"""SELECT * FROM context_entries
                WHERE status = 'active' AND kind IN ({marks}) AND valid_from <= ?
                ORDER BY valid_from, created_at, rowid""",  # noqa: S608 - placeholders only
            (*kinds, to_db(as_of)),
        ).fetchall()
        if r["produced_by"] not in own
    ]
    n = _copy_entries(control, arm, rows, routines)
    _copy_candidates(control, arm, [r for r in rows if r["kind"] == "candidate"])
    _copy_raw_docs(control, arm, [r["subject"] for r in rows if r["kind"] == "raw_doc_ref"])
    return n


def sync_persona_inputs(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    routines: RoutinesConfig,
    personas: Iterable[str],
    *,
    as_of: _dt.datetime,
    since: _dt.datetime | None = None,
) -> int:
    """E13.12: before the arm's own Scout / Scalp run, copy control's inputs they read.

    The kinds those personas read (``channel_brief``, ``options_daily``, the options
    tape, control's tiers) minus what they write themselves; for the Scalp also
    control's raw docs ingested in ``(max(since, as_of - 2 days), as_of]`` that the arm
    store lacks (sources never run in an arm; *since* = the arm's t0, so a new arm never
    re-reads a backlog). Rows the arm's own persona jobs wrote in control are never
    copied.
    """
    personas = sorted(set(personas))
    if not personas:
        return 0
    return sync_shared_context(control, arm, routines, [], [], as_of=as_of, personas=personas) + (
        _sync_raw_docs(control, arm, as_of=as_of, since=since) if "scalp" in personas else 0
    )


def _sync_raw_docs(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    *,
    as_of: _dt.datetime,
    since: _dt.datetime | None = None,
) -> int:
    """Control's raw docs ingested in the 2 days up to *as_of* that the arm store lacks.

    ``ingested_at`` is ISO-8601 UTC text (``arc.ingest.store``), compared as text.
    """
    utc = as_of.astimezone(_dt.UTC)
    floor = utc - _dt.timedelta(days=2)
    if since is not None:
        floor = max(floor, since.astimezone(_dt.UTC))
    ids = [
        r[0]
        for r in control.execute(
            """SELECT id FROM raw_docs WHERE ingested_at <= ? AND ingested_at >= ?
               ORDER BY ingested_at, id""",
            (utc.strftime(_INGEST_FMT), floor.strftime(_INGEST_FMT)),
        )
    ]
    return _copy_raw_docs(control, arm, ids)


def _copy_candidates(
    control: sqlite3.Connection, arm: sqlite3.Connection, rows: list[sqlite3.Row]
) -> int:
    """The ``candidates`` rows behind copied ``candidate`` entries (same ids; skip existing)."""
    ids = sorted({str(i) for r in rows if (i := json.loads(r["payload"]).get("id"))})
    return _copy_rows(control, arm, "candidates", ids)


def _copy_raw_docs(control: sqlite3.Connection, arm: sqlite3.Connection, ids: list[str]) -> int:
    """E13.12: control's raw docs *ids* into the arm store for the arm's own Scalp.

    The Scalp bookkeeping (``scalped_at`` / ``scalp_run_id`` / ``scalp_status``) is kept
    per store: a copied doc arrives unread, except an ingest-time ``filtered`` close
    (D55), which is part of the doc, not of a Scalp run. Existing rows are untouched.
    """
    from arc.ingest.store import FILTERED_STATUS

    def reset(row: dict[str, Any]) -> dict[str, Any]:
        filtered = row.get("scalp_status") == FILTERED_STATUS
        return {
            **row,
            "scalped_at": row.get("scalped_at") if filtered else None,
            "scalp_run_id": None,
            "scalp_status": FILTERED_STATUS if filtered else None,
        }

    return _copy_rows(control, arm, "raw_docs", ids, transform=reset)


def _copy_rows(
    control: sqlite3.Connection,
    arm: sqlite3.Connection,
    table: str,
    ids: list[str],
    *,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    if not ids:
        return 0
    cols = [r[1] for r in control.execute(f"PRAGMA table_info({table})")]
    arm_cols = {r[1] for r in arm.execute(f"PRAGMA table_info({table})")}
    keep = [c for c in cols if c in arm_cols]
    n = 0
    with arm:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            have = {
                r[0]
                for r in arm.execute(
                    f"SELECT id FROM {table} WHERE id IN ({marks})",  # noqa: S608
                    chunk,
                )
            }
            for r in control.execute(
                f"SELECT * FROM {table} WHERE id IN ({marks})",  # noqa: S608
                chunk,
            ):
                row = {c: r[c] for c in keep}
                if row["id"] in have:
                    continue
                if transform is not None:
                    row = transform(row)
                cur = arm.execute(
                    f"INSERT OR IGNORE INTO {table} ({', '.join(keep)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' * len(keep))})",
                    tuple(row[c] for c in keep),
                )
                n += cur.rowcount
    return n


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

    by_job = {r.job: r for r in control_rows}
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
    chain = [root.job, *found[1].chain]
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
    status = {r.job: r.status.value for r in rows}
    plan = plan_of(ident, routines, runner)
    personas = list(plan.arm_personas)
    fork = fork_step(chain, ident.overlay, personas)
    planned = fork
    book = _open_book(arm)
    fork = book_fork(chain, fork, book)
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
            control,
            arm,
            routines,
            chain,
            runner.arm_jobs,
            as_of=root.scheduled_for,
            personas=personas,
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
            else f"forked at {fork}"
            + (f" (own book: {len(book)} open; plan {planned})" if fork != planned else "")
            + f"; reused {len(upstream)} step(s), synced {synced} entries"
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
    account_number: Callable[[str], str] | None = None,
    notifier: Any = None,
) -> dict[str, Any]:
    """Pair recent control loop chains, then run each arm's own jobs (see the module doc).

    D69: serves the arms of EVERY running experiment; ``report["arms"]`` is keyed by
    arm id (``XP-<n>:<arm>``). *account_number* ``(keys_env) -> account number`` (live)
    is read once per keys per tick: an arm whose recorded t0 account differs runs
    nothing and is halted ``account_changed`` (:func:`check_arm_account`).

    Never raises for one arm's failure; the report says what each arm did.
    """
    from arc.experiments.tape import prune_tape, running_experiments

    report: dict[str, Any] = {"now": now.isoformat(), "arms": {}}
    running = running_experiments(control)
    if not running:
        report["skipped"] = "no running experiment"
        return report
    runner = runner_config(control)
    report["experiment_ids"] = [st.experiment_id for st in running]
    if not runner.enabled:
        report["skipped"] = "experiments.runner.enabled is off"
        return report
    report["tape_pruned"] = prune_tape(control, keep_days=runner.tape_keep_days, now=now)
    accounts: dict[str, str | Exception] = {}

    def live_number(keys_env: str) -> str | Exception:
        if keys_env not in accounts:
            try:
                accounts[keys_env] = account_number(keys_env)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - reported per arm; the arm skips
                accounts[keys_env] = exc
        return accounts[keys_env]

    for st in running:
        for name, path in arm_stores(control, st.experiment_id).items():
            out: dict[str, Any] = {"store": str(path)}
            report["arms"][f"{st.experiment_id}:{name}"] = out
            _tick_one_arm(
                control,
                st,
                name,
                path,
                out,
                runner=runner,
                routines_path=routines_path,
                now=now,
                lock_dir=lock_dir,
                clock=clock,
                handlers=handlers,
                spawner=spawner,
                live_number=live_number if account_number is not None else None,
                notifier=notifier,
            )
    log.info("experiments.arms_tick", **{k: v for k, v in report.items() if k != "arms"})
    return report


def _tick_one_arm(
    control: sqlite3.Connection,
    st: ExperimentState,
    name: str,
    path: Path,
    out: dict[str, Any],
    *,
    runner: RunnerConfig,
    routines_path: str | None,
    now: _dt.datetime,
    lock_dir: Path | None,
    clock: Callable[[], _dt.datetime] | None,
    handlers: Mapping[str, Handler] | None,
    spawner: Any,
    live_number: Callable[[str], str | Exception] | None,
    notifier: Any,
) -> None:
    from arc.approvals.cli import make_service
    from arc.control.effective import effective_routines, effective_settings
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.handlers import RunEnv
    from arc.routines.heartbeat import LogNotifier
    from arc.routines.locks import LockManager, NullLocks

    if not path.is_file():
        out["error"] = "arm store missing"
        return
    since = max(
        now - _dt.timedelta(seconds=runner.max_lag_seconds),
        st.running.t0 if st.running is not None else now,
    )
    arm = _connect(path)
    try:
        ident = read_identity(arm)
        if ident is None or ident.experiment_id != st.experiment_id:
            out["error"] = "arm store belongs to another experiment"
            return
        if live_number is not None and ident.account_sha256 is not None:
            number = live_number(ident.keys_env)
            if isinstance(number, Exception):
                out["error"] = f"account check failed: {type(number).__name__}: {number}"
                return
            if not check_arm_account(arm, ident, number, now=now, notifier=notifier):
                out["error"] = f"{ACCOUNT_CHANGED}: arm halted, nothing ran"
                return
        # D69: two experiments may reuse an arm name; lock per experiment + arm
        arm_lock = lock_dir / f"arm-{st.experiment_id}-{name}" if lock_dir is not None else None
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
        plan = plan_of(ident, routines, runner)
        out["plan"] = plan.model_dump(mode="json")
        own = persona_jobs(plan.arm_personas)
        if own:  # E13.12: the arm's own Scout / Scalp read control's synced inputs
            out["persona_synced"] = sync_persona_inputs(
                control,
                arm,
                routines,
                plan.arm_personas,
                as_of=now,
                since=st.running.t0 if st.running is not None else None,
            )
        disp = Dispatcher(
            arm,
            arm_routines(routines, [*runner.arm_jobs, *own]),
            handlers=handlers,
            locks=LockManager(arm_lock) if arm_lock is not None else NullLocks(),
            notifier=LogNotifier(),
            clock=clock,
            run_env=env,
            spawner=spawner,
        )
        tick = disp.tick(now)
        out["jobs"] = [(o.job, o.status) for o in tick.outcomes]
        out["expired_approvals"] = len(
            make_service(arm, effective_settings(arm), slack=False).expire_due(now)
        )
    except Exception as exc:  # noqa: BLE001 - one arm's failure never stops another
        out["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("experiments.arm_tick_failed", arm=name, experiment_id=st.experiment_id)
    finally:
        arm.close()


def arms_preview(
    control: sqlite3.Connection,
    experiment_id: str | None,
    *,
    routines_path: str | None,
    now: _dt.datetime,
    since: _dt.datetime | None = None,
    runner: RunnerConfig | None = None,
) -> dict[str, Any]:
    """E13.12: each arm's :class:`ArmPlan` and its dry-run tick, with no write anywhere.

    For a running experiment the plans are the stored ones; for a draft or registered
    one (*experiment_id*) they are what ``arc experiment start`` would compute now.
    The tick listing plans the arm's own jobs (``arm_jobs`` + its personas) at *now*
    on an in-memory copy of the schema (window ``(since, now]``, default one tick).
    Sources and control's jobs are never listed: an arm never runs them.
    """
    import sqlite3 as _sqlite3

    from arc.control.effective import routines_for_overlay
    from arc.experiments.store import ExperimentStore
    from arc.experiments.tape import running_experiment
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.heartbeat import LogNotifier
    from arc.store.migrate import migrate

    st = running_experiment(control)
    if experiment_id is not None and (st is None or st.experiment_id != experiment_id):
        st = ExperimentStore(control).require(experiment_id)
    if st is None:
        return {"now": now.isoformat(), "skipped": "no running experiment"}
    runner = runner if runner is not None else runner_config(control)
    stored = st.running.arm_plans if st.running is not None else {}
    plans = {**plans_at_start(control, st, runner, routines_path=routines_path), **stored}
    report: dict[str, Any] = {
        "now": now.isoformat(),
        "experiment_id": st.experiment_id,
        "status": st.status.value,
        "plans_from": "stored (running)" if stored else "computed (not started)",
        "arms": {},
    }
    for name, arm in runner.arms_for(st.spec).items():
        plan = plans[name]
        overlay = st.spec.arms.arm(arm.spec_arm).overlay
        routines = routines_for_overlay(control, overlay, routines_path)
        scratch = _sqlite3.connect(":memory:")
        try:
            migrate(scratch)
            disp = Dispatcher(
                scratch,
                arm_routines(routines, [*plan.arm_jobs, *persona_jobs(plan.arm_personas)]),
                notifier=LogNotifier(),
                is_halted=lambda: False,
            )
            tick = disp.tick(now, dry_run=True, since=since)
        finally:
            scratch.close()
        report["arms"][name] = {
            "spec_arm": arm.spec_arm,
            "plan": plan.model_dump(mode="json"),
            "tick": tick.lines(),
        }
    return report


def arm_t0_iso(conn: sqlite3.Connection) -> str | None:
    """The arm store's t0 (``open`` ledger row) as ISO text, for reports."""
    r = conn.execute("SELECT at FROM virtual_ledger WHERE kind = 'open' LIMIT 1").fetchone()
    return from_db(r[0]).isoformat() if r is not None else None


def _dump(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=2)
