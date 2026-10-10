"""``arc experiment``: the forward A/B experiment registry CLI (PLAN D44, E10.1).

- ``arc experiment create --spec <yaml>``    store a draft (defaults filled from
  ``config/experiments.yaml`` + D26 overrides); a draft may be re-created
- ``arc experiment register <id>``           hash-lock the spec; queues if its area is busy
- ``arc experiment list [--status S]``
- ``arc experiment show <id> [--json]``
- ``arc experiment verify <id>``             recompute the spec hash (exit 1 on mismatch)
- ``arc experiment stop <id> --reason R --actor A``  owner stop
- ``arc experiment report <id> [--stored] [--now ISO]``  the E10.3 evaluation (read-only:
  computed as of now, or the latest stored report)
- ``arc experiment evaluate [<id>] [--now ISO]``  run the daily evaluation now: stores
  the report and applies its verdict (the ``experiments.evaluate`` routine does this)

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
    r = common(esub.add_parser("register", help="Pre-register: hash-lock the spec"))
    r.add_argument("experiment_id")
    r.add_argument("--actor", default=LOCAL_ACTOR)
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
    ev = common(esub.add_parser("evaluate", help="Evaluate running experiments; apply verdicts"))
    ev.add_argument("experiment_id", nargs="?", default=None)
    ev.add_argument("--now", default=None, help="Evaluate as of this ISO time (default: now)")
    pl = common(
        esub.add_parser(
            "plan", help="D69: sessions needed for K arms with Holm at the A/A sigma (read-only)"
        )
    )
    pl.add_argument("spec", help="config/experiments/live/<x>.yaml, or a stored experiment id")
    pl.add_argument("--sigma", type=float, default=None, help="sigma of d_t (default: A/A's)")
    pl.add_argument(
        "--effect", type=float, default=None, help="daily effect to detect (default: spec mde)"
    )
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
        lines.append(f"    #{e.id} {e.at:%Y-%m-%d %H:%M %Z} {e.status.value}{reason} by {e.actor}")
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
        f"equity {r.t0_equity:,.2f}; legacy book {len(r.legacy_book)}",
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
    c = r.calibration
    lines.append(
        f"  calibration: sigma {_pct(c.sigma)}  MDE "
        + ", ".join(f"{n}:{_pct(v)}" for n, v in c.mde_fixed.items())
        + "  always-valid MDE "
        + ", ".join(f"{n}:{_pct(v)}" for n, v in c.mde_always_valid.items())
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


def _run_plan(args: argparse.Namespace, store: Any, conn: sqlite3.Connection) -> int:
    from pathlib import Path

    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.overlay import fill_defaults, load_spec

    defaults = experiments_config(effective_settings(conn)).defaults
    if Path(args.spec).is_file():
        spec = fill_defaults(load_spec(args.spec), defaults)
    else:
        spec = store.require(args.spec).spec
    sigma = args.sigma if args.sigma is not None else store.aa_sigma()
    if sigma is None or sigma <= 0:
        _err(
            "arc experiment plan: no A/A sigma recorded yet (no A/A stopped with one); "
            "pass --sigma <fraction of t0 equity>"
        )
        return 2
    assert spec.alpha is not None and spec.power is not None  # noqa: S101 - filled
    assert spec.min_sessions is not None and spec.max_sessions is not None  # noqa: S101
    effect = args.effect if args.effect is not None else spec.mde
    k = len(spec.arms.names)
    rows = plan_rows(
        sigma=sigma, effect=effect, k_max=max(k, 4), alpha=spec.alpha,
        power=spec.power, min_sessions=spec.min_sessions, max_sessions=spec.max_sessions,
        mde=spec.mde,
    )  # fmt: skip
    if args.json:
        _out(
            json.dumps(
                {
                    "experiment_id": spec.id,
                    "k": k,
                    "sigma": sigma,
                    "sigma_source": "flag" if args.sigma is not None else "aa",
                    "effect": effect,
                    "alpha": spec.alpha,
                    "power": spec.power,
                    "min_sessions": spec.min_sessions,
                    "max_sessions": spec.max_sessions,
                    "rows": rows,
                },
                indent=2,
            )
        )
        return 0
    _out(
        f"{spec.id}  K={k} treatment(s)  sigma {_pct(sigma)}/day "
        f"({'--sigma' if args.sigma is not None else 'A/A'})  effect "
        f"{_pct(effect) if effect is not None else '- (no spec mde; pass --effect)'}  "
        f"alpha {spec.alpha} (Holm)  power {spec.power:.0%}  "
        f"window {spec.min_sessions}-{spec.max_sessions}"
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
    return 0


def _run_eval(args: argparse.Namespace, store: Any, conn: sqlite3.Connection) -> int:
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.evaluate import build_report, evaluate, evaluate_running, latest_report
    from arc.experiments.models import ExperimentStatus

    now = _parse_now(args.now)
    cfg = experiments_config(effective_settings(conn))
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
            reports = [evaluate(store, args.experiment_id, cfg, now=now)]
        else:
            reports = evaluate_running(store, cfg, now=now)
    if args.json:
        payload = [r.model_dump(mode="json") for r in reports]
        _out(json.dumps(payload[0] if args.experiment_command == "report" else payload, indent=2))
    else:
        for r in reports:
            _out("\n".join(report_lines(r)))
        if not reports:
            _out("no running experiments")
    return 0


def run_experiment(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from arc.experiments.models import ExperimentStatus, StopDetail, StopReason
    from arc.experiments.store import ExperimentError, ExperimentStore

    cmd = args.experiment_command
    conn = _open(args.db)
    try:
        from arc.control.effective import effective_settings, experiments_config

        # D69: register/stop queue and promote within experiments.runner.max_parallel_arms
        runner = experiments_config(effective_settings(conn)).runner
        store = ExperimentStore.for_runner(conn, runner)
        if cmd == "create":
            from arc.experiments.overlay import fill_defaults, load_spec

            try:
                spec = load_spec(args.spec)
            except (ValidationError, ValueError, OSError) as exc:
                _err(f"arc experiment create: invalid spec {args.spec}: {exc}")
                return 2
            spec = fill_defaults(spec, experiments_config(effective_settings(conn)).defaults)
            st = store.create(spec, actor=args.actor)
        elif cmd == "register":
            st = store.register(args.experiment_id, actor=args.actor)
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
        elif cmd in ("start", "pair", "arms-tick"):
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
        else:  # pragma: no cover - argparse enforces the choice
            return 2
    except ExperimentError as exc:
        _err(f"arc experiment {cmd}: {exc}")
        return 2
    finally:
        conn.close()
    if args.json:
        _out(json.dumps(_state_json(st), indent=2))
    else:
        _out("\n".join(_detail(st)) if cmd == "show" else _summary(st))
    return 0
