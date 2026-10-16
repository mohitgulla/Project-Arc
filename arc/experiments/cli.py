"""``arc experiment``: the forward A/B experiment registry CLI (PLAN D44, E10.1).

- ``arc experiment create --spec <yaml> --owner-approval <ref>``  store a draft (defaults
  filled from ``config/experiments.yaml`` + D26 overrides); a draft may be re-created
- ``arc experiment register <id> --owner-approval <ref>``  hash-lock the spec; queues if
  its area is busy. D86: both refuse without ``--owner-approval`` (``P-<n>``,
  ``slack:<ts>`` or ``owner:<note>``); dev cards never write experiment rows, the owner
  adopts an approved Analyst proposal (``arc experiment adopt``, E21.4)
- ``arc experiment list [--status S]``
- ``arc experiment show <id> [--json]``
- ``arc experiment verify <id>``             recompute the spec hash (exit 1 on mismatch)
- ``arc experiment stop <id> --reason R --actor A``  owner stop
- ``arc experiment report <id> [--stored] [--now ISO] [--spy stored|alpaca]``  the E10.3
  evaluation (read-only: computed as of now, or the latest stored report). D119:
  ``--spy alpaca`` fetches SPY's daily bars for sessions no stored report carries
- ``arc experiment evaluate [<id>] [--now ISO] [--spy stored|alpaca]``  run the daily
  evaluation now: stores the report and applies its verdict (the
  ``experiments.evaluate`` routine does this, fetching SPY on a live tick)
- ``arc experiment adopt P-<n> --proposal <backlog json | P-<n>.yaml> --owner-approval
  slack:<ts>``  E21.4 (D86): next free XP id, defaults, validate, create + register, the
  spec PR (auto-merge) and one Slack line; idempotent per P-<n>; ``--dry-run`` writes
  nothing
- ``arc experiment verdict <id> [--dry-run]``  E21.4: the concluded experiment's verdict
  file PR (the daily evaluation opens these itself; this retries or previews one)

Every subcommand takes ``--db`` (default ``data/arc.db``). Exit 0 on success,
1 on a failed verify, 2 on a refusal (locked spec, bad transition, bad spec).
No trading behaviour changes here: the treatment runner is E10.2.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.experiments.evaluate import ExperimentReport
    from arc.experiments.models import ExperimentState

__all__ = ["add_experiment_parser", "run_experiment"]

LOCAL_ACTOR = "local"
OWNER_APPROVAL_HELP = (
    "required (D86): the owner's approval of this experiment, P-<n> (Analyst proposal), "
    "slack:<ts> or owner:<note>; normally set by `arc experiment adopt` (E21.4)"
)


def add_experiment_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from arc.experiments.models import ExperimentStatus, StopReason

    p = sub.add_parser("experiment", help="Forward A/B experiment registry (E10.1, D44)")
    esub = p.add_subparsers(dest="experiment_command", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        sp.add_argument("--json", action="store_true", help="Print JSON")
        return sp

    c = common(esub.add_parser("create", help="Store a spec as a draft"))
    c.add_argument("--spec", required=True, help="config/experiments/live/<x>.yaml")
    c.add_argument("--actor", default=LOCAL_ACTOR, help="owner Slack id, arc-analyst, or local")
    c.add_argument("--owner-approval", default=None, help=OWNER_APPROVAL_HELP)
    r = common(esub.add_parser("register", help="Pre-register: hash-lock the spec"))
    r.add_argument("experiment_id")
    r.add_argument("--actor", default=LOCAL_ACTOR)
    r.add_argument("--owner-approval", default=None, help=OWNER_APPROVAL_HELP)
    ls = common(esub.add_parser("list", help="All experiments and their status"))
    ls.add_argument("--status", choices=[s.value for s in ExperimentStatus], default=None)
    s = common(esub.add_parser("show", help="One experiment: spec, hash, status, events"))
    s.add_argument("experiment_id")
    v = common(esub.add_parser("verify", help="Recompute the spec hash against the lock"))
    v.add_argument("experiment_id")
    st = common(esub.add_parser("stop", help="Stop an experiment (owner)"))
    st.add_argument("experiment_id")
    st.add_argument("--reason", required=True, choices=[x.value for x in StopReason])
    st.add_argument("--actor", required=True, help="owner Slack id, or local (shell owner)")
    st.add_argument("--note", default=None)
    rp = common(esub.add_parser("report", help="E10.3 evaluation report (read-only)"))
    rp.add_argument("experiment_id")
    rp.add_argument("--stored", action="store_true", help="Latest stored report, no recompute")
    rp.add_argument("--now", default=None, help="Evaluate as of this ISO time (default: now)")
    rp.add_argument(
        "--format",
        choices=["text", "md"],
        default="text",
        help="text (default) or md: the A/A calibration report as Markdown (E10.4)",
    )
    rp.add_argument("--out", default=None, help="With --format md: write the document here")
    rp.add_argument(
        "--spy",
        choices=["stored", "alpaca"],
        default="stored",
        help="D119 covariate: SPY returns the stored reports carry only (default, offline; "
        "new days stay missing), or alpaca: fetch SPY daily bars for the other days",
    )
    ev = common(esub.add_parser("evaluate", help="Evaluate running experiments; apply verdicts"))
    ev.add_argument("experiment_id", nargs="?", default=None)
    ev.add_argument("--now", default=None, help="Evaluate as of this ISO time (default: now)")
    ev.add_argument(
        "--spy",
        choices=["stored", "alpaca"],
        default="stored",
        help="D119 covariate: SPY returns the stored reports carry only (default, offline; "
        "new days stay missing), or alpaca: fetch SPY daily bars for the other days",
    )
    pl = common(
        esub.add_parser(
            "plan", help="D69: sessions needed for K arms with Holm at the A/A sigma (read-only)"
        )
    )
    pl.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="config/experiments/live/<x>.yaml, or a stored experiment id (default: an ab "
        "spec with the config/experiments.yaml defaults, e.g. for an Analyst P-<n> proposal)",
    )
    pl.add_argument("--sigma", type=float, default=None, help="sigma of d_t (default: A/A's)")
    pl.add_argument(
        "--effect", type=float, default=None, help="daily effect to detect (default: spec mde)"
    )
    pl.add_argument(
        "--k",
        type=int,
        default=None,
        help="treatments to plan for (default: the spec's; 4 without a spec, D86)",
    )
    pl.add_argument(
        "--effects",
        default=None,
        help="E21.3: comma list of daily effects; sessions needed at --k for each",
    )
    ad = common(
        esub.add_parser(
            "adopt", help="E21.4: register an owner-approved Analyst proposal P-<n> (D86)"
        )
    )
    ad.add_argument("proposal_id", nargs="?", default=None, help="P-<n>")
    ad.add_argument(
        "--proposal",
        required=True,
        help="the proposal: an xp_backlog.json entry (or the whole backlog), or the "
        "Analyst's approved proposals/P-<n>.yaml",
    )
    ad.add_argument("--owner-approval", default=None, help=OWNER_APPROVAL_HELP)
    ad.add_argument("--now", default=None, help="ISO time (default: now)")
    ad.add_argument("--dry-run", action="store_true", help="Change nothing; print the plan")
    ad.add_argument("--no-pr", action="store_true", help="Register only; no spec PR")
    ad.add_argument("--no-slack", action="store_true", help="No #arc-investor line")
    ad.add_argument("--no-fetch", action="store_true", help="Do not read origin/main")
    common(
        esub.add_parser(
            "regime-panel",
            help="E25.5 (D108): P - C and Ti - C pooled across v3 XPs by regime "
            "(descriptive, read-only)",
        )
    )
    vd = common(esub.add_parser("verdict", help="E21.4: a concluded experiment's verdict PR"))
    vd.add_argument("experiment_id")
    vd.add_argument("--dry-run", action="store_true", help="Print the verdict file only")
    # E10.2 arm runner: start | pair | arms-tick
    from arc.experiments.cli_arms import add_arm_parsers

    add_arm_parsers(esub, common, LOCAL_ACTOR)


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _open(db: str | None) -> sqlite3.Connection:
    from arc.control.effective import open_store

    return open_store(db)


def _summary(s: ExperimentState) -> str:
    reason = f" ({s.reason.value})" if s.reason else ""
    return (
        f"{s.experiment_id}  {s.status.value}{reason}  {s.spec.area.value}/{s.spec.kind.value}  "
        f"r{s.revision}  sha256 {s.spec_hash[:12]}  {s.spec.title}"
    )


def _detail(s: ExperimentState) -> list[str]:
    sp = s.spec
    margin = sp.non_inferiority_margin if sp.non_inferiority_margin else "-"
    lines = [
        _summary(s),
        f"  hypothesis: {sp.hypothesis.strip()}",
        f"  proposed by {sp.proposed_by}; backtest ref {sp.backtest_ref or '-'}",
        f"  primary {sp.primary_metric}; secondary {sp.secondary_metric} "
        f"(non-inferiority margin {margin})",
        f"  alpha {sp.alpha}, power {sp.power}, mde {sp.mde if sp.mde else 'unset (A/A)'}, "
        f"sessions {sp.min_sessions}-{sp.max_sessions}",
    ]
    many = len(sp.arms.treatments) > 1
    if sp.control_runs:
        lines.append(
            f"  control: arm {sp.arm_id_for('control')} (spec v{sp.spec_version}, D108): "
            "verdict = Ti vs control (Holm); production is a descriptive panel only"
        )
    for name, arm in sp.arms.treatments.items():
        over = arm.overlay
        label = f"{name} overlay" if many else "treatment overlay"
        lines.append(
            f"  {label}: " + (json.dumps(over, sort_keys=True) if over else "none (= control)")
        )
    lines.append(f"  spec sha256 {s.spec_hash}")
    lines.append(f"  registered sha256 {s.registered_hash or '- (draft: not locked yet)'}")
    lines.append("  events:")
    for e in s.events:
        reason = f" ({e.reason.value})" if e.reason else ""
        ok = e.detail.get("owner_approval") if isinstance(e.detail, dict) else None
        approval = f" (approval {ok})" if ok else ""
        lines.append(
            f"    #{e.id} {e.at:%Y-%m-%d %H:%M %Z} {e.status.value}{reason} by {e.actor}{approval}"
        )
    return lines


def _state_json(s: ExperimentState) -> dict[str, Any]:
    return s.model_dump(mode="json")


def _parse_now(text: str | None) -> _dt.datetime:
    from arc.utils.calendar import ET, now_et

    if text is None:
        return now_et()
    t = _dt.datetime.fromisoformat(text)
    return t.replace(tzinfo=ET) if t.tzinfo is None else t.astimezone(ET)


def _pct(v: float | None, digits: int = 3) -> str:
    return "-" if v is None else f"{v:+.{digits}%}"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def report_lines(r: ExperimentReport) -> list[str]:
    """Plain-text rendering of an :class:`ExperimentReport` (``arc experiment report``)."""
    p, s = r.primary, r.secondary
    ci = f"[{_pct(p.ci.lo)}, {_pct(p.ci.hi)}]" if p.ci else "- (too few sessions)"
    lines = [
        f"{r.experiment_id}  {r.kind.value}/{r.area}  {r.status.value}  "
        f"verdict {r.verdict.upper()}: {r.verdict_reason}",
        f"  evaluated {r.evaluated_at:%Y-%m-%d %H:%M %Z}; t0 {r.t0:%Y-%m-%d %H:%M %Z} "
        f"equity {r.t0_equity:,.2f}; "
        + (
            f"reference control arm (D108); production start book "
            f"{len(r.production.start_book.ids) if r.production else 0}"
            if r.reference == "control"
            else f"legacy book {len(r.legacy_book)}"
        ),
        f"  sessions {r.sessions} (min {r.min_sessions}, max {r.max_sessions}); "
        f"as of {r.as_of_day or '-'}"
        + (f"; missing {', '.join(map(str, r.missing_sessions))}" if r.missing_sessions else ""),
        f"  primary  mean d {_pct(p.mean)}/day  always-valid {1 - r.alpha:.0%} CI {ci}  "
        f"sigma {_pct(p.sigma)} ({p.sigma_source or '-'})  tau {_pct(p.tau)}",
        f"  secondary Sortino control {_num(s.sortino_control)}"
        f" treatment {_num(s.sortino_treatment)}"
        + (f"  diff CI [{s.diff_ci.lo:+.2f}, {s.diff_ci.hi:+.2f}]" if s.diff_ci else "  diff CI -")
        + (
            f"  margin {s.margin}: {'non-inferior' if s.non_inferior else 'not shown'}"
            if s.margin is not None
            else "  (aa: no margin)"
        ),
    ]
    lines += long_run_lines(r)
    lines.append("  arms:")
    for a in r.arms:
        lines.append(
            f"    {a.arm:9} pnl {a.total_pnl:+,.2f}  max DD {a.max_drawdown:.2%}  "
            f"worst day {_pct(a.worst_day, 2)}  orders {a.orders}  "
            f"fills {a.filled_executions}/{a.executions}  "
            f"slippage {'-' if a.mean_slippage_bps is None else f'{a.mean_slippage_bps:.1f} bps'}"
        )
    if r.multi_arm:
        lines += _multi_arm_lines(r)
    if r.production is not None:
        lines += production_lines(r)
    c = r.calibration
    lines.append(
        f"  calibration: sigma {_pct(c.sigma)}  MDE "
        + ", ".join(f"{n}:{_pct(v)}" for n, v in c.mde_fixed.items())
        + "  always-valid MDE "
        + ", ".join(f"{n}:{_pct(v)}" for n, v in c.mde_always_valid.items())
    )
    if c.sigma_adj is not None:
        lines.append(
            f"    D119 sigma raw {_pct(c.sigma)}  SPY-adjusted {_pct(c.sigma_adj)}  long-run "
            f"{_pct(c.sigma_lr)} (f {_num(c.hac_factor)}, lag {c.hac_lag}, rho "
            + " ".join(f"{x:+.2f}" for x in c.rho or [])
            + f")  beta spread {_num(c.beta_spread)}"
        )
        for label, fx, av in (
            ("adjusted", c.mde_fixed_adj, c.mde_always_valid_adj),
            ("long-run", c.mde_fixed_lr, c.mde_always_valid_lr),
        ):
            lines.append(
                f"    {label} MDE "
                + ", ".join(f"{n}:{_pct(v)}" for n, v in (fx or {}).items())
                + "  always-valid MDE "
                + ", ".join(f"{n}:{_pct(v)}" for n, v in (av or {}).items())
            )
    slip = "-" if c.slippage_gap_bps is None else f"{c.slippage_gap_bps:+.1f} bps"
    lines.append(
        f"    slippage gap {slip}"
        f"  fill-rate gap {_pct(c.fill_rate_gap, 1)}  LLM divergence "
        f"{'-' if c.llm_divergence_rate is None else f'{c.llm_divergence_rate:.0%}'} "
        f"({c.divergent_chains}/{c.paired_chains} paired chains)"
    )
    if r.breakdowns:
        lines.append("  breakdowns (reported only, never decision inputs):")
        for b in r.breakdowns:
            lines.append(
                f"    {b.by:14} {b.key:16} {b.arm:9} trades {b.trades:3}  "
                f"realised {b.realised_pnl:+,.2f}"
            )
    if r.series:
        lines.append("  series (day: control pnl, legacy pnl, treatment pnl, d):")
        for row in r.series:
            lines.append(
                f"    {row.day}  {row.control_pnl:+10,.2f}  {row.legacy_pnl:+9,.2f}  "
                f"{row.treatment_pnl:+10,.2f}  {_pct(row.d)}"
            )
    lines.append(
        f"  spec sha256 {r.spec_hash}  config sha256 {r.config_hash[:16]}  "
        f"shas control {r.control_sha[:12]} treatment {(r.treatment_sha or '-')[:12]} "
        f"evaluator {(r.evaluator_sha or '-')[:12]}"
    )
    return lines


def _multi_arm_lines(r: ExperimentReport) -> list[str]:
    """D69 v2 sections: the all-arms table (ranked) and every pair (p next to each CI)."""
    lines = [
        f"  headline arm {r.headline_arm} (top-level primary/secondary/series = control vs it)",
        "  all arms (ranked by primary; Ti vs control):",
        "    rank arm        cum P&L %  daily mean ± sd        Sortino  max DD  orders  "
        "conflicts  sessions  verdict",
    ]
    for a in sorted(r.arms, key=lambda x: x.rank or 0):
        lines.append(
            f"    {a.rank or '-':>4} {a.arm:9} {_pct(a.cum_pnl_pct, 2):>10}  "
            f"{_pct(a.daily_mean)} ± {_pct(a.daily_sd).lstrip('+')}  "
            f"{_num(a.sortino):>7}  {a.max_drawdown:6.2%}  {a.orders:6}  "
            f"{a.cross_arm_conflicts if a.cross_arm_conflicts is not None else '-':>9}  "
            f"{a.sessions:8}  {a.verdict or '-'}"
        )
    if r.omnibus is not None:
        o = r.omnibus
        lines.append(
            f"  omnibus (any of {o.k} treatments differs from control): Holm-min p "
            f"{'-' if o.p_value is None else f'{o.p_value:.4f}'} -> "
            f"{'yes' if o.differs else 'no'}"
        )
    lines.append("  pairs (b - a; always-valid CI, p, Holm p):")
    for p in r.pairwise or []:
        ci = f"[{_pct(p.ci.lo)}, {_pct(p.ci.hi)}]" if p.ci else "-"
        pv = "-" if p.p_value is None else f"{p.p_value:.4f}"
        hp = "-" if p.holm_p is None else f"{p.holm_p:.4f}"
        tag = (
            "no verdict"
            if p.descriptive
            else f"{(p.verdict or '-').upper()}: {p.verdict_reason or ''}"
            + (f" (stopped {p.decided_day})" if p.decided_day else "")
        )
        lines.append(
            f"    {p.b} vs {p.a:8} n {p.n:3}  mean {_pct(p.mean)}/day  CI {ci}  p {pv}  "
            f"Holm p {hp}  {tag}"
        )
        if p.adjustment is not None:
            lines.append("      " + _long_run_line(p, r.alpha).lstrip())
    return lines


def _long_run_line(p: Any, alpha: float) -> str:
    """D119: one primary's (or pair's) adjustment + HAC numbers on one line."""
    a, h = p.adjustment, p.hac
    ci = f"[{_pct(a.ci.lo)}, {_pct(a.ci.hi)}]" if a.ci else "-"
    role = "pre-registered" if a.pre_registered else "descriptive"
    cov = "+".join(a.covariates) or "unadjusted"
    miss = (
        f"  SPY missing {', '.join(map(str, a.covariate_missing))}" if a.covariate_missing else ""
    )
    rho = " ".join(f"{x:+.2f}" for x in h.rho) if h is not None and h.rho else "-"
    f = (
        "-"
        if h is None
        else f"{_num(h.factor)} ({h.source or '-'}; running {_num(h.factor_running)})"
    )
    return (
        f"  D119 {role} ({cov} {a.status}, HAC lag {h.lag if h and h.lag else 'iid'}): n {a.n}"
        f"  estimate {_pct(a.estimate)}/day  {1 - alpha:.0%} CI {ci}  p "
        f"{'-' if a.p_value is None else f'{a.p_value:.4f}'}  beta {_num(a.beta)} "
        f"(se {_num(a.beta_se)}, R2 {_num(a.r2)})  sigma raw {_pct(p.sigma_raw)} adj "
        f"{_pct(p.sigma_adj)} LR {_pct(p.sigma_lr)} ({a.sigma_source or '-'})  f {f}  rho {rho}"
        + miss
    )


def long_run_lines(r: ExperimentReport) -> list[str]:
    """D119: the headline pair's adjusted + HAC primary and (pre-registered) the iid one."""
    p = r.primary
    out: list[str] = []
    if p.adjustment is not None:
        out.append(_long_run_line(p, r.alpha))
    if p.primary_unadjusted is not None:
        u = p.primary_unadjusted
        ci = f"[{_pct(u.ci.lo)}, {_pct(u.ci.hi)}]" if u.ci else "-"
        out.append(
            f"  D44 iid unadjusted (transparency): mean {_pct(u.mean)}/day  CI {ci}  p "
            f"{'-' if u.p_value is None else f'{u.p_value:.4f}'}  sigma {_pct(u.sigma)}"
        )
    return out


