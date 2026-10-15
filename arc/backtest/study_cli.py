"""``arc backtest study run|show|ledger`` (card E7.12, PLAN D84): the study framework CLI."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.pull import CheckpointIncompleteError
from arc.backtest.study import (
    DEFAULT_LEDGER,
    DEFAULT_REPORT_DIR,
    DEFAULT_RUNS_DIR,
    StudyRefusedError,
    arm_set_hash,
    check_holdout,
    ladder_flags,
    load_ledger,
    load_study,
    run_study,
    study_hash,
    study_path,
    variants,
    verdict_lines,
)

if TYPE_CHECKING:
    import argparse

__all__ = ["add_study_parser", "run_study_cli"]


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _now(s: str) -> dt.datetime:
    from arc.utils.calendar import ET

    v = dt.datetime.fromisoformat(s)
    return v if v.tzinfo else v.replace(tzinfo=ET)


def add_study_parser(bs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    st = bs.add_parser(
        "study",
        help="E7.12: pre-registered backtest studies (dev/holdout, ladder, research ledger)",
    )
    ss = st.add_subparsers(dest="study_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("id", help="Study id, e.g. BT-1 (config/backtest_studies/BT-1.yaml)")
        p.add_argument("--studies-dir", type=Path, default=None, help="Spec directory")
        p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Research ledger")

    run = ss.add_parser("run", help="Run a study (dev by default; --holdout once per spec)")
    common(run)
    run.add_argument(
        "--holdout",
        action="store_true",
        help="Run the locked holdout period (refused after one run per spec hash)",
    )
    run.add_argument(
        "--allow-overfit-warning",
        action="store_true",
        help="Run the holdout although variants > max_variants (stamped in the report)",
    )
    run.add_argument(
        "--universe",
        choices=["c125", "c250", "c500"],
        default=None,
        help="Ladder rung (default: first rung for dev, last rung for holdout)",
    )
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help="Per-run outputs")
    run.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help="Where <id>.md + <id>.json go (the latest run of the study)",
    )
    run.add_argument("--no-report", action="store_true", help="Skip the docs/RESEARCH copy")
    run.add_argument("--tickers", default=None, help="Comma list (default: the checkpoint)")
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--no-cache", action="store_true", help="Bypass the menu cache")
    run.add_argument("--cache-dir", type=Path, default=None)
    run.add_argument(
        "--latest", type=_date, default=None, help="Last data session (default: last session)"
    )
    run.add_argument("--now", type=_now, default=None, help="Run timestamp (tests)")

    show = ss.add_parser("show", help="Spec, its hash, variants tried and holdout state")
    common(show)
    show.add_argument(
        "--json",
        action="store_true",
        help="One JSON object (E7.15: the Analyst gate reads the current spec hash from it)",
    )

    led = ss.add_parser("ledger", help="Ledger lines of a study (or 'all')")
    common(led)
    led.add_argument("--json", action="store_true", help="Raw JSON lines")


def run_study_cli(args: argparse.Namespace) -> int:
    cmd = args.study_command
    if cmd == "ledger":
        return _ledger(args)
    path = study_path(args.id, args.studies_dir)
    if not path.is_file():
        sys.stderr.write(f"arc backtest study: no spec {path}\n")
        return 2
    try:
        spec = load_study(path)
    except ValueError as exc:
        sys.stderr.write(f"arc backtest study: {exc}\n")
        return 2
    if cmd == "show":
        return _show(args, spec)
    return _run(args, spec)


def _show(args: argparse.Namespace, spec: object) -> int:
    from arc.backtest.study import StudySpec

    assert isinstance(spec, StudySpec)
    entries = load_ledger(args.ledger)
    h, a = study_hash(spec), arm_set_hash(spec)
    mine = [e for e in entries if e.study == spec.id]
    hold = [e for e in mine if e.phase == "holdout"]
    try:
        check_holdout(spec, entries, allow_overfit_warning=False)
        state = "holdout open"
    except StudyRefusedError as exc:
        state = f"holdout refused: {exc}"
    if getattr(args, "json", False):
        doc = {
            "id": spec.id,
            "hypothesis": spec.hypothesis,
            "spec_hash": h,
            "arm_set_hash": a,
            "arms": spec.arms.model_dump(mode="json"),
            "profiles": list(spec.profiles),
            "universe_ladder": list(spec.universe_ladder),
            "primary_metric": spec.primary_metric,
            "max_variants": spec.max_variants,
            "variants_tried": variants(entries, spec.id),
            "runs": len(mine),
            "holdout_runs": len(hold),
            "holdout_state": state,
        }
        sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
        return 0
    out = [
        f"{spec.id} · {spec.hypothesis}",
        f"spec hash {h}",
        f"arm set   {a}",
        f"profiles {', '.join(spec.profiles)}; ladder {', '.join(spec.universe_ladder)}; "
        f"primary {spec.primary_metric}",
        f"dev {spec.period.dev_start} -> {spec.period.dev_end}; holdout "
        f"{spec.period.holdout_start} -> {spec.period.holdout_end or 'latest'}",
        f"rule: {spec.decision_rule.text()}",
        f"runs {len(mine)} (holdout {len(hold)}); variants tried "
        f"{variants(entries, spec.id)} of max {spec.max_variants} (this arm set "
        f"{'already counted' if any(e.arm_set_hash == a for e in mine) else 'new'})",
        state,
    ]
    for row in ladder_flags(entries, spec):
        flip = " SIGN FLIP" if row["sign_flip_vs_first_rung"] else ""
        out.append(
            f"  {row['at']} {row['phase']:7} {row['universe']} {row['profile']} "
            f"{row['treatment']}: {row['primary_diff']:+g} CI [{row['ci_lo']}, "
            f"{row['ci_hi']}] switch={row['switch']}{flip}"
        )
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def _ledger(args: argparse.Namespace) -> int:
    entries = load_ledger(args.ledger)
    sel = entries if args.id == "all" else [e for e in entries if e.study == args.id]
    for e in sel:
        if args.json:
            sys.stdout.write(e.model_dump_json() + "\n")
            continue
        sys.stdout.write(
            f"{e.at:%Y-%m-%d %H:%M} {e.study} {e.phase:7} {e.universe} "
            f"spec {e.spec_hash[:12]} arms {e.arm_set_hash[:12]} "
            f"{e.start} -> {e.end} git {e.git_sha} variants {e.variants}"
            + (" OVERFIT-WARNING" if e.overfit_warning else "")
            + "\n"
        )
        for p, tr in e.verdicts.items():
            for t, v in tr.items():
                sys.stdout.write(
                    f"    {p} {t}: switch={v['switch']} won {v['subperiods_won']} "
                    f"P&L diff {v['pnl_diff']:,.0f} CI [{v['ci_lo']:,.0f}, {v['ci_hi']:,.0f}]\n"
                )
    if not sel:
        sys.stdout.write(f"no ledger lines for {args.id} in {args.ledger}\n")
    return 0


def _run(args: argparse.Namespace, spec: object) -> int:
    from arc.backtest.study import StudySpec
    from arc.config import ArcSettings
    from arc.utils.calendar import now_et, previous_session

    assert isinstance(spec, StudySpec)
    now = args.now or now_et()
    latest = args.latest or previous_session(now.date())
    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    )
    try:
        entry = run_study(
            spec,
            phase="holdout" if args.holdout else "dev",
            universe=args.universe,
            now=now,
            latest=latest,
            data_dir=args.data_dir,
            ledger=args.ledger,
            runs_dir=args.runs_dir,
            report_dir=None if args.no_report else args.report_dir,
            settings=ArcSettings(),
            allow_overfit_warning=args.allow_overfit_warning,
            tickers=tickers,
            workers=args.workers,
            cache=not args.no_cache,
            cache_dir=args.cache_dir,
        )
    except (StudyRefusedError, CheckpointIncompleteError) as exc:
        sys.stderr.write(f"arc backtest study: refused: {exc}\n")
        return 3
    payload = json.loads((Path(entry.run_dir) / "report.json").read_text())
    sys.stdout.write((Path(entry.run_dir) / "report.md").read_text())
    sys.stdout.write("\n" + "\n".join(verdict_lines(payload)) + "\n")
    sys.stdout.write(f"\nledger: {args.ledger} · run dir: {entry.run_dir}\n")
    return 0
