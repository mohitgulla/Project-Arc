"""``arc backtest study run|show|ledger`` (card E7.12, PLAN D84): the study framework CLI.

E26.2 (D121) adds ``adopt`` (commit an owner-approved Analyst draft via an auto-merged PR
and queue it), ``queue`` (the study queue with ETAs) and ``stop`` (the owner's
``stop BT-<n> <why>``)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.pull import CheckpointIncompleteError
from arc.backtest.queue import DEFAULT_QUEUE
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

    ad = ss.add_parser(
        "adopt",
        help="E26.2 (D121): commit an owner-approved Analyst draft study (auto-merged PR "
        "on bt/<n>) and queue it for the nightly runner",
    )
    common(ad)
    ad.add_argument("--spec", type=Path, required=True, help="The draft (RUN_DIR/study-specs)")
    ad.add_argument(
        "--approval", required=True, help="slack:<ts> of the owner's `approve BT-<n>` message"
    )
    ad.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="The study queue file")
    ad.add_argument(
        "--expect-hash", default=None, help="Refuse unless the draft still has this spec hash"
    )
    ad.add_argument("--dry-run", action="store_true", help="Validate only; write nothing")
    ad.add_argument(
        "--dry-run-pr", action="store_true", help="Skip the PR step only (queue is written)"
    )
    ad.add_argument("--no-fetch", action="store_true", help="Do not read origin/main")
    ad.add_argument("--now", type=_now, default=None, help="Approval time (tests)")
    ad.add_argument("--json", action="store_true", help="One JSON object")

    qu = ss.add_parser("queue", help="E26.2: the study queue (status, rung, nights, ETA)")
    qu.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    qu.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Research ledger")
    qu.add_argument("--json", action="store_true", help="One JSON object")

    sp = ss.add_parser("stop", help="E26.2: owner stop of a queued/running study (D121)")
    sp.add_argument("id", help="BT-<n>")
    sp.add_argument("--reason", required=True, help="The owner's reason")
    sp.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    sp.add_argument("--json", action="store_true", help="One JSON object")


def run_study_cli(args: argparse.Namespace) -> int:
    cmd = args.study_command
    if cmd == "ledger":
        return _ledger(args)
    if cmd == "adopt":
        return _adopt(args)
    if cmd == "queue":
        return _queue(args)
    if cmd == "stop":
        return _stop(args)
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


def _adopt(args: argparse.Namespace) -> int:
    from arc.backtest.study_adopt import AdoptStudyError, adopt_study
    from arc.experiments.publish import GitMainView, GitPublisher, PublishError
    from arc.utils.calendar import now_et

    try:
        res = adopt_study(
            args.id,
            args.spec,
            approval=args.approval,
            now=args.now or now_et(),
            queue_path=args.queue,
            ledger=args.ledger,
            studies_dir=args.studies_dir,
            main=None if args.no_fetch else GitMainView(),
            publisher=None if (args.dry_run or args.dry_run_pr) else GitPublisher(),
            expect_hash=args.expect_hash,
            dry_run=args.dry_run,
            dry_run_pr=args.dry_run_pr,
        )
    except (AdoptStudyError, PublishError) as exc:
        sys.stderr.write(f"arc backtest study adopt: refused: {exc}\n")
        return 3
    if args.json:
        sys.stdout.write(json.dumps(res.as_json(), sort_keys=True, default=str) + "\n")
        return 0
    head = "dry-run (nothing written)" if res.dry_run else ("queued" if res.created else "found")
    out = [
        f"{head}: {res.id} [{res.status}] spec sha256 {res.spec_hash}",
        f"  file: {res.path} (branch {res.branch})",
        f"  PR: {res.pr_url or '-'} ({res.pr_status})",
        f"  queue position {res.queue_position or '-'} · ETA "
        f"{res.eta_nights if res.eta_nights is not None else '?'} night(s)",
    ]
    if res.dry_run:
        out += ["  PR body:", *("    " + x for x in res.pr_body.splitlines())]
        out += ["  spec file:", *("    " + x for x in res.file_text.splitlines())]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def _queue(args: argparse.Namespace) -> int:
    from arc.backtest.queue import eta_rows, load_queue, rung_estimates

    entries = load_ledger(args.ledger)
    rows = eta_rows(load_queue(args.queue), entries)
    est = rung_estimates(entries)
    if args.json:
        doc = {"entries": rows, "rung_estimates": est, "queue": str(args.queue)}
        sys.stdout.write(json.dumps(doc, sort_keys=True, default=str) + "\n")
        return 0
    if not rows:
        sys.stdout.write(f"study queue empty ({args.queue})\n")
    for r in rows:
        eta = r["eta_nights"]
        sys.stdout.write(
            f"{r['id']} [{r['status']}] next {r['ladder_state'] or '-'} · nights so far "
            f"{r['nights']} · ETA {eta if eta is not None else '?' if r['queue_position'] else '-'}"
            f" night(s) · {r['pr_url'] or 'no PR'}"
            + (f" · {r['stop_reason']}" if r["stop_reason"] else "")
            + "\n"
        )
    return 0


def _stop(args: argparse.Namespace) -> int:
    from arc.backtest.queue import QueueError, stop_entry, update_queue

    try:
        e = update_queue(args.queue, lambda q: stop_entry(q, args.id, args.reason))
    except QueueError as exc:
        sys.stderr.write(f"arc backtest study stop: refused: {exc}\n")
        return 3
    if args.json:
        sys.stdout.write(json.dumps(e.model_dump(mode="json"), sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"{e.id} -> stopped ({e.stop_reason})\n")
    return 0
