"""``[Experiments]`` posts in the #arc-investor day thread (PLAN D44, card E10.5).

Two shapes, both built from the stored E10.3
:class:`~arc.experiments.evaluate.ExperimentReport` and the shared
:mod:`arc.slack.blocks` layout (no hand-rolled Block Kit):

- :func:`experiment_line`: one line per running experiment after the daily
  evaluation, e.g.
  ``[Experiments] X-2 • exits • day 14/20–60 • Δ +0.08%/day [−0.03, +0.19] • Sortino ok``.
- :func:`experiment_stop_card`: one card when the evaluation stops an experiment
  (win, futility, invalid): header, summary line, a fact grid with the numbers,
  the verdict reason, and where to read the full report (the tower detail page
  when its address is known, and the ``arc experiment report`` command).

Every number is rendered by :mod:`arc.experiments.view`, the same helpers the
tower API uses, so Slack and the tower show the same values. Pure: no DB, no
client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.experiments import view
from arc.experiments.models import ExperimentKind
from arc.slack import blocks as B

if TYPE_CHECKING:
    from arc.experiments.evaluate import ArmSummary, ExperimentReport

__all__ = ["experiment_line", "experiment_stop_card", "report_command", "stop_title"]

_VERDICT_LABEL = {
    "win": "Win",
    "futility": "Futility",
    "invalid": "Invalid",
    "continue": "Continue",
}


def experiment_line(report: ExperimentReport) -> B.CardView:
    """The daily one-line status of a running experiment (text only, no blocks)."""
    return B.CardView(text=view.daily_line(report), blocks=[])


def report_command(experiment_id: str) -> str:
    return f"arc experiment report {experiment_id} --stored"


def stop_title(report: ExperimentReport) -> str:
    """``[Experiments] Stopped: X-2 • Win • 24 Sessions`` (header facts in Title Case)."""
    kind = " A/A" if report.kind is ExperimentKind.AA else ""
    return (
        f"{view.LINE_LABEL} Stopped: {report.experiment_id}{kind} • "
        f"{_VERDICT_LABEL[report.verdict]} • {report.sessions} Sessions"
    )


def _money(v: float) -> str:
    text = f"{v:+,.2f}".replace("-", view.MINUS)
    return text[0] + "$" + text[1:]


def _arm(a: ArmSummary) -> str:
    return "\n".join(
        [
            f"P&L {_money(a.total_pnl)}",
            f"Max drawdown {view.pct(-a.max_drawdown, sign=a.max_drawdown > 0)}",
            f"Worst day {view.pct(a.worst_day)}",
            f"Orders {a.orders} · fills {a.filled_executions}/{a.executions}",
        ]
    )


def _ratio(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}".replace("-", view.MINUS)


def experiment_stop_card(report: ExperimentReport, *, tower_url: str | None = None) -> B.CardView:
    """The card posted once when the daily evaluation stops an experiment.

    *tower_url* is the tower's base URL (``http://<tailscale-ip>:4174``) or None
    when it is not known; the CLI command is always shown.
    """
    r = report
    p, s = r.primary, r.secondary
    ci_level = f"{p.ci.level:.0%}" if p.ci else f"{1 - r.alpha:.0%}"
    primary = "\n".join(
        [
            f"{view.delta_text(r)}",
            f"Always-valid {ci_level} CI, % of t0 equity",
            f"Sigma {view.pct(p.sigma, 3, sign=False)} ({p.sigma_source or 'n/a'})",
        ]
    )
    sec_lines = [
        view.secondary_text(r),
        f"Control {_ratio(s.sortino_control)} · treatment {_ratio(s.sortino_treatment)}",
    ]
    if s.diff_ci is not None:
        sec_lines.append(f"Diff CI [{_ratio(s.diff_ci.lo)}, {_ratio(s.diff_ci.hi)}]")
    if s.margin is not None:
        sec_lines.append(f"Margin {s.margin}")
    cal = r.calibration
    mde = ", ".join(f"{n}: {view.pct(v, 3, sign=False)}" for n, v in cal.mde_fixed.items())
    pairs: list[tuple[str, str]] = [
        ("Primary (paired daily P&L)", primary),
        ("Secondary (Sortino)", "\n".join(sec_lines)),
    ]
    arms = {a.arm: a for a in r.arms}
    for name in ("control", "treatment"):
        if name in arms:
            pairs.append((name.capitalize(), _arm(arms[name])))
    pairs.append(
        (
            "Sessions",
            "\n".join(
                [
                    f"{view.progress_text(r)}",
                    f"t0 {r.t0:%b %-d} · equity {_money(r.t0_equity)[1:]}",
                    f"Legacy book {len(r.legacy_book)}",
                    f"Missing {len(r.missing_sessions)}",
                ]
            ),
        )
    )
    if cal.sigma is not None:
        pairs.append(
            (
                "Calibration",
                f"Sigma {view.pct(cal.sigma, 3, sign=False)}\nMDE {mde or 'n/a'}",
            )
        )
    where = [f"`{report_command(r.experiment_id)}`"]
    if tower_url:
        where.insert(0, f"<{tower_url.rstrip('/')}/experiments/{r.experiment_id}|Tower report>")
    out: list[B.Block] = [
        B.header(stop_title(r)),
        B.summary(
            f"Verdict {_VERDICT_LABEL[r.verdict]}",
            f"{r.area} {r.kind.value}",
            view.delta_text(r),
            view.secondary_text(r),
        ),
        *B.facts(pairs),
    ]
    reason = B.bullets("Verdict reason", [r.verdict_reason])
    if reason:
        out.append(reason)
    out.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Report*\n" + " · ".join(where)}}
    )
    out.append(
        B.footer(
            experiment=r.experiment_id,
            report=r.report_hash()[:12],
            spec=r.spec_hash[:12],
            evaluated=f"{r.evaluated_at:%Y-%m-%d %H:%M %Z}",
        )
    )
    text = " • ".join([stop_title(r), view.delta_text(r), view.secondary_text(r)])
    return B.CardView(text=text, blocks=out)
