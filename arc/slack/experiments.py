"""Experiment posts in the #arc-investor day thread (PLAN D44, card E10.5).

Two shapes, both built from the stored E10.3
:class:`~arc.experiments.evaluate.ExperimentReport` and the shared
:mod:`arc.slack.blocks` layout (no hand-rolled Block Kit):

- :func:`experiment_line`: the daily status of each running experiment (D108,
  E25.6): ``*:microscope: [XP-12] Day 14/60*`` (bold), a blank line, then a bold-italic
  one-sentence headline (where the treatments stand against the reference,
  ≤ ``RECAP_MAX``). No per-arm ``Ti − C`` lines: the arm detail is on the Tower's
  Experiments page (owner 2026-10-16). Also sent to #arc-investor (D65). No
  production or sleeve stats (D109).
- :func:`experiment_stop_card`: one card when the evaluation stops an experiment
  (win, futility, invalid). Its header is the daily line with the verdict
  (``[XP-2] Day 25 • Win • P&L ∆ … • Sortino ∆ …``), then a fact grid with the
  two metrics in the same shape (∆, p, CI, control / treatment actuals, margin),
  the per-arm blocks (Control, T1..T3; "Production" for a v1/v2 control series),
  a Production block for spec v3 (P − C = start book + residual), sessions and
  calibration, and the verdict reason as one plain sentence. Owner layout,
  2026-10-03; headline, names and the Production block per D108 (E25.6).

Every number is rendered by :mod:`arc.experiments.view`, the same helpers the
tower API uses, so Slack and the tower show the same values. Pure: no DB, no
client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.experiments import view
from arc.slack import blocks as B
from arc.slack.loop import RECAP_MAX, bold_italic

if TYPE_CHECKING:
    from arc.experiments.evaluate import ArmSummary, ExperimentReport

__all__ = ["experiment_line", "experiment_stop_card", "stop_title", "verdict_sentence"]

STATUS_EMOJI = ":microscope:"  # daily status title (owner 2026-10-16)

_VERDICT_LABEL = {
    "win": "Win",
    "futility": "Futility",
    "invalid": "Invalid",
    "continue": "Continue",
}


def experiment_line(report: ExperimentReport) -> B.CardView:
    """The daily status of a running experiment (D108, E25.6; text only, no blocks).

    ``*:microscope: [XP-12] Day 14/60*`` (bold), a blank line, then a bold-italic one-sentence
    headline (≤ ``RECAP_MAX`` = 300 chars, D109). The per-arm ``Ti − C`` lines are not
    posted (owner 2026-10-16): the Tower's Experiments page carries them. D119: a
    pre-registered adjusted spec missing a SPY return adds one "verdict on hold" line.
    No production or sleeve stats. Posted in the day thread and also sent to
    #arc-investor (D65).
    """
    r = report
    lines = [
        f"*{STATUS_EMOJI} {B.esc(view.status_title(r))}*",
        "",
        bold_italic(view.status_headline(r), RECAP_MAX),
    ]
    if r.covariate_missing:  # D119: the pre-registered adjusted verdict waits on SPY
        days = ", ".join(d.strftime("%b %-d") for d in r.covariate_missing[:5])
        more = len(r.covariate_missing) - 5
        lines.append(
            f"_Verdict on hold: no SPY return yet for {days}"
            + (f" (+{more} more)" if more > 0 else "")
            + "_"
        )
    return B.CardView(text="\n".join(lines), blocks=[], broadcast=True)


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


def _production(r: ExperimentReport) -> str | None:
    """D108 Production block (spec v3): P total and P − C = start book + residual."""
    pp = r.production
    if pp is None:
        return None
    t0 = r.t0_equity
    ret = pp.cum_p_pnl / t0 if t0 > 0 else None
    book = pp.start_book
    held = book.days[-1].open if book.days else len(book.ids)
    return "\n".join(
        [
            f"P&L {_money(pp.cum_p_pnl)} ({view.pct(ret)})",
            f"P {view.MINUS} C {view.pct(pp.cum_d_p_c)}",
            f"= start book {view.pct(pp.cum_start_book)}",
            f"+ residual {view.pct(pp.cum_residual)}",
            f"Start book {held}/{len(book.ids)} open",
        ]
    )


def _arm_title(r: ExperimentReport, name: str) -> str:
    """``Control`` (the spec v3 control arm), ``Production`` (v1/v2's reference, which was
    the production account, D108 naming), or the treatment's short name ``T2``."""
    if name == "control":
        return "Control" if r.reference == "control" else "Production"
    return view.arm_short(name)


def experiment_stop_card(report: ExperimentReport) -> B.CardView:
    """The card posted once when the daily evaluation stops an experiment.

    D108 (E25.6): a bold-italic headline under the title, arm blocks Control, T1..T3,
    a Production block (spec v3), ``Start book`` (was ``Legacy book``); also sent to
    #arc-investor (D65).
    """
    r = report
    pairs: list[tuple[str, str]] = [
        ("Paired Daily P&L", _primary(r)),
        ("Sortino Ratio", _secondary(r)),
    ]
    arms = {a.arm: a for a in r.arms}
    # control first, then every treatment arm (D69: t1..tK) in report order
    for name in ["control", *(a for a in arms if a != "control")]:
        if name in arms:
            pairs.append((_arm_title(r, name), _arm(arms[name], r.t0_equity)))
    prod = _production(r)
    if prod is not None:
        pairs.append(("Production", prod))
    book = len(r.production.start_book.ids) if r.production is not None else len(r.legacy_book)
    pairs.append(
        (
            "Sessions",
            "\n".join(
                [
                    view.progress_text(r, bounds=True),
                    f"{r.t0:%b %-d} (t0) • Equity ${r.t0_equity:,.0f}",
                    f"Start book {book}",
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
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": bold_italic(view.status_headline(r), RECAP_MAX)},
        },
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
    return B.CardView(text=title, blocks=out, broadcast=True)
