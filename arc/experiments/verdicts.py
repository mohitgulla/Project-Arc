"""Verdict file for every concluded experiment (E21.4, PLAN D86).

When ``experiments.evaluate`` or the owner's ``arc experiment stop`` concludes an XP, its
outcome is committed as ``config/experiments/live/verdicts/XP-<n>.yaml`` through an
auto-merged PR (branch ``xp/<n>-verdict``, :mod:`arc.experiments.publish`). The file
unlocks the spec's leaves for the strategy-lane check (E21.1); the store's queue
promotion (:meth:`ExperimentStore.stop`) already made the next queued experiment
``registered`` for the owner's manual start (D112).

``verdict`` per stop reason: ``win`` (plus ``winner: t<k>``), ``loss`` (harm),
``futility``, ``stopped`` (owner) and ``invalid`` (a failed A/A harness check). The
``report_hash`` is the latest stored E10.3 report's (``null`` when none was stored).

A published verdict is remembered in ``routine_state`` (``xp_verdict_pr:<id>``), so the
daily evaluation retries a failed publish and never opens a second PR.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
import yaml

from arc.experiments.models import ExperimentKind, ExperimentState, ExperimentStatus, StopReason
from arc.experiments.publish import VERDICTS_DIR

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

    from arc.experiments.evaluate import ExperimentReport
    from arc.experiments.publish import MainView, Publisher, PublishResult
    from arc.experiments.store import ExperimentStore

__all__ = [
    "STATE_VERDICT_PR",
    "VERDICT_OF",
    "publish_pending_verdicts",
    "publish_verdict",
    "verdict_doc",
    "verdict_file_text",
    "verdict_path",
    "verdict_pr_body",
    "winner_of",
]

log = structlog.get_logger(__name__)

STATE_VERDICT_PR = "xp_verdict_pr:{experiment_id}"
VERDICT_OF: dict[StopReason, str] = {
    StopReason.WIN: "win",
    StopReason.HARM: "loss",
    StopReason.FUTILITY: "futility",
    StopReason.OWNER: "stopped",
    StopReason.INVALID: "invalid",
}
_CONCLUDED = frozenset(
    {ExperimentStatus.STOPPED, ExperimentStatus.PROMOTED, ExperimentStatus.REJECTED}
)


def verdict_path(experiment_id: str) -> str:
    return f"{VERDICTS_DIR}/{experiment_id}.yaml"


def winner_of(report: ExperimentReport | None, st: ExperimentState) -> str:
    """The winning arm ``t<k>``: the headline arm when it won, else the first control
    pair whose verdict is ``win`` (a one-treatment experiment: ``t1``)."""
    if report is not None and report.pairwise:
        wins = [
            p.b for p in report.pairwise if p.kind == "control_vs_treatment" and p.verdict == "win"
        ]
        if report.headline_arm in wins:
            return str(report.headline_arm)
        if wins:
            return wins[0]
    return st.spec.arms.names[0]


def verdict_doc(st: ExperimentState, report: ExperimentReport | None) -> dict[str, Any]:
    """What the verdict file holds (the strategy-lane check reads id/verdict/winner)."""
    if st.reason is None:
        msg = f"{st.experiment_id} has no stop reason: not concluded"
        raise ValueError(msg)
    verdict = VERDICT_OF[st.reason]
    doc: dict[str, Any] = {
        "experiment_id": st.experiment_id,
        "verdict": verdict,
        "report_hash": None if report is None else report.report_hash(),
    }
    if verdict == "win":
        doc["winner"] = winner_of(report, st)
    doc["kind"] = st.kind.value
    doc["area"] = st.spec.area.value
    doc["spec_hash"] = st.registered_hash
    if st.spec.control_runs:
        # D108: a spec v3's verdict is Ti vs the control arm (never vs production)
        doc["reference"] = "control"
    if st.stop is not None:
        if st.stop.sessions is not None:
            doc["sessions"] = st.stop.sessions
        if st.kind is ExperimentKind.AA and st.stop.sigma is not None:
            doc["sigma"] = st.stop.sigma
    return doc


def verdict_file_text(st: ExperimentState, report: ExperimentReport | None) -> str:
    doc = verdict_doc(st, report)
    stopped = next((e for e in reversed(st.events) if e.status is ExperimentStatus.STOPPED), None)
    note = "" if st.stop is None or not st.stop.note else st.stop.note.replace("\n", " ")
    head = (
        f"# {st.experiment_id}: {doc['verdict']} ({st.spec.title}).\n"
        "# Written by arc.experiments.verdicts (E21.4, PLAN D86) when the experiment stopped"
        + (f" at {stopped.at.isoformat()} by {stopped.actor}" if stopped else "")
        + ".\n"
        + (f"# {note[:300]}\n" if note else "")
        + (
            f"# Promotion: a PR citing `Experiment: {st.experiment_id}` may set arm "
            f"{doc['winner']}'s values.\n"
            if doc["verdict"] == "win"
            else "# Not a promotion: this file only unlocks the spec's leaves (E21.1).\n"
        )
    )
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def verdict_pr_body(st: ExperimentState, doc: dict[str, Any]) -> str:
    win = f", winner `{doc['winner']}`" if doc["verdict"] == "win" else ""
    return (
        f"Lane: fast — {st.experiment_id} verdict `{doc['verdict']}` (PLAN D86)\n"
        "XP-advisory: none — verdict record only, no config change\n\n"
        f"{st.experiment_id} ({st.spec.title}) concluded: `{doc['verdict']}`{win}. "
        f"Report sha256 `{doc['report_hash']}`.\n\n"
        "Committing the verdict unlocks the spec's leaves for the strategy-lane check "
        "(E21.1). Auto-merged once CI is green (E21.4).\n"
    )


def publish_verdict(
    conn: sqlite3.Connection,
    store: ExperimentStore,
    experiment_id: str,
    publisher: Publisher,
    *,
    now: _dt.datetime,
    main: MainView | None = None,
) -> PublishResult | None:
    """Open (or find) the verdict PR of a concluded *experiment_id*; None if it is not
    concluded, was already published, or *main* already has a verdict file for it (a
    hand-committed one, e.g. the D80 history files, is never overwritten)."""
    from arc.experiments.evaluate import latest_report
    from arc.routines.runs import RoutineStateRepo

    state = RoutineStateRepo(conn)
    key = STATE_VERDICT_PR.format(experiment_id=experiment_id)
    st = store.require(experiment_id)
    if st.status not in _CONCLUDED or st.reason is None or state.get(key):
        return None
    path = verdict_path(experiment_id)
    if main is not None and path in main.files(VERDICTS_DIR):
        state.set(key, "on-main", now=now)
        return None
    report = latest_report(conn, experiment_id)
    doc = verdict_doc(st, report)
    pr = publisher.publish(
        branch=f"xp/{experiment_id.split('-')[1]}-verdict",
        files={path: verdict_file_text(st, report)},
        title=f"{experiment_id}: verdict {doc['verdict']} (D86)",
        body=verdict_pr_body(st, doc),
    )
    state.set(key, pr.url or pr.status, now=now)
    log.info(
        "experiments.verdict_published",
        experiment_id=experiment_id,
        verdict=doc["verdict"],
        url=pr.url,
        status=pr.status,
    )
    return pr


def publish_pending_verdicts(
    conn: sqlite3.Connection,
    store: ExperimentStore,
    publisher: Publisher,
    *,
    now: _dt.datetime,
    main: MainView | None = None,
) -> tuple[list[str], list[str]]:
    """Every concluded experiment without a published verdict: ``(lines, errors)``."""
    from arc.experiments.publish import PublishError

    lines: list[str] = []
    errors: list[str] = []
    for st in store.all():
        if st.status not in _CONCLUDED:
            continue
        try:
            pr = publish_verdict(conn, store, st.experiment_id, publisher, now=now, main=main)
        except PublishError as exc:
            errors.append(f"{st.experiment_id} verdict PR failed: {exc}")
            continue
        if pr is not None:
            lines.append(f"{st.experiment_id} verdict PR {pr.url or pr.status}")
    return lines, errors