def production_lines(r: ExperimentReport) -> list[str]:
    """D108 (E25.3): the descriptive Production panel, printed after the Ti vs C table."""
    pp = r.production
    if pp is None:
        return []
    sb = pp.start_book
    still = sum(1 for d in sb.close_dates.values() if d is None)
    lines = [
        "  production panel (descriptive only: never a verdict, stop or promotion input):",
        f"    P total {pp.cum_p_pnl:+,.2f}  C total {pp.cum_c_pnl:+,.2f}  "
        f"P - C {_pct(pp.cum_d_p_c)} = start book {_pct(pp.cum_start_book)} "
        f"+ residual {_pct(pp.cum_residual)} (of t0 equity)",
        f"    start book {len(sb.ids)} structure(s), {still} still open, "
        f"cum P&L {sb.cum_pnl:+,.2f}"
        + (
            "; closed "
            + ", ".join(f"{i[:8]} {d}" for i, d in sorted(sb.close_dates.items()) if d is not None)
            if any(d is not None for d in sb.close_dates.values())
            else ""
        ),
        f"    ramped (C open structures >= P, median of {pp.ramp_window}): "
        f"{pp.ramped_from or 'not yet'}",
        "    arm - production (always-valid CI, no Holm, no verdict):",
    ]
    for c in pp.compare:
        ci = f"[{_pct(c.ci.lo)}, {_pct(c.ci.hi)}]" if c.ci else "-"
        pv = "-" if c.p_value is None else f"{c.p_value:.4f}"
        lines.append(
            f"      {c.arm:9} {c.basis:13} n {c.n:3}  mean {_pct(c.mean)}/day  CI {ci}  p {pv}"
        )
    if pp.sessions:
        lines.append(
            "    sessions (day: P pnl, start book, P ex-start, C pnl, d_p_c, residual, "
            "P/C open, ramped):"
        )
        for s in pp.sessions:
            lines.append(
                f"      {s.day}  {s.p_pnl:+10,.2f}  {s.p_start_book_pnl:+9,.2f}  "
                f"{s.p_ex_start_pnl:+10,.2f}  {s.c_pnl:+10,.2f}  {_pct(s.d_p_c)}  "
                f"{_pct(s.residual)}  {s.p_open if s.p_open is not None else '-'}/"
                f"{s.c_open if s.c_open is not None else '-'}  {'yes' if s.ramped else 'no'}"
            )
    if pp.missing_sessions:
        lines.append(f"    missing {', '.join(map(str, pp.missing_sessions))}")
    return lines


