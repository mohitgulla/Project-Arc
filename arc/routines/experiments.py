"""``experiments.evaluate`` routine (E10.3, D44): the daily experiment evaluation.

``personas.experiments.evaluate`` in ``config/routines.yaml``: trading days after
the Auditor's EOD reconcile (which writes the per-arm ``pnl_snapshots`` it reads).
Deterministic, no LLM, no broker. For every running experiment it stores an
:class:`~arc.experiments.evaluate.ExperimentReport` and applies the verdict
(stop on win / futility / invalid).

E10.5: the day thread gets one ``[Experiments]`` line per experiment still running
after the evaluation, and one ``[Experiments] Stopped`` card per experiment the
evaluation stopped (:mod:`arc.slack.experiments`), each its own post. The stop
card is the alert for an ``invalid`` A/A too (no separate notice: the same state
is never posted twice in one thread). The routine's own ``[Routines]`` summary is
folded (``notify: quiet``).

The stop card links the tower's detail page when the tower address is known: the
``tower_url`` handler option, else ``http://<tailscale-ip>:<tower_port>`` when
``monitoring.remote_access`` is enabled (the address the tower binds to).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    from arc.experiments.evaluate import ExperimentReport
    from arc.routines.handlers import JobContext
    from arc.slack.blocks import CardView

__all__ = ["experiments_evaluate_step", "experiment_posts", "tower_url"]

log = structlog.get_logger(__name__)


def tower_url(ctx: JobContext) -> str | None:
    """The tower's base URL for report links, or None when it is not known."""
    opt = ctx.options.get("tower_url")
    if opt:
        return str(opt)
    ra = ctx.routines.monitoring.remote_access
    if not ra.enabled:
        return None
    from arc.tower.net import NoTailscaleAddressError, resolve_bind_address

    try:
        return f"http://{resolve_bind_address()}:{ra.tower_port}"
    except NoTailscaleAddressError:
        log.info("experiments.tower_url_unknown")
        return None


def experiment_posts(
    reports: list[ExperimentReport], *, tower: str | None = None
) -> list[CardView]:
    """A line per still-running experiment, then a card per stopped one (report order)."""
    from arc.slack.experiments import experiment_line, experiment_stop_card

    lines = [experiment_line(r) for r in reports if r.verdict == "continue"]
    stops = [experiment_stop_card(r, tower_url=tower) for r in reports if r.verdict != "continue"]
    return [*lines, *stops]


def experiments_evaluate_step(ctx: JobContext) -> JobResult:
    from arc.control.effective import experiments_config
    from arc.experiments.evaluate import evaluate_running
    from arc.experiments.store import ExperimentStore

    cfg = experiments_config(ctx.settings)
    store = ExperimentStore(ctx.conn, now=lambda: ctx.now)
    reports = evaluate_running(store, cfg, now=ctx.now, run_id=ctx.run_id)
    for r in reports:
        ctx.record_input(
            f"experiment:{r.experiment_id}",
            "audit_store",
            r.model_dump(mode="json", exclude={"evaluated_at", "evaluator_sha"}),
            as_of=ctx.now,
            count=r.sessions,
        )
    stopped = any(r.verdict != "continue" for r in reports)
    posts = experiment_posts(reports, tower=tower_url(ctx) if stopped else None)
    parts = [
        f"{r.experiment_id} {r.verdict} n={r.sessions}"
        + (
            f" Δ {r.primary.ci.estimate:+.3%}/day [{r.primary.ci.lo:+.3%}, {r.primary.ci.hi:+.3%}]"
            if r.primary.ci
            else ""
        )
        for r in reports
    ]
    return JobResult(
        summary="; ".join(parts) if parts else "no running experiments",
        extra_cards=posts,
        metrics={
            "experiments": len(reports),
            "stopped": sum(r.verdict != "continue" for r in reports),
            **{f"verdict_{r.experiment_id}": r.verdict for r in reports},
        },
    )
