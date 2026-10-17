"""``arc backtest study run|show|ledger`` (card E7.12, PLAN D84): the study framework CLI.

E26.2 (D121) adds ``adopt`` (commit an owner-approved Analyst draft via an auto-merged PR
and queue it), ``queue`` (the study queue with ETAs) and ``stop`` (the owner's
``stop BT-<n> <why>``)."""

from __future__ import annotations

import datetime as dt
import json
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.pull import CheckpointIncompleteError
from arc.backtest.queue import DEFAULT_QUEUE
from arc.backtest.study import (
    DEFAULT_LEDGER,
    DEFAULT_REPORT_DIR,
    DEFAULT_RUNS_DIR,
    RetiredStudy,
    StudyRefusedError,
    arm_set_hash,
    check_holdout,
    ladder_flags,
    load_ledger,
    load_retired,
    load_study,
    retired_message,
    run_study,
    study_hash,
    study_path,
    variants,
    verdict_lines,
)

if TYPE_CHECKING:
    import argparse

__all__ = ["INTERRUPTED_RC", "StudyInterruptedError", "add_study_parser", "run_study_cli"]

#: ``study run`` exit code on SIGTERM (128 + 15): the E26.3 runner's ``interrupted``.
INTERRUPTED_RC = 143


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

    ni = ss.add_parser(
        "night",
        help="E26.3: work the study queue now, like the nightly backtest.studies routine",
    )
    ni.add_argument(
        "--minutes",
        type=float,
        default=None,
        help="End the window this many minutes from now (default: the configured window)",
    )
    ni.add_argument("--config", type=Path, default=None, help="routines.yaml")
    ni.add_argument("--queue", type=Path, default=None, help="Override backtest_studies.queue")
    ni.add_argument("--ledger", type=Path, default=None, help="Override backtest_studies.ledger")
    ni.add_argument("--runs-dir", type=Path, default=None)
    ni.add_argument("--report-dir", type=Path, default=None)
    ni.add_argument("--data-dir", type=Path, default=None)
    ni.add_argument("--main-ref", default="origin/main", help="Ref the committed specs come from")
    ni.add_argument("--no-fetch", action="store_true", help="Don't `git fetch` the ref first")
    ni.add_argument(
        "--slack", action="store_true", help="Post the event lines to #arc-analyst (else print)"
    )

    rt = ss.add_parser(
        "retire",
        help="E26.5 (D127): append a study to config/backtest_studies/retired.yaml "
        "(history only; commit the file via a PR)",
    )
    rt.add_argument("id", help="BT-<n>")
    rt.add_argument("--reason", required=True, help="Why it no longer describes the algo")
    rt.add_argument("--by", default="owner", help="Who retired it (default owner)")
    rt.add_argument("--at", type=_date, default=None, help="Date (default: today ET)")
    rt.add_argument("--studies-dir", type=Path, default=None, help="Spec directory")
    rt.add_argument("--dry-run", action="store_true", help="Print the new file; write nothing")


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
    if cmd == "night":
        return _night(args)
    if cmd == "retire":
        return _retire(args)
    path = study_path(args.id, args.studies_dir)
    if not path.is_file():
        sys.stderr.write(f"arc backtest study: no spec {path}\n")
        return 2
    try:
        spec = load_study(path)
        retired = load_retired(args.studies_dir).get(args.id)
    except ValueError as exc:
        sys.stderr.write(f"arc backtest study: {exc}\n")
        return 2
    if cmd == "show":
        return _show(args, spec, retired)
    if retired is not None:  # E26.5 (D127): history only, never re-run
        sys.stderr.write(f"arc backtest study run: refused: {retired_message(retired)}\n")
        return 2
    return _run(args, spec)