def _owner(actor: str) -> bool:
    from arc.config import ArcSettings

    if actor == LOCAL_ACTOR:
        return True
    return actor.upper() in {u.upper() for u in ArcSettings().approver_slack_user_ids}


def _write_md(rep: ExperimentReport, out: str | None) -> int:
    """``report --format md``: the calibration Markdown to stdout, or the doc to *out*."""
    from pathlib import Path

    from arc.experiments.calibration import aa_document, calibration_markdown

    if out is None:
        _out("\n".join(calibration_markdown(rep)))
        return 0
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(aa_document(rep))
    _out(f"wrote {path}")
    return 0


def plan_rows(
    *, sigma: float, effect: float | None, k_max: int, alpha: float, power: float,
    min_sessions: int, max_sessions: int, mde: float | None,
) -> list[dict[str, Any]]:  # fmt: skip
    """D69: per K (1..k_max) the sessions one arm needs (Holm worst step alpha/K) and the MDE.

    ``mde_at_max`` is the always-valid MDE at ``max_sessions`` and level ``alpha / K``:
    the smallest daily effect the run can still detect if it goes the full window.
    """
    from arc.experiments import stats

    tau = stats.mixing_tau(sigma, mde=mde, min_sessions=min_sessions)
    out = []
    for k in range(1, k_max + 1):
        need = (
            None
            if effect is None
            else stats.sessions_needed(sigma, effect, tau=tau, alpha=alpha, power=power, k=k)
        )
        out.append(
            {
                "k": k,
                "alpha_per_arm": alpha / k,
                "sessions_needed": need,
                "fits_window": None if need is None else need <= max_sessions,
                "mde_at_max": stats.mde_always_valid(
                    sigma, max_sessions, tau=tau, alpha=alpha / k, power=power
                ),
            }
        )
    return out


