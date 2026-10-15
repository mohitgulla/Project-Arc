"""A/A calibration report as Markdown (PLAN D44, card E10.4).

``arc experiment report XP-1 --format md [--out docs/RESEARCH/experiments/XP-1-aa.md]``
renders one stored or freshly computed :class:`~arc.experiments.evaluate.ExperimentReport`
for the owner. Pure: every number comes from the report (E10.3's ``Calibration``,
``Primary`` and ``ArmSummary``), formatted with :mod:`arc.experiments.view`, so the
Markdown, the Slack card, the Tower and ``--json`` never disagree. Nothing here
computes a statistic.

What an A/A measures (owner, 2026-10-03): sigma of the paired daily difference d_t,
the achievable MDE, the slippage / fill-rate gap between the two paper accounts,
the LLM-divergence rate, and per-arm drawdown / worst day / orders (informational).
alpha 0.05 and the 20/60 session window are fixed owner policy (D44): the report
states them and never recommends a value for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc.experiments import view
from arc.experiments.models import ExperimentKind

if TYPE_CHECKING:
    from arc.experiments.evaluate import ArmSummary, ExperimentReport

__all__ = ["aa_document", "calibration_markdown"]

DOC_PATH = "docs/RESEARCH/experiments/XP-1-aa.md"
PENDING_HEADER = "Status: **pending live run (E15.9)**."


def aa_document(r: ExperimentReport, *, example: bool = False) -> str:
    """The whole ``XP-<n>-aa.md`` document for *r*.

    ``example=True`` (the committed template, E10.4): the status header says the
    live run is pending and the body is labelled as a fixture dry run. E15.9 writes
    the live report of XP-1 (the K=3 A/A, PLAN D111) with ``arc experiment report XP-1
    --stored --format md --out``.
    """
    head = [f"# {r.experiment_id} A/A calibration", ""]
    if example:
        head += [
            PENDING_HEADER + " XP-1 is the K=3 calibration A/A (control arm plus "
            "t1–t3, every arm flat on the shared experiment account; "
            "`config/experiments/live/xp1_aa_k3.yaml`, PLAN D108/D111) and has not run "
            "yet. The section below is the **fixture dry run** of the v1 two-account "
            "layout (synthetic sessions on scratch stores, no orders); E15.9 replaces "
            "this file with the live report:",
            "",
            f"    arc experiment report {r.experiment_id} --db data/arc.db --stored "
            f"--format md --out {DOC_PATH}",
            "",
            "Regenerate the example with `.venv/bin/python scripts/xp1_aa_dry_run.py "
            "--dir <empty scratch dir> --write-doc`.",
            "",
            "## Example: fixture dry run (synthetic data)",
            "",
        ]
        body = calibration_markdown(r, level=3)
    else:
        head += [
            f"Status: live run, evaluated {r.evaluated_at:%Y-%m-%d %H:%M %Z} "
            f"(verdict {r.verdict}).",
            "",
        ]
        body = calibration_markdown(r, level=2)
    return "\n".join([*head, *body]) + "\n"


def _usd(v: float) -> str:
    return f"${v:,.2f}".replace("$-", view.MINUS + "$")


def _bps(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f} bps".replace("-", view.MINUS)


def _arm_row(a: ArmSummary) -> str:
    fill = "n/a" if not a.executions else f"{a.filled_executions}/{a.executions}"
    slip = "n/a" if a.mean_slippage_bps is None else f"{a.mean_slippage_bps:.1f} bps"
    return (
        f"| {a.arm} | {a.sessions} | {_usd(a.total_pnl)} | "
        f"{view.pct(a.max_drawdown, sign=False)} | {view.pct(a.worst_day)} | "
        f"{a.orders} | {fill} | {slip} |"
    )


def _production_md(r: ExperimentReport, h: str) -> list[str]:
    """D108: the descriptive production panel (v3 reports only; nothing for v1/v2)."""
    pp = r.production
    if pp is None:
        return []
    out = [
        f"{h}# Production (descriptive; never a verdict, stop or promotion input)",
        "",
        f"- P − C = {view.pct(pp.cum_d_p_c, 3)} of t0 equity = start book "
        f"{view.pct(pp.cum_start_book, 3)} + residual {view.pct(pp.cum_residual, 3)}",
        f"- Start book: {len(pp.start_book.ids)} structure(s), cumulative P&L "
        f"{_usd(pp.start_book.cum_pnl)}",
        f"- Ramped (control's open structures ≥ production's, median of {pp.ramp_window} "
        f"sessions) from: {pp.ramped_from or 'not yet'}",
        "",
        "| Arm − production | Basis | n | Mean/day | Always-valid CI | p |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for c in pp.compare:
        ci = "n/a" if c.ci is None else f"[{view.pct(c.ci.lo, 3)}, {view.pct(c.ci.hi, 3)}]"
        out.append(
            f"| {c.arm} | {c.basis} | {c.n} | {view.pct(c.mean, 3)} | {ci} | "
            f"{view.p_text(c.p_value)} |"
        )
    return [*out, ""]


def calibration_markdown(r: ExperimentReport, *, level: int = 1) -> list[str]:
    """The calibration report of *r* as Markdown lines; ``level`` = top heading depth."""
    h = "#" * level
    c, p = r.calibration, r.primary
    sigma_usd = None if c.sigma is None else c.sigma * r.t0_equity
    div = (
        "n/a"
        if c.llm_divergence_rate is None
        else f"{c.llm_divergence_rate:.0%} ({c.divergent_chains}/{c.paired_chains} paired chains)"
    )
    lines = [
        f"{h} {r.experiment_id} {'A/A' if r.kind is ExperimentKind.AA else 'A/B'} "
        "calibration report",
        "",
        f"- Evaluated: {r.evaluated_at:%Y-%m-%d %H:%M %Z}; "
        f"t0 {r.t0:%Y-%m-%d %H:%M %Z} at {_usd(r.t0_equity)} "
        + (
            f"(every arm, flat); reference: the control arm (D108); production start book "
            f"{len(r.production.start_book.ids) if r.production else 0} structure(s), "
            "in the production panel only"
            if r.reference == "control"
            else f"(both arms); legacy book {len(r.legacy_book)} structure(s), excluded"
        ),
        f"- Sessions: {r.sessions} paired (window {r.min_sessions}–{r.max_sessions}); "
        f"as of {r.as_of_day or 'n/a'}"
        + (
            f"; missing {', '.join(map(str, r.missing_sessions))}"
            if r.missing_sessions
            else "; none missing"
        ),
        f"- Harness check (verdict): **{r.verdict}**: {r.verdict_reason}",
        f"- Mean d_t {view.pct(p.mean, 3)}/day, always-valid {1 - r.alpha:.0%} CI "
        f"{view.ci_text(r)} (% of t0 equity; p {view.p_text(p.p_value)}). "
        "An A/A whose CI excludes 0 is `invalid`: the harness, not the strategy, "
        "differs between the arms.",
        "",
        f"{h}# Noise: sigma of the paired daily difference",
        "",
        f"- sigma(d_t) = {view.pct(c.sigma, 3, sign=False)} of t0 equity per session"
        + ("" if sigma_usd is None else f" ({_usd(sigma_usd)} at t0 equity)"),
        "",
        f"{h}# Minimum detectable effect (daily mean of d_t)",
        "",
        f"| Sessions | Fixed horizon (2.8·sigma/√n) | Always-valid (power {r.power:.0%}) |",
        "|---:|---:|---:|",
    ]
    for n in sorted(c.mde_fixed):
        lines.append(
            f"| {n} | {view.pct(c.mde_fixed[n], 3, sign=False)} | "
            f"{view.pct(c.mde_always_valid.get(n), 3, sign=False)} |"
        )
    if not c.mde_fixed:
        lines.append("| n/a | n/a | n/a |")
    lines += [
        "",
        "The always-valid MDE is the effect the daily-peeking mSPRT detects by that "
        "session with the stated power; it is larger than the fixed-horizon MDE, which "
        "is the price of peeking every day.",
        "",
        f"{h}# Execution gap between the two paper accounts",
        "",
        f"- Slippage gap (treatment − control mean): {_bps(c.slippage_gap_bps)}",
        f"- Fill-rate gap (treatment − control): {view.pct(c.fill_rate_gap, 1)}",
        f"- LLM divergence on identical inputs: {div}",
        "",
        f"{h}# Per arm (informational; no guardrails)",
        "",
        "| Arm | Sessions | P&L | Max DD | Worst day | Orders | Fills | Mean slippage |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        *(_arm_row(a) for a in r.arms),
        "",
        *_production_md(r, h),
        f"{h}# Policy (fixed, not set from this A/A)",
        "",
        f"- alpha {r.config['defaults']['alpha']}, power {r.config['defaults']['power']}, "
        f"min {r.config['defaults']['min_sessions']} / max "
        f"{r.config['defaults']['max_sessions']} sessions for A/B experiments (owner, D44). "
        "This A/A informs only sigma, the MDE and the execution gaps above.",
        "",
        f"{h}# Provenance",
        "",
        f"- Spec sha256 `{r.spec_hash}` (registered `{r.registered_hash or 'n/a'}`)",
        f"- Config sha256 `{r.config_hash}`",
        f"- Shas: control `{r.control_sha[:12]}`, treatment `{(r.treatment_sha or 'n/a')[:12]}`, "
        f"evaluator `{(r.evaluator_sha or 'n/a')[:12]}`",
        f"- Report sha256 `{r.report_hash()}`",
    ]
    if r.series:
        lines += [
            "",
            f"{h}# Series",
            "",
            "| Session | Control P&L | Legacy P&L | Treatment P&L | d_t |",
            "|---|---:|---:|---:|---:|",
            *(
                f"| {s.day} | {_usd(s.control_pnl)} | {_usd(s.legacy_pnl)} | "
                f"{_usd(s.treatment_pnl)} | {view.pct(s.d, 3)} |"
                for s in r.series
            ),
        ]
    return lines
