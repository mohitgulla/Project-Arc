"""What the owner sees of an experiment (PLAN D44, card E10.5): one view, two surfaces.

Pure: an :class:`~arc.experiments.evaluate.ExperimentReport` (plus the spec) in,
numbers and text out. The Slack daily line / stop card (:mod:`arc.slack.experiments`)
and the Tower API (:mod:`arc.tower.data_experiments`) both read these helpers, so
the two surfaces and ``arc experiment report --json`` always show the same numbers
(``tests/test_experiments_views.py`` pins the parity).

Formatting: percentages are of t0 equity, 2 dp, with a real minus sign (``−``)
like the rest of the Slack cards. The primary CI bounds are shown without a
``%`` sign: ``Δ +0.08%/day [−0.03, +0.19]``.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.experiments import stats
from arc.experiments.models import ExperimentKind

if TYPE_CHECKING:
    from arc.experiments.evaluate import ExperimentReport

__all__ = [
    "LINE_LABEL",
    "CumulativePoint",
    "CurvePoint",
    "SecondaryState",
    "ci_text",
    "cumulative",
    "curves",
    "daily_line",
    "delta_text",
    "pct",
    "progress_text",
    "secondary_state",
    "secondary_text",
]

LINE_LABEL = "[Experiments]"
MINUS = "\u2212"
_FORBID = ConfigDict(extra="forbid", frozen=True)

SecondaryState = Literal["ok", "not_shown", "aa", "pending"]


def pct(v: float | None, digits: int = 2, *, sign: bool = True, unit: str = "%") -> str:
    """A fraction as a percentage: ``0.0008`` -> ``+0.08%``; None -> ``n/a``."""
    if v is None:
        return "n/a"
    text = f"{v * 100:{'+' if sign else ''}.{digits}f}"
    return text.replace("-", MINUS) + unit


def ci_text(r: ExperimentReport) -> str:
    """``[−0.03, +0.19]`` (percent of t0 equity) or ``[n/a]`` before a CI exists."""
    ci = r.primary.ci
    if ci is None:
        return "[n/a]"
    return f"[{pct(ci.lo, unit='')}, {pct(ci.hi, unit='')}]"


def delta_text(r: ExperimentReport) -> str:
    """``Δ +0.08%/day [−0.03, +0.19]``: mean paired daily difference and its CI."""
    return f"Δ {pct(r.primary.mean)}/day {ci_text(r)}"


def progress_text(r: ExperimentReport) -> str:
    """``day 14/20–60``: paired sessions so far / the spec's min–max."""
    return f"day {r.sessions}/{r.min_sessions}–{r.max_sessions}"


def secondary_state(r: ExperimentReport) -> SecondaryState:
    """Sortino non-inferiority: ok, not shown (yet), A/A (no margin), pending (no data)."""
    if r.kind is ExperimentKind.AA or r.secondary.margin is None:
        return "aa"
    if r.secondary.non_inferior is None or r.secondary.diff_ci is None:
        return "pending"
    return "ok" if r.secondary.non_inferior else "not_shown"


_SECONDARY_TEXT: dict[SecondaryState, str] = {
    "ok": "Sortino ok",
    "not_shown": "Sortino not shown",
    "aa": "Sortino n/a (A/A)",
    "pending": "Sortino pending",
}


def secondary_text(r: ExperimentReport) -> str:
    return _SECONDARY_TEXT[secondary_state(r)]


def daily_line(r: ExperimentReport) -> str:
    """``[Experiments] X-2 • exits • day 14/20–60 • Δ +0.08%/day [−0.03, +0.19] • Sortino ok``."""
    area = r.area + (" A/A" if r.kind is ExperimentKind.AA else "")
    parts = [r.experiment_id, area, progress_text(r), delta_text(r), secondary_text(r)]
    return f"{LINE_LABEL} " + " • ".join(parts)


# ---------------------------------------------------------------------------
# Chart series (Tower detail)
# ---------------------------------------------------------------------------


class CurvePoint(BaseModel):
    """One point of both arms' equity curves from t0 (legacy book excluded)."""

    model_config = _FORBID

    day: _dt.date | None = Field(..., description="Session; None = t0 (both arms at t0 equity)")
    control: float = Field(..., description="t0 equity + control's cumulative P&L ($)")
    treatment: float = Field(..., description="t0 equity + treatment's cumulative P&L ($)")


class CumulativePoint(BaseModel):
    """Cumulative paired difference after *n* sessions, with its always-valid band.

    ``cum_d`` = sum of d_t so far; ``lo``/``hi`` = n x the always-valid CI on the
    mean after n sessions (the same :func:`stats.confidence_sequence` the
    evaluator runs), so the last point is the report's CI x sessions.
    """

    model_config = _FORBID

    day: _dt.date
    n: int
    cum_d: float
    lo: float | None
    hi: float | None


def curves(r: ExperimentReport) -> list[CurvePoint]:
    """Both arms from t0: the report's daily P&L accumulated on t0 equity."""
    c = t = r.t0_equity
    out = [CurvePoint(day=None, control=c, treatment=t)]
    for row in r.series:
        c += row.control_pnl
        t += row.treatment_pnl
        out.append(CurvePoint(day=row.day, control=c, treatment=t))
    return out


def cumulative(r: ExperimentReport, *, mde: float | None) -> list[CumulativePoint]:
    """Cumulative d_t with the always-valid band at every session (see the model).

    The band reuses the report's own inputs: the A/A sigma when the report used
    one (``sigma_source == "aa"``), else the running corrected sigma of the prefix;
    alpha, min_sessions and ``stats.sigma_upper_q`` from the report; *mde* from
    the spec.
    """
    known = r.primary.sigma if r.primary.sigma_source == "aa" else None
    upper_q = float(r.config.get("stats", {}).get("sigma_upper_q", 0.05))
    d = [row.d for row in r.series]
    out: list[CumulativePoint] = []
    total = 0.0
    for k, row in enumerate(r.series, start=1):
        total += row.d
        ci, _, _ = stats.confidence_sequence(
            d[:k],
            alpha=r.alpha,
            sigma=known,
            sigma_upper_q=upper_q,
            mde=mde,
            min_sessions=r.min_sessions,
        )
        out.append(
            CumulativePoint(
                day=row.day,
                n=k,
                cum_d=total,
                lo=None if ci is None else ci.lo * k,
                hi=None if ci is None else ci.hi * k,
            )
        )
    return out