def effect_grid(
    *, sigma: float, effects: list[float], k: int, alpha: float, power: float,
    min_sessions: int, max_sessions: int, mde: float | None,
) -> list[dict[str, Any]]:  # fmt: skip
    """E21.3 (D86): sessions one arm of a *k*-treatment run needs per daily effect.

    What the Analyst's pre-run gate puts in context, so a ``P-<n>`` proposal's
    ``sessions_needed`` is read off this table, not guessed.
    """
    from arc.experiments import stats

    tau = stats.mixing_tau(sigma, mde=mde, min_sessions=min_sessions)
    out = []
    for e in effects:
        need = stats.sessions_needed(sigma, e, tau=tau, alpha=alpha, power=power, k=k)
        out.append(
            {
                "effect": e,
                "sessions_needed": need,
                "fits_window": need is not None and need <= max_sessions,
            }
        )
    return out


def _parse_effects(text: str | None) -> list[float]:
    if not text:
        return []
    vals = [float(x) for x in text.split(",") if x.strip()]
    if any(v <= 0 for v in vals):
        msg = "--effects must be positive daily fractions of t0 equity"
        raise ValueError(msg)
    return vals


def _run_plan(args: argparse.Namespace, store: Any, conn: sqlite3.Connection) -> int:
    from pathlib import Path

    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.overlay import fill_defaults, load_spec

    defaults = experiments_config(effective_settings(conn)).defaults
    if args.spec is None:
        # D86: an Analyst proposal has no id yet; plan it at the registry defaults.
        exp_id, alpha, power = "(defaults)", defaults.alpha, defaults.power
        min_s, max_s, mde, k = defaults.min_sessions, defaults.max_sessions, None, 4
    else:
        if Path(args.spec).is_file():
            spec = fill_defaults(load_spec(args.spec), defaults)
        else:
            spec = store.require(args.spec).spec
        assert spec.alpha is not None and spec.power is not None  # noqa: S101 - filled
        assert spec.min_sessions is not None and spec.max_sessions is not None  # noqa: S101
        exp_id, alpha, power = spec.id, spec.alpha, spec.power
        min_s, max_s, mde, k = spec.min_sessions, spec.max_sessions, spec.mde, len(spec.arms.names)
    if args.k is not None:
        if args.k < 1:
            _err("arc experiment plan: --k must be >= 1")
            return 2
        k = args.k
    try:
        effects = _parse_effects(args.effects)
    except ValueError as exc:
        _err(f"arc experiment plan: {exc}")
        return 2
    sigma = args.sigma if args.sigma is not None else store.aa_sigma()
    if sigma is None or sigma <= 0:
        _err(
            "arc experiment plan: no A/A sigma recorded yet (no A/A stopped with one); "
            "pass --sigma <fraction of t0 equity>"
        )
        return 2
    effect = args.effect if args.effect is not None else mde
    rows = plan_rows(
        sigma=sigma, effect=effect, k_max=max(k, 4), alpha=alpha,
        power=power, min_sessions=min_s, max_sessions=max_s, mde=mde,
    )  # fmt: skip
    grid = effect_grid(
        sigma=sigma, effects=effects, k=k, alpha=alpha, power=power,
        min_sessions=min_s, max_sessions=max_s, mde=mde,
    )  # fmt: skip
    if args.json:
        _out(
            json.dumps(
                {
                    "experiment_id": exp_id,
                    "k": k,
                    "sigma": sigma,
                    "sigma_source": "flag" if args.sigma is not None else "aa",
                    "effect": effect,
                    "alpha": alpha,
                    "power": power,
                    "min_sessions": min_s,
                    "max_sessions": max_s,
                    "rows": rows,
                    "effects": grid,
                },
                indent=2,
            )
        )
        return 0
    _out(
        f"{exp_id}  K={k} treatment(s)  sigma {_pct(sigma)}/day "
        f"({'--sigma' if args.sigma is not None else 'A/A'})  effect "
        f"{_pct(effect) if effect is not None else '- (no spec mde; pass --effect)'}  "
        f"alpha {alpha} (Holm)  power {power:.0%}  "
        f"window {min_s}-{max_s}"
    )
    _out("    K  alpha/K   sessions needed   MDE at max_sessions")
    for r in rows:
        need = "-" if r["sessions_needed"] is None else str(r["sessions_needed"])
        if r["fits_window"] is False:
            need += " (> max)"
        mark = "  <- this spec" if r["k"] == k else ""
        _out(
            f"  {r['k']:3}  {r['alpha_per_arm']:.4f}   {need:>15}   "
            f"{_pct(r['mde_at_max'])}/day{mark}"
        )
    _out("  each extra arm lowers Holm's first step alpha/K, so every arm needs more sessions")
    if grid:
        _out(f"  effect -> sessions needed at K={k}:")
        for g in grid:
            need = "-" if g["sessions_needed"] is None else str(g["sessions_needed"])
            if not g["fits_window"]:
                need += " (> max)"
            _out(f"    {_pct(g['effect'])}/day -> {need}")
    return 0


