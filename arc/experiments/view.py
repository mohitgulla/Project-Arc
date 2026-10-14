"""What the owner sees of an experiment (PLAN D44, card E10.5): one view, two surfaces.

Pure: an :class:`~arc.experiments.evaluate.ExperimentReport` (plus the spec) in,
numbers and text out. The Slack daily line / stop card (:mod:`arc.slack.experiments`)
and the Tower API (:mod:`arc.tower.data_experiments`) both read these helpers, so
the two surfaces and ``arc experiment report --json`` always show the same numbers
(``tests/test_experiments_views.py`` pins the parity).

Formatting (owner, 2026-10-03): percentages are of t0 equity, 2 dp, with a real
minus sign (``−``). The stop header leads with the experiment id and shows each
metric's delta with its p-value:
``[XP-2] Day 25 • Win • P&L ∆ +0.08%/day (p 0.21) • Sortino ∆ +0.35 (p 0.04)``.
The daily status (D108, :func:`status_lines`) is a title, a headline and one
``Ti − C`` line per treatment in the same metric shape.
The primary p is the always-valid mSPRT p (dual of the CI); the Sortino p is the
paired-bootstrap p (one-sided non-inferiority against the margin).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.experiments import stats
from arc.experiments.models import ExperimentKind

if TYPE_CHECKING:
    from arc.experiments.evaluate import ExperimentReport

__all__ = [
    "CumulativePoint",
    "CurvePoint",
    "SecondaryState",
    "ci_text",
    "cumulative",
    "curves",
    "daily_line",
    "delta_text",
    "label",
    "p_text",
    "pct",
    "progress_text",
    "ratio",
    "secondary_state",
    "sortino_delta",
    "sortino_text",
]

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


def p_text(p: float | None) -> str:
    """``0.21`` / ``0.004`` / ``<0.001`` / ``n/a``."""
    if p is None:
        return "n/a"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}" if p < 0.01 else f"{p:.2f}"


def ratio(v: float | None, *, sign: bool = False) -> str:
    """``1.20`` / ``+0.35`` / ``−0.10`` / ``n/a`` (Sortino values and differences)."""
    if v is None:
        return "n/a"
    return f"{v:{'+' if sign else ''}.2f}".replace("-", MINUS)


def sortino_delta(r: ExperimentReport) -> float | None:
    """Treatment − control Sortino (the bootstrap CI's point estimate)."""
    s = r.secondary
    if s.diff_ci is not None:
        return s.diff_ci.estimate
    if s.sortino_control is None or s.sortino_treatment is None:
        return None
    return s.sortino_treatment - s.sortino_control


def delta_text(r: ExperimentReport) -> str:
    """``P&L ∆ +0.08%/day (p 0.21)``: mean paired daily difference and its mSPRT p."""
    mean = "n/a" if r.primary.mean is None else f"{pct(r.primary.mean)}/day"
    return f"P&L ∆ {mean} (p {p_text(r.primary.p_value)})"


def sortino_text(r: ExperimentReport) -> str:
    """``Sortino ∆ +0.35 (p 0.04)``: treatment − control and its bootstrap p."""
    return f"Sortino ∆ {ratio(sortino_delta(r), sign=True)} (p {p_text(r.secondary.p_value)})"


def progress_text(r: ExperimentReport, *, bounds: bool = False) -> str:
    """``Day 14`` (daily line) or ``Day 14/20–60`` (paired sessions / the spec's min–max)."""
    if not bounds:
        return f"Day {r.sessions}"
    return f"Day {r.sessions}/{r.min_sessions}–{r.max_sessions}"


def label(r: ExperimentReport) -> str:
    """``[XP-2]``, or ``[XP-1] A/A`` for an A/A run."""
    return f"[{r.experiment_id}]" + (" A/A" if r.kind is ExperimentKind.AA else "")


def secondary_state(r: ExperimentReport) -> SecondaryState:
    """Sortino non-inferiority: ok, not shown (yet), A/A (no margin), pending (no data)."""
    if r.kind is ExperimentKind.AA or r.secondary.margin is None:
        return "aa"
    if r.secondary.non_inferior is None or r.secondary.diff_ci is None:
        return "pending"
    return "ok" if r.secondary.non_inferior else "not_shown"


def daily_line(r: ExperimentReport) -> str:
    """``[XP-2] Day 14 • P&L ∆ +0.08%/day (p 0.21) • Sortino ∆ +0.35 (p 0.04)``."""
    return " • ".join([f"{label(r)} {progress_text(r)}", delta_text(r), sortino_text(r)])


# ---------------------------------------------------------------------------
# D108 (E25.6): the multi-line daily status, Ti − C per treatment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmGap:
    """One treatment's gap to the verdict's reference (control from spec v3)."""

    arm: str  # display name, ``T1``
    mean: float | None
    ci: stats.Interval | None
    p_value: float | None
    holm: bool  # p_value is Holm-adjusted across the K treatments
    sortino: float | None
    sortino_p: float | None
    verdict: str | None


def arm_short(arm: str) -> str:
    """``control`` -> ``Control``; ``treatment`` (v1) -> ``T1``; ``t3`` -> ``T3``."""
    if arm == "treatment":
        return "T1"
    if len(arm) > 1 and arm[0] in "tT" and arm[1:].isdigit():
        return arm.upper()
    return arm.replace("_", " ").title()


def reference_name(r: ExperimentReport) -> str:
    """What the verdict compares against: ``control`` (spec v3, D108) or ``production``
    (spec v1/v2, whose control series was the production account)."""
    return "control" if r.reference == "control" else "production"


def _ref_letter(r: ExperimentReport) -> str:
    return "C" if r.reference == "control" else "P"


def arm_gaps(r: ExperimentReport) -> list[ArmGap]:
    """Every treatment vs the reference, in arm order (v1: the single treatment)."""
    if r.pairwise is None:
        t = next((a.arm for a in r.arms if a.arm != "control"), "treatment")
        return [
            ArmGap(
                arm=arm_short(t),
                mean=r.primary.mean,
                ci=r.primary.ci,
                p_value=r.primary.p_value,
                holm=False,
                sortino=sortino_delta(r),
                sortino_p=r.secondary.p_value,
                verdict=r.verdict,
            )
        ]
    out: list[ArmGap] = []
    for p in r.pairwise:
        if p.kind != "control_vs_treatment":
            continue
        s = p.secondary
        sd: float | None = None
        if s is not None:
            if s.diff_ci is not None:
                sd = s.diff_ci.estimate
            elif s.sortino_control is not None and s.sortino_treatment is not None:
                sd = s.sortino_treatment - s.sortino_control
        holm = p.holm_p is not None
        out.append(
            ArmGap(
                arm=arm_short(p.b),
                mean=p.mean,
                ci=p.ci,
                p_value=p.holm_p if holm else p.p_value,
                holm=holm,
                sortino=sd,
                sortino_p=None if s is None else s.p_value,
                verdict=p.verdict,
            )
        )
    return out


def _p_clause(g: ArmGap) -> str:
    return f"p {p_text(g.p_value)}" + (" Holm" if g.holm else "")


def gap_line(r: ExperimentReport, g: ArmGap) -> str:
    """``T2 − C • P&L ∆ +0.08%/day (p 0.21 Holm) • Sortino ∆ +0.35 (p 0.04)``."""
    mean = "n/a" if g.mean is None else f"{pct(g.mean)}/day"
    return " • ".join(
        [
            f"{g.arm} {MINUS} {_ref_letter(r)}",
            f"P&L ∆ {mean} ({_p_clause(g)})",
            f"Sortino ∆ {ratio(g.sortino, sign=True)} (p {p_text(g.sortino_p)})",
        ]
    )


def status_title(r: ExperimentReport) -> str:
    """``[XP-12] Day 14/60`` (``[XP-12] A/A Day 14/60``): paired sessions / the spec's max."""
    return f"{label(r)} Day {r.sessions}/{r.max_sessions}"


def _names(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _within_noise(g: ArmGap) -> bool:
    return g.ci is None or g.ci.lo <= 0 <= g.ci.hi


def status_headline(r: ExperimentReport) -> str:
    """One sentence on where the treatments stand against the reference (D108, D109).

    Deterministic from the stored report: decided arms first (beats / trails / no
    difference), else the undecided arm with the largest gap (leads / trails), then the
    rest (within noise, or ahead / behind so far), ending ``after N sessions``. Up to
    ``RECAP_CHARS`` (2x a loop headline); never a sleeve stat (D109).
    """
    from arc.routines.headline import RECAP_CHARS, headline_points

    ref, n = reference_name(r), r.sessions
    gaps = arm_gaps(r)
    after = f"after {n} session{'' if n == 1 else 's'}"
    known = [g for g in gaps if g.mean is not None]
    if not known:
        main = "Waiting for the first paired session" if n == 0 else "No range yet " + after
        return headline_points(main, limit=RECAP_CHARS)
    points: list[str] = []
    if r.verdict == "invalid":
        worst = min(known, key=lambda g: g.p_value if g.p_value is not None else 1.0)
        points.append(
            f"A/A check failed: {worst.arm} differs from {ref} by {pct(worst.mean)}/day"
            f" ({_p_clause(worst)})"
        )
    for g in known:
        if g.verdict == "win":
            points.append(f"{g.arm} beats {ref} by {pct(g.mean)}/day ({_p_clause(g)})")
        elif g.verdict == "loss":
            gap = pct(abs(g.mean or 0.0), sign=False)
            points.append(f"{g.arm} trails {ref} by {gap}/day ({_p_clause(g)})")
        elif g.verdict == "futility":
            points.append(f"{g.arm} shows no difference from {ref}")
    open_ = [g for g in known if g.verdict not in ("win", "loss", "futility")]
    if open_ and r.verdict != "invalid":
        lead = max(open_, key=lambda g: abs(g.mean or 0.0))
        mean = lead.mean or 0.0
        if mean > 0:
            points.append(f"{lead.arm} leads {ref} by {pct(mean)}/day ({_p_clause(lead)})")
        elif mean < 0:
            gap = pct(abs(mean), sign=False)
            points.append(f"{lead.arm} trails {ref} by {gap}/day ({_p_clause(lead)})")
        else:
            points.append(f"{lead.arm} is level with {ref}")
        rest = [g for g in open_ if g is not lead]
        noise = [g.arm for g in rest if _within_noise(g)]
        for g in rest:
            if not _within_noise(g):
                side = "ahead" if (g.mean or 0.0) > 0 else "behind"
                points.append(f"{g.arm} {side} so far ({pct(g.mean)}/day)")
        if noise:
            points.append(f"{_names(noise)} within noise")
    missing = [g.arm for g in gaps if g.mean is None]
    if missing:
        points.append(f"{_names(missing)} without a range yet")
    # the session count always closes the sentence: points that would push it past the
    # limit are skipped, not the count
    text = headline_points(points[0], *points[1:], limit=RECAP_CHARS - len(after) - 1)
    return f"{text.rstrip('.…')} {after}."


def status_lines(r: ExperimentReport) -> list[str]:
    """The daily status (plain text): title, headline, then one ``Ti − C`` line per arm."""
    return [status_title(r), status_headline(r), *(gap_line(r, g) for g in arm_gaps(r))]


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