def _show(args: argparse.Namespace, spec: object, retired: RetiredStudy | None = None) -> int:
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
            "retired": (
                {"at": retired.at.isoformat(), "by": retired.by, "reason": retired.reason}
                if retired
                else None
            ),
        }
        sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
        return 0
    out = [
        f"{spec.id} · {spec.hypothesis}",
        *([f"RETIRED: {retired_message(retired)}"] if retired else []),
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


def _night(args: argparse.Namespace) -> int:
    """E26.3: one study night now (manual / live evidence). ``--minutes`` sets the
    window end; otherwise the configured window applies (outside it: exit 4)."""
    import time as _time

    from arc.backtest.runner import RunnerPaths, SkipNightError, run_night
    from arc.experiments.publish import REPO_ROOT, GitMainView
    from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines
    from arc.utils.calendar import now_et

    cfg = load_routines(args.config or DEFAULT_ROUTINES_PATH).backtest_studies

    def _p(override: Path | None, rel: str) -> Path:
        p = override or Path(rel)
        return p if p.is_absolute() else REPO_ROOT / p

    paths = RunnerPaths(
        queue=_p(args.queue, cfg.queue),
        ledger=_p(args.ledger, cfg.ledger),
        runs_dir=_p(args.runs_dir, cfg.runs_dir),
        report_dir=_p(args.report_dir, cfg.report_dir),
        data_dir=_p(args.data_dir, cfg.data_dir),
        cwd=REPO_ROOT,
    )
    post = _printer
    if args.slack:
        from arc.slack.client import CHANNEL_ARC_ANALYST, ArcSlackClient

        client = ArcSlackClient()

        def post(text: str) -> None:
            client.post_thread_root(channel=CHANNEL_ARC_ANALYST, text=text)

    end = None
    if args.minutes is not None:
        end = now_et() + dt.timedelta(minutes=args.minutes)
    try:
        res = run_night(
            cfg=cfg,
            paths=paths,
            clock=now_et,
            sleep=_time.sleep,
            main=GitMainView(repo=REPO_ROOT, ref=args.main_ref, fetch=not args.no_fetch),
            post=post,
            window_end=end,
        )
    except SkipNightError as exc:
        sys.stderr.write(f"arc backtest study night: skipped: {exc}\n")
        return 4
    sys.stdout.write(res.summary + "\n")
    return 0


def _retire(args: argparse.Namespace) -> int:
    """E26.5 (D127): append *id* to ``retired.yaml`` (refuses a malformed id or a repeat)."""
    from arc.backtest.study import RETIRED_FILE, STUDIES_DIR, retired_text
    from arc.utils.calendar import now_et

    d = args.studies_dir or STUDIES_DIR
    try:
        rows = load_retired(d)
        if args.id in rows:
            msg = f"{args.id} is already retired ({rows[args.id].at}: {rows[args.id].reason})"
            raise ValueError(msg)
        new = RetiredStudy(
            id=args.id, at=args.at or now_et().date(), by=args.by, reason=args.reason
        )
    except ValueError as exc:
        sys.stderr.write(f"arc backtest study retire: refused: {exc}\n")
        return 3
    text = retired_text([*rows.values(), new])
    if args.dry_run:
        sys.stdout.write(text)
        return 0
    (d / RETIRED_FILE).write_text(text)
    sys.stdout.write(f"{retired_message(new)}\nwrote {d / RETIRED_FILE}: commit it via a PR\n")
    return 0


def _printer(text: str) -> None:
    sys.stdout.write(f"[#arc-analyst dry-run] {text}\n")
    sys.stdout.flush()


class StudyInterruptedError(BaseException):
    """SIGTERM during ``study run`` (E26.3): the run stops with no ledger line.

    A ``BaseException`` so no ``except Exception`` in the build/walk swallows it."""


def _on_sigterm(signum: int, frame: object) -> None:
    import multiprocessing

    for child in multiprocessing.active_children():  # the menu-build pool workers
        child.terminate()
    raise StudyInterruptedError


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
    # E26.3: the nightly runner stops a run with SIGTERM at the window end; exit 143
    # with no ledger line (run_study holds SIGTERM while the line + report are written).
    old = signal.signal(signal.SIGTERM, _on_sigterm)
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
    except StudyInterruptedError:
        sys.stderr.write(f"arc backtest study: {spec.id} interrupted (SIGTERM); no ledger line\n")
        return INTERRUPTED_RC
    finally:
        signal.signal(signal.SIGTERM, old)
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