def _spy_market(args: argparse.Namespace, settings: Any) -> Any:
    """D119: the SPY bar provider for ``--spy alpaca`` (None for ``stored`` or no keys)."""
    if getattr(args, "spy", "stored") != "alpaca" or getattr(args, "stored", False):
        return None
    try:
        from arc.data.alpaca import AlpacaMarketData
        from arc.experiments.covariate import FeedPinned

        return FeedPinned(AlpacaMarketData(), feed=settings.alpaca_history_feed)
    except Exception as exc:  # noqa: BLE001 - no keys: stored SPY returns only
        _err(f"arc experiment: no SPY market data ({exc}); using stored SPY returns only")
        return None


def _run_eval(args: argparse.Namespace, store: Any, conn: sqlite3.Connection) -> int:
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.evaluate import build_report, evaluate, evaluate_running, latest_report
    from arc.experiments.models import ExperimentStatus

    now = _parse_now(args.now)
    settings = effective_settings(conn)
    cfg = experiments_config(settings)
    market = _spy_market(args, settings)
    if args.experiment_command == "report":
        if args.stored:
            rep = latest_report(conn, args.experiment_id)
            if rep is None:
                _err(f"arc experiment report: no stored report for {args.experiment_id}")
                return 2
        else:
            from arc.experiments.paired import paired_view

            with paired_view(conn) as view:
                rep = build_report(
                    view,
                    store.require(args.experiment_id),
                    cfg,
                    now=now,
                    aa_sigma=store.aa_sigma(),
                    aa_cal=store.aa_calibration(),
                    market=market,
                )
        reports = [rep]
        if args.format == "md":
            return _write_md(rep, args.out)
        if args.out:
            _err("arc experiment report: --out needs --format md")
            return 2
    else:
        store._now = lambda: now  # noqa: SLF001 - stop events carry the evaluation time
        if args.experiment_id is not None:
            st = store.require(args.experiment_id)
            if st.status is not ExperimentStatus.RUNNING:
                _err(f"arc experiment evaluate: {args.experiment_id} is {st.status.value}")
                return 2
            reports = [evaluate(store, args.experiment_id, cfg, now=now, market=market)]
        else:
            reports = evaluate_running(store, cfg, now=now, market=market)
    if args.json:
        payload = [r.model_dump(mode="json") for r in reports]
        _out(json.dumps(payload[0] if args.experiment_command == "report" else payload, indent=2))
    else:
        for r in reports:
            _out("\n".join(report_lines(r)))
            if args.experiment_command == "report":
                # D86 (E21.1): read-only context, outside the stored (hashed) report
                from arc.experiments.shipped import shipped_lines

                _out("\n".join(shipped_lines(r.t0)))
        if not reports:
            _out("no running experiments")
    return 0


