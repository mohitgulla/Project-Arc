"""Experiment posts in the #arc-investor day thread (PLAN D44, card E10.5).

Two shapes, both built from the stored E10.3
:class:`~arc.experiments.evaluate.ExperimentReport` and the shared
:mod:`arc.slack.blocks` layout (no hand-rolled Block Kit):

- :func:`experiment_line`: one line per running experiment after the daily
  evaluation, e.g.
  ``[XP-2] Day 14 • P&L ∆ +0.08%/day (p 0.21) • Sortino ∆ +0.35 (p 0.04)``.
- :func:`experiment_stop_card`: one card when the evaluation stops an experiment
  (win, futility, invalid). Its header is the daily line with the verdict
  (``[XP-2] Day 25 • Win • P&L ∆ … • Sortino ∆ …``), then a fact grid with the
  two metrics in the same shape (∆, p, CI, control / treatment actuals, margin),
  the per-arm blocks, sessions and calibration, and the verdict reason as one
  plain sentence. Owner layout, 2026-10-03.

Every number is rendered by :mod:`arc.experiments.view`, the same helpers the
tower API uses, so Slack and the tower show the same values. Pure: no DB, no
client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.experiments import view
from arc.slack import blocks as B

if TYPE_CHECKING:
    from arc.experiments.evaluate import ArmSummary, ExperimentReport

__all__ = ["experiment_line", "experiment_stop_card", "stop_title", "verdict_sentence"]

_VERDICT_LABEL = {
    "win": "Win",
    "futility": "Futility",
    "invalid": "Invalid",
    "continue": "Continue",
}


def experiment_line(report: ExperimentReport) -> B.CardView:
    """The daily one-line status of a running experiment (text only, no blocks)."""
    return B.CardView(text=view.daily_line(report), blocks=[])


def stop_title(report: ExperimentReport) -> str:
    """``[XP-2] Day 25 • Win • P&L ∆ +0.08%/day (p 0.004) • Sortino ∆ +0.35 (p 0.01)``."""
    r = report
    return " • ".join(
        [
            f"{view.label(r)} {view.progress_text(r)}",
            _VERDICT_LABEL[r.verdict],
            view.delta_text(r),
            view.sortino_text(r),
        ]
    )


def _money(v: float) -> str:
    text = f"{v:+,.2f}".replace("-", view.MINUS)
    return text[0] + "$" + text[1:]


def _mean_pct(pnl: list[float], t0_equity: float) -> float | None:
    return sum(pnl) / len(pnl) / t0_equity if pnl and t0_equity > 0 else None


def _primary(r: ExperimentReport) -> str:
    p = r.primary
    level = f"{p.ci.level:.0%}" if p.ci else f"{1 - r.alpha:.0%}"
    c_mean = _mean_pct([s.control_pnl for s in r.series], r.t0_equity)
    t_mean = _mean_pct([s.treatment_pnl for s in r.series], r.t0_equity)
    sigma_src = {"aa": "A/A", "running_corrected": "running"}.get(p.sigma_source or "", "n/a")
    return "\n".join(
        [
            f"∆ {view.pct(p.mean)}/day",
            f"mSPRT p {view.p_text(p.p_value)}",
            f"{level} CI {view.ci_text(r)} (always-valid)",
            f"Control {view.pct(c_mean)}/day",
            f"Treatment {view.pct(t_mean)}/day",
            "Margin 0 (superiority)",
            f"Sigma {view.pct(p.sigma, 3, sign=False)} ({sigma_src})",
        ]
    )


def _secondary(r: ExperimentReport) -> str:
    s = r.secondary
    ci = s.diff_ci
    level = f"{ci.level:.0%}" if ci else f"{1 - 2 * r.alpha:.0%}"
    ci_txt = (
        "[n/a]"
        if ci is None
        else f"[{view.ratio(ci.lo, sign=True)}, {view.ratio(ci.hi, sign=True)}]"
    )
    margin = (
        "Margin n/a (A/A)"
        if s.margin is None
        else f"Margin {view.ratio(-s.margin, sign=True)} (non-inferiority)"
    )
    return "\n".join(
        [
            f"∆ {view.ratio(view.sortino_delta(r), sign=True)}",
            f"Bootstrap p {view.p_text(s.p_value)}",
            f"{level} CI {ci_txt} (paired bootstrap)",
            f"Control {view.ratio(s.sortino_control)}",
            f"Treatment {view.ratio(s.sortino_treatment)}",
            margin,
        ]
    )


def _arm(a: ArmSummary, t0_equity: float) -> str:
    ret = a.total_pnl / t0_equity if t0_equity > 0 else None
    slip = "n/a" if a.mean_slippage_bps is None else f"{a.mean_slippage_bps:.1f} bps"
    return "\n".join(
        [
            f"P&L {_money(a.total_pnl)} ({view.pct(ret)})",
            f"Max drawdown {view.pct(-a.max_drawdown, sign=a.max_drawdown > 0)}",
            f"Mean slippage {slip}",
            f"Worst day {view.pct(a.worst_day)}",
            f"Orders {a.orders}",
            f"Fills {a.filled_executions}/{a.executions}",
        ]
    )


def verdict_sentence(r: ExperimentReport) -> str:
    """One plain sentence on why the experiment stopped (owner: concise, no metric names).

    ``P&L ∆ CI [+0.26, +0.42] is above 0 and Sortino ∆ CI [+56.18, +371.20] clears
    the −0.50 margin after 25 sessions.``
    """
    p, s = r.primary, r.secondary
    pnl = f"P&L ∆ CI {view.ci_text(r)}" if p.ci else "P&L ∆ CI n/a"
    if s.diff_ci is not None:
        lo, hi = view.ratio(s.diff_ci.lo, sign=True), view.ratio(s.diff_ci.hi, sign=True)
        srt = f"Sortino ∆ CI [{lo}, {hi}]"
    else:
        srt = "Sortino ∆ CI n/a"
    n = f"after {r.sessions} sessions"
    margin = None if s.margin is None else view.ratio(-s.margin, sign=True)
    if r.verdict == "win":
        return f"{pnl} is above 0 and {srt} clears the {margin} margin {n}."
    if r.verdict == "invalid":
        return f"A/A arms differ: {pnl} excludes 0 {n}, so the harness needs checking."
    if r.kind.value == "aa":
        return f"A/A complete: {pnl} includes 0 {n}, as expected."
    if p.ci is not None and p.ci.lo > 0 and margin is not None:
        return (
            f"{pnl} is above 0 but {srt} did not clear the {margin} margin by session {r.sessions}."
        )
    where = "is below 0" if p.ci is not None and p.ci.hi < 0 else "still includes 0"
    return f"{pnl} {where} {n}, the maximum."


def experiment_stop_card(report: ExperimentReport) -> B.CardView:
    """The card posted once when the daily evaluation stops an experiment."""
    r = report
    pairs: list[tuple[str, str]] = [
        ("Paired Daily P&L", _primary(r)),
        ("Sortino Ratio", _secondary(r)),
    ]
    arms = {a.arm: a for a in r.arms}
    # control first, then every treatment arm (D69: t1..tK) in report order
    for name in ["control", *(a for a in arms if a != "control")]:
        if name in arms:
            pairs.append((name.capitalize(), _arm(arms[name], r.t0_equity)))
    pairs.append(
        (
            "Sessions",
            "\n".join(
                [
                    view.progress_text(r, bounds=True),
                    f"{r.t0:%b %-d} (t0) • Equity ${r.t0_equity:,.0f}",
                    f"Legacy book {len(r.legacy_book)}",
                    f"Missing {len(r.missing_sessions)}",
                ]
            ),
        )
    )
    cal = r.calibration
    if cal.sigma is not None:
        mde = ", ".join(f"{n}: {view.pct(v, 3, sign=False)}" for n, v in cal.mde_fixed.items())
        pairs.append(
            ("Calibration", f"Sigma {view.pct(cal.sigma, 3, sign=False)}\nMDE {mde or 'n/a'}")
        )
    title = stop_title(r)
    out: list[B.Block] = [
        B.header(title),
        B.summary(
            f"Area {r.area}",
            "A/A" if r.kind.value == "aa" else "A/B",
            f"Verdict {_VERDICT_LABEL[r.verdict]}",
        ),
        *B.facts(pairs),
    ]
    out.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Verdict reason*\n{B.esc(verdict_sentence(r))}"},
        }
    )
    out.append(
        B.footer(
            experiment=r.experiment_id,
            report=r.report_hash()[:12],
            spec=r.spec_hash[:12],
            evaluated=f"{r.evaluated_at:%Y-%m-%d %H:%M %Z}",
        )
    )
    return B.CardView(text=title, blocks=out)
