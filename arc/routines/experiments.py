"""``experiments.evaluate`` routine (E10.3, D44): the daily experiment evaluation.

``personas.experiments.evaluate`` in ``config/routines.yaml``: trading days after
the Auditor's EOD reconcile (which writes the per-arm ``pnl_snapshots`` it reads).
Deterministic, no LLM, no broker. For every running experiment it stores an
:class:`~arc.experiments.evaluate.ExperimentReport` and applies the verdict
(stop on win / futility / invalid). An invalid stop is posted as an immediate
notice; the per-experiment Slack line and stop card are E10.5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    from arc.routines.handlers import JobContext

__all__ = ["experiments_evaluate_step"]

log = structlog.get_logger(__name__)


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
    alerts = [
        f"[Experiments] {r.experiment_id} stopped ({r.verdict}): {r.verdict_reason}"
        for r in reports
        if r.verdict == "invalid"
    ]
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
        notice="\n".join(alerts),
        metrics={
            "experiments": len(reports),
            "stopped": sum(r.verdict != "continue" for r in reports),
            **{f"verdict_{r.experiment_id}": r.verdict for r in reports},
        },
    )
