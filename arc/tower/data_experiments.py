"""Experiments page (E10.5, D44): ``/api/experiments`` and ``/api/experiments/{id}``.

Read-only. Every number is the latest stored E10.3
:class:`~arc.experiments.evaluate.ExperimentReport` (``experiment_reports``): the
same report the daily Slack line is rendered from and that
``arc experiment report <id> --stored --json`` prints. The tower never re-runs the
evaluation. The chart series (both arms' equity from t0, cumulative d_t with its
always-valid band) and the Slack-line text come from :mod:`arc.experiments.view`.
An experiment that has not been evaluated yet (draft, queued, registered, or
running before its first EOD evaluation) shows its spec and status only.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.experiments import view
from arc.experiments.evaluate import ExperimentReport, latest_report
from arc.experiments.models import (  # noqa: TC001 - pydantic resolves field annotations
    ExperimentEvent,
    ExperimentKind,
    ExperimentSpec,
    ExperimentState,
    ExperimentStatus,
    RunningDetail,
    StopDetail,
    StopReason,
)
from arc.experiments.store import ExperimentStore

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "ExperimentDetailResponse",
    "ExperimentListItem",
    "ExperimentsResponse",
    "load_experiment",
    "load_experiments",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ExperimentListItem(BaseModel):
    """One row of the list: status, area, sessions n/min/max, primary diff + CI, secondary."""

    model_config = _STRICT

    experiment_id: str
    title: str
    status: ExperimentStatus
    reason: StopReason | None = None
    area: str
    kind: ExperimentKind
    min_sessions: int | None
    max_sessions: int | None
    sessions: int | None = Field(None, description="Paired sessions in the latest report")
    primary_mean: float | None = Field(
        None, description="Mean paired daily P&L difference (fraction of t0 equity)"
    )
    primary_ci_lo: float | None = None
    primary_ci_hi: float | None = None
    ci_level: float | None = Field(None, description="Always-valid CI coverage, e.g. 0.95")
    primary_p: float | None = Field(None, description="Always-valid mSPRT p of no difference")
    secondary: view.SecondaryState | None = Field(
        None, description="Sortino non-inferiority: ok | not_shown | aa | pending"
    )
    sortino_control: float | None = None
    sortino_treatment: float | None = None
    sortino_delta: float | None = Field(None, description="Treatment − control Sortino")
    sortino_p: float | None = Field(
        None, description="Paired-bootstrap p (one-sided vs the margin; two-sided for aa)"
    )
    verdict: str | None = Field(None, description="Latest verdict: continue|win|futility|invalid")
    verdict_reason: str | None = None
    evaluated_at: _dt.datetime | None = None
    as_of_day: _dt.date | None = None
    line: str | None = Field(None, description="The daily [XP-n] Slack line, verbatim")


class ExperimentsResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    items: list[ExperimentListItem]


class ExperimentDetailResponse(BaseModel):
    """Everything on the detail page: the list row, spec + hashes, report and chart series."""

    model_config = _STRICT

    as_of: _dt.datetime
    experiment: ExperimentListItem
    spec: ExperimentSpec
    spec_hash: str
    registered_hash: str | None
    revision: int
    running: RunningDetail | None
    stop: StopDetail | None
    events: list[ExperimentEvent]
    report: ExperimentReport | None = Field(None, description="Latest stored E10.3 report")
    report_hash: str | None = Field(None, description="sha256 of the report's canonical JSON")
    curves: list[view.CurvePoint] = Field(default_factory=list)
    cumulative: list[view.CumulativePoint] = Field(default_factory=list)


def _item(st: ExperimentState, rep: ExperimentReport | None) -> ExperimentListItem:
    sp = st.spec
    base = {
        "experiment_id": st.experiment_id,
        "title": sp.title,
        "status": st.status,
        "reason": st.reason,
        "area": sp.area.value,
        "kind": sp.kind,
        "min_sessions": sp.min_sessions,
        "max_sessions": sp.max_sessions,
    }
    if rep is None:
        return ExperimentListItem(**base)
    ci = rep.primary.ci
    return ExperimentListItem(
        **base,
        sessions=rep.sessions,
        primary_mean=rep.primary.mean,
        primary_ci_lo=None if ci is None else ci.lo,
        primary_ci_hi=None if ci is None else ci.hi,
        ci_level=None if ci is None else ci.level,
        primary_p=rep.primary.p_value,
        secondary=view.secondary_state(rep),
        sortino_control=rep.secondary.sortino_control,
        sortino_treatment=rep.secondary.sortino_treatment,
        sortino_delta=view.sortino_delta(rep),
        sortino_p=rep.secondary.p_value,
        verdict=rep.verdict,
        verdict_reason=rep.verdict_reason,
        evaluated_at=rep.evaluated_at,
        as_of_day=rep.as_of_day,
        line=view.daily_line(rep),
    )


def load_experiments(conn: sqlite3.Connection, *, now: _dt.datetime) -> ExperimentsResponse:
    """Every experiment, newest first, each with its latest stored report's numbers."""
    store = ExperimentStore(conn, now=lambda: now)
    states = list(reversed(store.all()))
    return ExperimentsResponse(
        as_of=now, items=[_item(s, latest_report(conn, s.experiment_id)) for s in states]
    )


def load_experiment(
    conn: sqlite3.Connection, experiment_id: str, *, now: _dt.datetime
) -> ExperimentDetailResponse | None:
    """One experiment's detail page, or None when the id is unknown."""
    store = ExperimentStore(conn, now=lambda: now)
    st = store.get(experiment_id)
    if st is None:
        return None
    rep = latest_report(conn, experiment_id)
    return ExperimentDetailResponse(
        as_of=now,
        experiment=_item(st, rep),
        spec=st.spec,
        spec_hash=st.spec_hash,
        registered_hash=st.registered_hash,
        revision=st.revision,
        running=st.running,
        stop=st.stop,
        events=st.events,
        report=rep,
        report_hash=None if rep is None else rep.report_hash(),
        curves=[] if rep is None else view.curves(rep),
        cumulative=[] if rep is None else view.cumulative(rep, mde=st.spec.mde),
    )
