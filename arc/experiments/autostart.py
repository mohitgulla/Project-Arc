"""Pre-open auto-start of owner-approved experiments (E21.4, PLAN D86).

The ``experiments.autostart`` routine (``config/routines.yaml``, trading days 09:05 ET,
after the pre-market sources and before the 09:40 trading loop) takes the oldest
``registered`` experiment that ``arc experiment adopt`` brought in (it carries a
``proposal_ref``) and starts it once every t0 check passes:

- ``experiments.autostart.enabled`` is on;
- today is a trading session (``arc.utils.calendar.is_session``);
- no halt is active;
- an A/A calibration is committed: an ``aa`` experiment stopped with a recorded sigma
  (E10.4 / E15.9) **and** its verdict file is on ``origin/main`` (refused while none
  exists: the autostart never uses the owner's A/A override);
- the spec is merged on ``origin/main`` and its hash equals the registered lock;
- ``arc experiment start``'s own checks (:func:`arc.experiments.runner.start_arms`):
  arm stores, ``max_parallel_arms``, the shared account flat / ``shared_capacity_frac``.

At most ``experiments.autostart.max_per_day`` start per day. Experiments registered by
hand (no ``proposal_ref``, e.g. the E15.9 A/A, which waits for the owner's "ok") are
never auto-started. The decision is pure (:func:`decide`); the routine wires the live
store, ``origin/main`` and the broker probe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from arc.experiments.models import ExperimentKind, ExperimentSpec, ExperimentStatus, spec_hash
from arc.experiments.publish import EXPERIMENTS_DIR, VERDICTS_DIR
from arc.experiments.store import proposal_ref_of

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.models import ExperimentState
    from arc.experiments.publish import MainView
    from arc.experiments.store import ExperimentStore

__all__ = [
    "AUTOSTART_ACTOR",
    "AutostartDecision",
    "aa_calibration",
    "autostart",
    "decide",
    "merged_spec_hash",
    "started_today",
]

log = structlog.get_logger(__name__)

AUTOSTART_ACTOR = "arc.autostart"


@dataclass
class AutostartDecision:
    """What the routine did (or, dry, would do): one line per experiment looked at."""

    started: list[str] = field(default_factory=list)
    chosen: list[str] = field(default_factory=list)  # passed every pre-check (dry: would start)
    blocked: dict[str, str] = field(default_factory=dict)  # experiment id -> reason
    skipped: str | None = None  # why nothing was looked at

    def lines(self) -> list[str]:
        if self.skipped:
            return [f"autostart: {self.skipped}"]
        out = [f"{x} started (pre-open auto-start)" for x in self.started]
        out += [f"{x} would start (all t0 checks pass)" for x in self.chosen]
        out += [f"{x} blocked: {why}" for x, why in self.blocked.items()]
        return out or ["autostart: nothing to start"]


def started_today(store: ExperimentStore, day: _dt.date) -> int:
    return sum(
        1
        for s in store.all()
        for e in s.events
        if e.status is ExperimentStatus.RUNNING
        and e.actor == AUTOSTART_ACTOR
        and e.at.date() == day
    )


def aa_calibration(store: ExperimentStore) -> ExperimentState | None:
    """The latest ``aa`` experiment that stopped with a recorded sigma (E10.4)."""
    best: tuple[int, ExperimentState] | None = None
    for s in store.all():
        if s.kind is not ExperimentKind.AA or s.stop is None or s.stop.sigma is None:
            continue
        sid = max(e.id for e in s.events if e.status is ExperimentStatus.STOPPED)
        if best is None or sid > best[0]:
            best = (sid, s)
    return None if best is None else best[1]


def merged_spec_hash(main: MainView, experiment_id: str) -> str | None:
    """sha256 of the spec on ``origin/main`` whose ``id`` is *experiment_id* (None: absent)."""
    for name, text in main.files(EXPERIMENTS_DIR).items():
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict) or str(data.get("id", "")).upper() != experiment_id:
            continue
        try:
            return spec_hash(ExperimentSpec.model_validate(data))
        except ValueError as exc:
            log.warning("experiments.autostart.bad_spec", path=name, error=str(exc))
            return "invalid"
    return None


def _verdict_on_main(main: MainView, experiment_id: str) -> bool:
    return f"{VERDICTS_DIR}/{experiment_id}.yaml" in main.files(VERDICTS_DIR)


def decide(
    store: ExperimentStore,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    halted: bool,
    session: bool,
    main: MainView,
) -> tuple[AutostartDecision, list[ExperimentState]]:
    """The pre-checks: ``(decision, the experiments to start in order)``.

    ``start_arms``'s own checks (capacity, accounts) run when starting, not here.
    """
    d = AutostartDecision()
    if not cfg.autostart.enabled:
        d.skipped = "off (experiments.autostart.enabled false)"
        return d, []
    queue = [
        s for s in store.all(status=ExperimentStatus.REGISTERED) if proposal_ref_of(s) is not None
    ]
    queue.sort(key=lambda s: max(e.id for e in s.events if e.status is s.status))
    if not queue:
        d.skipped = "no registered (adopted) experiment"
        return d, []
    room = cfg.autostart.max_per_day - started_today(store, now.date())
    common: str | None = None
    if not session:
        common = f"{now.date():%a %b %d} is not a trading session"
    elif halted:
        common = "trading is halted (`arc halt-status`)"
    elif room <= 0:
        common = f"max_per_day {cfg.autostart.max_per_day} already started today"
    aa = aa_calibration(store)
    out: list[ExperimentState] = []
    for st in queue:
        xid = st.experiment_id
        if common:
            d.blocked[xid] = common
            continue
        if st.kind is ExperimentKind.AB:
            if aa is None:
                d.blocked[xid] = (
                    "no A/A calibration yet (an aa experiment stopped with sigma, E15.9); "
                    "the auto-start never overrides it"
                )
                continue
            if not _verdict_on_main(main, aa.experiment_id):
                d.blocked[xid] = (
                    f"A/A {aa.experiment_id}'s calibration verdict is not committed on main "
                    f"({VERDICTS_DIR}/{aa.experiment_id}.yaml)"
                )
                continue
        merged = merged_spec_hash(main, xid)
        if merged is None:
            d.blocked[xid] = f"spec PR not merged: no {xid} spec on origin/main yet"
            continue
        if merged != st.registered_hash:
            d.blocked[xid] = (
                f"spec on main (sha256 {merged[:12]}) differs from the registered lock "
                f"({(st.registered_hash or '')[:12]})"
            )
            continue
        if len(out) >= room:
            d.blocked[xid] = f"max_per_day {cfg.autostart.max_per_day} reached"
            continue
        out.append(st)
    return d, out


def autostart(
    store: ExperimentStore,
    cfg: ExperimentsConfig,
    *,
    now: _dt.datetime,
    halted: bool,
    session: bool,
    main: MainView,
    start: Callable[[str], Any] | None,
) -> AutostartDecision:
    """Run :func:`decide`, then *start(experiment_id)* each chosen one (None = dry run).

    A start that raises (``ArmStartError`` / ``ExperimentError``: capacity, account not
    flat, ...) leaves the experiment registered and is reported as blocked.
    """
    from arc.experiments.runner import ArmStartError
    from arc.experiments.store import ExperimentError

    d, chosen = decide(store, cfg, now=now, halted=halted, session=session, main=main)
    for st in chosen:
        if start is None:
            d.chosen.append(st.experiment_id)
            continue
        try:
            start(st.experiment_id)
        except (ArmStartError, ExperimentError) as exc:
            d.blocked[st.experiment_id] = f"start refused: {exc}"
            continue
        d.started.append(st.experiment_id)
        log.info("experiments.autostarted", experiment_id=st.experiment_id)
    return d