def _proposal_from(args: argparse.Namespace) -> Any:
    from pathlib import Path

    from arc.experiments.adopt import AdoptError, load_proposal, read_proposal_file

    path = Path(args.proposal)
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("items"), dict):  # the whole backlog
            if not args.proposal_id:
                msg = "name the proposal: arc experiment adopt P-<n> --proposal <backlog>"
                raise AdoptError(msg)
            entry = next(
                (e for e in data["items"].values() if e.get("id") == args.proposal_id), None
            )
            if entry is None:
                msg = f"no {args.proposal_id} in {path}"
                raise AdoptError(msg)
            data = entry
        return load_proposal(data, proposal_id=args.proposal_id)
    return read_proposal_file(path, args.proposal_id)


def _run_adopt(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.adopt import adopt
    from arc.experiments.publish import GitMainView, GitPublisher
    from arc.experiments.store import check_owner_approval

    check_owner_approval(args.owner_approval)  # refuse before reading anything
    proposal = _proposal_from(args)
    cfg = experiments_config(effective_settings(conn))
    now = _parse_now(args.now)
    post = None
    if not (args.dry_run or args.no_slack):
        from arc.approvals.auto import post_day_notice

        def post(text: str) -> None:
            post_day_notice(conn, text, now)

    res = adopt(
        conn,
        proposal,
        cfg,
        owner_approval=args.owner_approval,
        now=now,
        main=None if args.no_fetch else GitMainView(),
        publisher=None if (args.dry_run or args.no_pr) else GitPublisher(),
        dry_run=args.dry_run,
        post=post,
    )
    if args.json:
        _out(json.dumps(res.as_json(), indent=2, default=str))
        return 0
    head = "dry-run (nothing written)" if res.dry_run else ("adopted" if res.created else "found")
    _out(f"{head}: {res.proposal_id} -> {res.experiment_id} [{res.status}]")
    if res.queue_reason:
        _out(f"  queued: {res.queue_reason}")
    _out(f"  area {res.area} · {res.arms} treatments · spec sha256 {res.spec_hash}")
    _out(f"  owner start: {res.start_cmd or 'when the queue frees a slot'} (D112, manual)")
    _out(f"  file: {res.path} (branch {res.branch})")
    if res.pr_url or res.pr_status:
        _out(f"  PR: {res.pr_url or '-'} ({res.pr_status})")
    _out(f"  slack: {res.slack}")
    if res.dry_run:
        _out(f"  PR title: {res.pr_title}")
        _out("  PR body:")
        _out("\n".join("    " + line for line in res.pr_body.splitlines()))
        _out("  spec file:")
        _out("\n".join("    " + line for line in res.file_text.splitlines()))
    return 0


def run_regime_panel(args: argparse.Namespace) -> int:
    """``arc experiment regime-panel``: the store is opened ``mode=ro`` (never migrated)."""
    from arc.experiments.regime_panel import (
        build_regime_panel,
        load_v3_reports,
        panel_json,
        panel_lines,
    )
    from arc.store.db import connect_ro

    try:
        conn = connect_ro(args.db)
    except FileNotFoundError as exc:
        _err(f"arc experiment regime-panel: {exc}")
        return 2
    try:
        panel = build_regime_panel(load_v3_reports(conn))
    finally:
        conn.close()
    if args.json:
        _out(panel_json(panel))
    else:
        panel_lines(panel, out=_out)
    return 0


def _run_verdict(args: argparse.Namespace, store: Any, conn: sqlite3.Connection) -> int:
    from arc.experiments.evaluate import latest_report
    from arc.experiments.publish import GitPublisher
    from arc.experiments.verdicts import publish_verdict, verdict_file_text

    st = store.require(args.experiment_id)
    if st.reason is None:
        _err(f"arc experiment verdict: {st.experiment_id} is {st.status.value}, not concluded")
        return 2
    if args.dry_run:
        _out(verdict_file_text(st, latest_report(conn, st.experiment_id)).rstrip())
        return 0
    from arc.experiments.publish import GitMainView

    pr = publish_verdict(
        conn, store, st.experiment_id, GitPublisher(), now=_parse_now(None), main=GitMainView()
    )
    _out(
        f"{st.experiment_id}: verdict already published"
        if pr is None
        else f"{st.experiment_id}: verdict PR {pr.url or pr.status}"
    )
    return 0


def run_experiment(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from arc.experiments.models import ExperimentStatus, StopDetail, StopReason
    from arc.experiments.publish import PublishError
    from arc.experiments.store import ExperimentError, ExperimentStore

    cmd = args.experiment_command
    if cmd == "arm-tick":  # E15.4: control is read mode=ro only; never migrated here
        from arc.experiments.cli_arms import run_arm_tick

        return run_arm_tick(args)
    if cmd == "regime-panel":  # E25.5: read-only view, never opens the store read-write
        return run_regime_panel(args)
    conn = _open(args.db)
    try:
        from arc.control.effective import effective_settings, experiments_config

        # D69: register/stop queue and promote within experiments.runner.max_parallel_arms
        runner = experiments_config(effective_settings(conn)).runner
        store = ExperimentStore.for_runner(conn, runner)
        if cmd == "create":
            from arc.experiments.overlay import fill_defaults, load_spec
            from arc.experiments.store import check_owner_approval

            check_owner_approval(args.owner_approval)  # refuse before reading the spec
            try:
                spec = load_spec(args.spec)
            except (ValidationError, ValueError, OSError) as exc:
                _err(f"arc experiment create: invalid spec {args.spec}: {exc}")
                return 2
            spec = fill_defaults(spec, experiments_config(effective_settings(conn)).defaults)
            st = store.create(spec, actor=args.actor, owner_approval=args.owner_approval)
        elif cmd == "register":
            st = store.register(
                args.experiment_id, actor=args.actor, owner_approval=args.owner_approval
            )
        elif cmd == "list":
            states = store.all(status=ExperimentStatus(args.status) if args.status else None)
            if args.json:
                _out(json.dumps([_state_json(s) for s in states], indent=2))
            else:
                for s in states:
                    _out(_summary(s))
                if not states:
                    _out("no experiments")
            return 0
        elif cmd == "show":
            st = store.require(args.experiment_id)
        elif cmd == "verify":
            v = store.verify(args.experiment_id)
            if args.json:
                _out(json.dumps(v, indent=2))
            else:
                verdict = "OK" if v["ok"] else "MISMATCH"
                _out(
                    f"{v['experiment_id']} ({v['status']}, r{v['revision']}): {verdict}\n"
                    f"  registered {v['registered_hash'] or '- (draft)'}\n"
                    f"  stored     {v['stored_hash']}\n"
                    f"  recomputed {v['recomputed_hash']}"
                )
            return 0 if v["ok"] else 1
        elif cmd in ("report", "evaluate"):
            return _run_eval(args, store, conn)
        elif cmd == "plan":
            return _run_plan(args, store, conn)
        elif cmd == "adopt":
            return _run_adopt(args, conn)
        elif cmd == "verdict":
            return _run_verdict(args, store, conn)
        elif cmd in ("start", "pair", "arms-tick", "repair-ledger"):
            from arc.experiments.cli_arms import run_arm_command

            return run_arm_command(args, conn)
        elif cmd == "stop":
            if not _owner(args.actor):
                _err(f"arc experiment stop: {args.actor} is not the owner")
                return 2
            st = store.stop(
                args.experiment_id,
                StopReason(args.reason),
                actor=args.actor,
                detail=StopDetail(note=args.note),
            )
            # E21.4: the verdict file PR follows at the 16:40 experiments.evaluate
            _err(
                f"{args.experiment_id}: verdict file PR opens at the next experiments.evaluate "
                f"(16:40 ET), or now: arc experiment verdict {args.experiment_id}"
            )
        else:  # pragma: no cover - argparse enforces the choice
            return 2
    except ExperimentError as exc:
        _err(f"arc experiment {cmd}: {exc}")
        return 2
    except PublishError as exc:  # E21.4: git / gh failed; the registry state is kept
        _err(f"arc experiment {cmd}: publishing failed (re-run to retry): {exc}")
        return 3
    finally:
        conn.close()
    if args.json:
        _out(json.dumps(_state_json(st), indent=2))
    else:
        _out("\n".join(_detail(st)) if cmd == "show" else _summary(st))
    return 0
