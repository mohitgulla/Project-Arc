"""``experiments.evaluate`` routine (E10.3, D44): the daily experiment evaluation.

``personas.experiments.evaluate`` in ``config/routines.yaml``: trading days after
the Broker's EOD reconcile (which writes the per-arm ``pnl_snapshots`` it reads).
Deterministic, no LLM, no broker. For every running experiment it stores an
:class:`~arc.experiments.evaluate.ExperimentReport` and applies the verdict
(stop on win / futility / invalid).

E10.5: the day thread gets one ``[XP-n] Day …`` line per experiment still running
after the evaluation, and one stop card per experiment the
evaluation stopped (:mod:`arc.slack.experiments`), each its own post. The stop
card is the alert for an ``invalid`` A/A too (no separate notice: the same state
is never posted twice in one thread). The routine's own ``[Routines]`` summary is
folded (``notify: quiet``).

E21.4 (D86): after the evaluation, every concluded experiment without a published
verdict gets its ``verdicts/XP-<n>.yaml`` PR (auto-merged; :mod:`arc.experiments.verdicts`),
on a live tick only. ``experiments.autostart`` (09:05 ET) starts the oldest registered
adopted experiment once its t0 checks pass (:mod:`arc.experiments.autostart`) and posts
started / blocked with the reason once per day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Callable

    from arc.experiments.evaluate import ExperimentReport
    from arc.routines.handlers import JobContext
    from arc.slack.blocks import CardView

__all__ = [
    "autostart_preview",
    "experiment_posts",
    "experiments_autostart_step",
    "experiments_evaluate_step",
]

log = structlog.get_logger(__name__)


def experiment_posts(reports: list[ExperimentReport]) -> list[CardView]:
    """A line per still-running experiment, then a card per stopped one (report order)."""
    from arc.slack.experiments import experiment_line, experiment_stop_card

    lines = [experiment_line(r) for r in reports if r.verdict == "continue"]
    stops = [experiment_stop_card(r) for r in reports if r.verdict != "continue"]
    return [*lines, *stops]


def experiments_evaluate_step(ctx: JobContext) -> JobResult:
    from arc.control.effective import experiments_config
    from arc.experiments.evaluate import evaluate_running
    from arc.experiments.store import ExperimentStore

    cfg = experiments_config(ctx.settings)
    # D69: stops promote queued experiments while their arms fit max_parallel_arms
    store = ExperimentStore.for_runner(ctx.conn, cfg.runner, now=lambda: ctx.now)
    reports = evaluate_running(store, cfg, now=ctx.now, run_id=ctx.run_id)
    for r in reports:
        ctx.record_input(
            f"experiment:{r.experiment_id}",
            "audit_store",
            r.model_dump(mode="json", exclude={"evaluated_at", "evaluator_sha"}),
            as_of=ctx.now,
            count=r.sessions,
        )
    posts = experiment_posts(reports)
    verdict_lines: list[str] = []
    verdict_errors: list[str] = []
    if _live(ctx):  # D86: the verdict file of every concluded experiment (stops above + owner)
        from arc.experiments.publish import GitMainView, GitPublisher
        from arc.experiments.verdicts import publish_pending_verdicts

        verdict_lines, verdict_errors = publish_pending_verdicts(
            ctx.conn, store, GitPublisher(), now=ctx.now, main=GitMainView()
        )
    parts = [
        f"{r.experiment_id} {r.verdict} n={r.sessions}"
        + (
            f" Δ {r.primary.ci.estimate:+.3%}/day [{r.primary.ci.lo:+.3%}, {r.primary.ci.hi:+.3%}]"
            if r.primary.ci
            else ""
        )
        for r in reports
    ]
    parts += verdict_lines + verdict_errors
    return JobResult(
        summary="; ".join(parts) if parts else "no running experiments",
        notice="; ".join(verdict_errors),
        extra_cards=posts,
        metrics={
            "experiments": len(reports),
            "stopped": sum(r.verdict != "continue" for r in reports),
            **{f"verdict_{r.experiment_id}": r.verdict for r in reports},
        },
    )


def _live(ctx: JobContext) -> bool:
    """Side effects outside the store (git, GitHub, broker) only on a live tick."""
    env = getattr(ctx, "run_env", None)
    return bool(env is not None and env.slack)


def _halted(conn: sqlite3.Connection) -> bool:
    from arc.gate.halt import HaltSwitch
    from arc.store.repos import HaltRepo

    try:
        return HaltSwitch(HaltRepo(conn)).opens_blocked()  # any halt stops new arms
    except Exception:  # noqa: BLE001 - fails closed, like the dispatcher
        return True


def experiments_autostart_step(ctx: JobContext) -> JobResult:
    """E21.4: start the oldest registered adopted experiment (t0 checks first)."""
    from arc.control.effective import experiments_config
    from arc.experiments.autostart import autostart
    from arc.experiments.publish import GitMainView, PublishError
    from arc.experiments.store import ExperimentStore
    from arc.utils.calendar import is_session

    cfg = experiments_config(ctx.settings)
    store = ExperimentStore.for_runner(ctx.conn, cfg.runner, now=lambda: ctx.now)
    start = _live_start(ctx) if _live(ctx) else None
    try:
        d = autostart(
            store,
            cfg,
            now=ctx.now,
            halted=_halted(ctx.conn),
            session=is_session(ctx.now.date()),
            main=GitMainView(fetch=_live(ctx)),
            start=start,
        )
    except PublishError as exc:  # git could not read origin/main: nothing started
        return JobResult(summary=f"autostart: origin/main unreadable: {exc}", notice=str(exc))
    lines = d.lines()
    # once per day: the routine runs once (09:05 + ttl); quiet unless it looked at one
    notice = "" if d.skipped else "; ".join(lines)
    return JobResult(
        summary="; ".join(lines),
        notice=notice,
        metrics={"started": len(d.started), "blocked": len(d.blocked)},
    )


def _live_start(ctx: JobContext) -> Callable[[str], object]:
    """``arc experiment start`` (live): control's equity, the arm account probe."""
    from arc.experiments.autostart import AUTOSTART_ACTOR
    from arc.experiments.broker import trading_broker
    from arc.experiments.runner import live_account_probe, runner_config, start_arms

    def start(experiment_id: str) -> object:
        return start_arms(
            ctx.conn,
            experiment_id,
            actor=AUTOSTART_ACTOR,
            now=ctx.now,
            t0_equity=trading_broker(ctx.conn).account().equity,
            runner=runner_config(ctx.conn),
            routines_path=ctx.run_env.config_path,
            probe=live_account_probe(),
        )

    return start


def autostart_preview(conn: sqlite3.Connection, now: _dt.datetime) -> str:
    """``tick --dry-run``: what ``experiments.autostart`` would do at *now* (read-only;
    origin/main as last fetched, no broker checks)."""
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.autostart import autostart
    from arc.experiments.publish import GitMainView
    from arc.experiments.store import ExperimentStore
    from arc.utils.calendar import is_session

    cfg = experiments_config(effective_settings(conn))
    store = ExperimentStore.for_runner(conn, cfg.runner, now=lambda: now)
    d = autostart(
        store,
        cfg,
        now=now,
        halted=_halted(conn),
        session=is_session(now.date()),
        main=GitMainView(fetch=False),
        start=None,
    )
    return "; ".join(d.lines())
