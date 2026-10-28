"""``arc ml study`` (card E27.7, PLAN D133): validate / run / adopt / queue / stop / drop /
show for ``ML-<n>`` model studies. Mirrors ``arc backtest study`` (E7.12/E26.2)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.queue import DEFAULT_QUEUE

if TYPE_CHECKING:
    import argparse

    from arc.backtest.study import LedgerEntry
    from arc.ml.study import MlStudySpec
    from arc.routines.config import MlStudiesSettings

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATA = REPO / "data"


def _now(s: str) -> dt.datetime:
    from arc.utils.calendar import ET

    v = dt.datetime.fromisoformat(s)
    return v if v.tzinfo else v.replace(tzinfo=ET)


def _settings(args: argparse.Namespace) -> MlStudiesSettings:
    from arc.routines.config import load_routines

    return load_routines(args.config).backtest_studies.ml_studies


def _bt_ledger(data: Path) -> list[LedgerEntry]:
    """The BT research ledger (its runtimes price the BT entries of the shared ETA)."""
    from arc.backtest.study import load_ledger

    return load_ledger(data / "backtest" / "ledger.jsonl")


def _say(s: str) -> None:
    sys.stderr.write(s + "\n")


def add_ml_study_parser(msub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    st = msub.add_parser(
        "study", help="E27.7: pre-registered ML studies (ML-<n>, dev only, shared runner)"
    )
    ss = st.add_subparsers(dest="study_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data", type=Path, default=DEFAULT_DATA)
        p.add_argument("--models", type=Path, default=None, help="Default <data>/ml/models")
        p.add_argument("--ledger", type=Path, default=None, help="Default <data>/ml/studies/…")
        p.add_argument("--studies-dir", type=Path, default=None, help="config/ml_studies")
        p.add_argument("--config", type=Path, default=REPO / "config" / "routines.yaml")

    va = ss.add_parser("validate", help="Validate a spec (overlay, rule, incumbent, grid)")
    va.add_argument("spec", type=Path)
    common(va)
    va.add_argument("--json", action="store_true")

    run = ss.add_parser(
        "run", help="Run the next rung(s): ML-<n> from the studies dir, or --spec for ML-0"
    )
    run.add_argument("id", nargs="?", default=None)
    run.add_argument("--spec", type=Path, default=None, help="A scratch spec (id ML-0)")
    run.add_argument(
        "--rung", choices=["dev_2022", "dev_2023", "verdict"], default=None, help="One rung"
    )
    common(run)
    run.add_argument("--runs-dir", type=Path, default=None, help="Default <data>/ml/studies")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--now", type=_now, default=None, help="Run timestamp (tests)")
    run.add_argument(
        "--ignore-window",
        action="store_true",
        help="Don't yield to the E26.3 runner (21:30–08:30 ET or data/backtest/queue.json)",
    )
    run.add_argument("--json", action="store_true")

    ad = ss.add_parser(
        "adopt",
        help="Commit an owner-approved Analyst draft ML study (auto-merged PR on ml/<n>) "
        "and queue it for the nightly runner (shared weekly cap)",
    )
    ad.add_argument("id")
    ad.add_argument("--spec", type=Path, required=True, help="The draft (RUN_DIR/ml-study-specs)")
    ad.add_argument("--approval", required=True, help="slack:<ts> of `approve ML-<n>`")
    ad.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    ad.add_argument("--expect-hash", default=None)
    ad.add_argument("--dry-run", action="store_true", help="Validate only; write nothing")
    ad.add_argument("--dry-run-pr", action="store_true", help="Skip the PR step only")
    ad.add_argument("--no-fetch", action="store_true", help="Do not read origin/main")
    ad.add_argument("--now", type=_now, default=None)
    ad.add_argument("--json", action="store_true")
    common(ad)

    qu = ss.add_parser("status", help="The shared study queue, BT + ML (rung, nights, ETA)")
    qu.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    qu.add_argument("--json", action="store_true")
    common(qu)

    vd = ss.add_parser("verdict", help="A study's verdict (verdict.json) or where its ladder is")
    vd.add_argument("id")
    vd.add_argument("--runs-dir", type=Path, default=None, help="Default <data>/ml/studies")
    vd.add_argument("--json", action="store_true")
    common(vd)

    sp = ss.add_parser("stop", help="Stop a queued ML study")
    sp.add_argument("id")
    sp.add_argument("reason")
    sp.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    sp.add_argument("--json", action="store_true")

    dr = ss.add_parser("drop", help="Drop an unqueued draft ML-<n> (the number stays a gap)")
    dr.add_argument("id")
    dr.add_argument("reason")
    dr.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    dr.add_argument("--now", type=_now, default=None)
    common(dr)

    sh = ss.add_parser("show", help="ML study ledger lines (one study or all)")
    sh.add_argument("id", nargs="?", default=None)
    sh.add_argument("--json", action="store_true")
    common(sh)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    from arc.ml.study import STUDIES_DIR

    data = Path(args.data)
    models = Path(args.models) if args.models else data / "ml" / "models"
    ledger = Path(args.ledger) if args.ledger else data / "ml" / "studies" / "ledger.jsonl"
    studies = Path(args.studies_dir) if args.studies_dir else STUDIES_DIR
    return data, models, ledger, studies


def run_ml_study(args: argparse.Namespace) -> int:
    return {
        "validate": _validate,
        "run": _run,
        "adopt": _adopt,
        "status": _status,
        "verdict": _verdict,
        "stop": _stop,
        "drop": _drop,
        "show": _show,
    }[args.study_command](args)


def _validate(args: argparse.Namespace) -> int:
    from arc.ml.study import load_ml_spec, ml_study_hash, resolve_spec, variants

    cfg = _settings(args)
    _, models, _, _ = _paths(args)
    try:
        spec = resolve_spec(load_ml_spec(args.spec), models, max_grid=cfg.max_grid)
    except ValueError as exc:
        sys.stderr.write(f"arc ml study validate: {exc}\n")
        return 2
    doc = {
        "id": spec.id,
        "family": spec.family,
        "incumbent": spec.incumbent,
        "rule": spec.decision_rule.kind,
        "variants": len(variants(spec)),
        "spec_hash": ml_study_hash(spec),
    }
    if args.json:
        sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"ok: {spec.id} {spec.family} incumbent {spec.incumbent} · {doc['variants']} "
            f"variant(s) · rule {doc['rule']} · sha256 {doc['spec_hash']}\n"
        )
    return 0


def _load_run_spec(args: argparse.Namespace, studies: Path) -> MlStudySpec:
    from arc.ml.study import SCRATCH_ID, MlStudyError, load_ml_spec

    if args.spec is not None:
        spec = load_ml_spec(args.spec)
        if spec.id != SCRATCH_ID:
            msg = f"--spec runs scratch specs only (id {SCRATCH_ID}); {spec.id} runs by id"
            raise MlStudyError(msg)
        return spec
    if not args.id:
        msg = "name a study (ML-<n>) or --spec <scratch.yaml>"
        raise MlStudyError(msg)
    return load_ml_spec(studies / f"{args.id}.yaml")


def _run(args: argparse.Namespace) -> int:
    from arc.ml.cli import _git_sha
    from arc.ml.study import (
        MlStudyError,
        StudyEnv,
        ml_rung_line,
        resolve_spec,
        run_ladder,
        run_rung,
    )
    from arc.utils.calendar import now_et

    cfg = _settings(args)
    data, models, ledger, studies = _paths(args)
    queue = data / "backtest" / "queue.json"

    def stop() -> bool:
        """Yield to the E26.3 runner (21:30–08:30 ET or its queue file) unless
        --ignore-window (the runner itself launches rungs with it)."""
        if args.ignore_window:
            return False
        t = now_et().time()
        return t >= dt.time(21, 30) or t < dt.time(8, 30) or queue.exists()

    if stop():
        sys.stderr.write(
            "arc ml study run: the E26.3 runner's window is open (21:30–08:30 ET or "
            f"{queue} exists); yielding. --ignore-window to override.\n"
        )
        return 4
    try:
        spec = resolve_spec(_load_run_spec(args, studies), models, max_grid=cfg.max_grid)
    except (ValueError, MlStudyError) as exc:
        sys.stderr.write(f"arc ml study run: {exc}\n")
        return 2
    env = StudyEnv(
        data_dir=data,
        models_root=models,
        ledger=ledger,
        runs_dir=Path(args.runs_dir) if args.runs_dir else data / "ml" / "studies",
        code_sha=_git_sha(),
        workers=args.workers or cfg.workers,
        say=_say,
        stop=stop,
    )
    try:
        if args.rung:
            line = run_rung(
                spec,
                args.rung,
                env,
                now=args.now or now_et(),
                fail_margin=cfg.fail_margin,
            )
            lines = [line] if line is not None else []
        else:
            lines = run_ladder(
                spec,
                env,
                clock=lambda: args.now or now_et(),
                fail_margin=cfg.fail_margin,
            )
    except MlStudyError as exc:
        sys.stderr.write(f"arc ml study run: {exc}\n")
        return 2
    for ln in lines:
        if args.json:
            sys.stdout.write(ln.model_dump_json() + "\n")
        else:
            sys.stdout.write(ml_rung_line(ln) + "\n")
    return 0 if lines else 4


def _adopt(args: argparse.Namespace) -> int:
    from arc.backtest.study_adopt import AdoptStudyError
    from arc.experiments.publish import GitMainView, GitPublisher, PublishError
    from arc.ml.study import adopt_ml_study
    from arc.utils.calendar import now_et

    cfg = _settings(args)
    data, models, ledger, studies = _paths(args)
    try:
        res = adopt_ml_study(
            args.id,
            args.spec,
            approval=args.approval,
            now=args.now or now_et(),
            queue_path=args.queue,
            ledger=ledger,
            models_root=models,
            max_grid=cfg.max_grid,
            default_rung_s=cfg.default_rung_s,
            studies_dir=studies,
            main=None if args.no_fetch else GitMainView(),
            publisher=None if (args.dry_run or args.dry_run_pr) else GitPublisher(),
            expect_hash=args.expect_hash,
            dry_run=args.dry_run,
            dry_run_pr=args.dry_run_pr,
            bt_ledger=_bt_ledger(data),
        )
    except (AdoptStudyError, PublishError) as exc:
        sys.stderr.write(f"arc ml study adopt: refused: {exc}\n")
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
        out += ["  spec file:", *("    " + x for x in res.file_text.splitlines())]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def _status(args: argparse.Namespace) -> int:
    """The one queue, both kinds, oldest first, with each entry's ETA (BT priced from
    the BT ledger, ML from the ML ledger)."""
    from arc.backtest.queue import eta_rows, load_queue
    from arc.ml.study import load_ml_ledger, load_ml_spec, queue_ml_estimates

    cfg = _settings(args)
    data, _, ledger, studies = _paths(args)
    q = load_queue(args.queue)
    specs = {}
    for e in q.entries:
        p = studies / f"{e.id}.yaml"
        if e.kind == "ml" and p.is_file():
            specs[e.id] = load_ml_spec(p)
    est = queue_ml_estimates(
        q,
        load_ml_ledger(ledger),
        specs,
        default_s=cfg.default_rung_s,
    )
    rows = eta_rows(q, _bt_ledger(data), est)
    if args.json:
        sys.stdout.write(json.dumps({"entries": rows}, sort_keys=True, default=str) + "\n")
        return 0
    if not rows:
        sys.stdout.write(f"study queue empty ({args.queue})\n")
    for r in rows:
        eta = r["eta_nights"]
        sys.stdout.write(
            f"{r['id']:<6} {r['kind']} [{r['status']}] next {r['ladder_state'] or '-'} · "
            f"nights so far {r['nights']} · ETA "
            f"{eta if eta is not None else '?' if r['queue_position'] else '-'} night(s)"
            + (f" · {r['stop_reason']}" if r["stop_reason"] else "")
            + "\n"
        )
    return 0


def _verdict(args: argparse.Namespace) -> int:
    from arc.ml.study import load_ml_ledger, ml_rung_line

    data, _, ledger, _ = _paths(args)
    runs = Path(args.runs_dir) if args.runs_dir else data / "ml" / "studies"
    f = runs / args.id / "verdict.json"
    if f.is_file():
        doc = json.loads(f.read_text())
        if args.json:
            sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
            return 0
        from arc.ml.study import MlLedgerEntry

        line = MlLedgerEntry.model_validate(doc)
        sys.stdout.write(ml_rung_line(line) + "\n")
        for y, d in sorted(line.rule_years.items()):
            sys.stdout.write(f"  {y}: {'pass' if d.get('pass') else 'fail'}\n")
        return 0
    rows = [e for e in load_ml_ledger(ledger) if e.study == args.id]
    if not rows:
        sys.stderr.write(f"{args.id}: no verdict and no ledger lines yet\n")
        return 1
    sys.stdout.write(f"{args.id}: no verdict yet; last rung {ml_rung_line(rows[-1])}\n")
    return 0


def _stop(args: argparse.Namespace) -> int:
    from arc.backtest.queue import QueueError, load_queue, stop_entry, update_queue

    e0 = load_queue(args.queue).get(args.id)
    if e0 is not None and e0.kind != "ml":
        sys.stderr.write(f"arc ml study stop: {args.id} is a backtest study\n")
        return 3
    try:
        e = update_queue(args.queue, lambda q: stop_entry(q, args.id, args.reason))
    except QueueError as exc:
        sys.stderr.write(f"arc ml study stop: refused: {exc}\n")
        return 3
    if args.json:
        sys.stdout.write(json.dumps(e.model_dump(mode="json"), sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"{e.id} -> stopped ({e.stop_reason})\n")
    return 0


def _drop(args: argparse.Namespace) -> int:
    from arc.backtest.study_adopt import AdoptStudyError
    from arc.ml.study import drop_ml_study, ml_rung_line
    from arc.utils.calendar import now_et

    _, _, ledger, _ = _paths(args)
    try:
        line = drop_ml_study(
            args.id, args.reason, now=args.now or now_et(), queue_path=args.queue, ledger=ledger
        )
    except AdoptStudyError as exc:
        sys.stderr.write(f"arc ml study drop: refused: {exc}\n")
        return 3
    sys.stdout.write(ml_rung_line(line) + "\n")
    return 0


def _show(args: argparse.Namespace) -> int:
    from arc.ml.study import load_ml_ledger, ml_rung_line

    _, _, ledger, _ = _paths(args)
    rows = [e for e in load_ml_ledger(ledger) if args.id is None or e.study == args.id]
    for e in rows:
        sys.stdout.write((e.model_dump_json() if args.json else ml_rung_line(e)) + "\n")
    if not rows and not args.json:
        sys.stdout.write(f"no ML study ledger lines ({ledger})\n")
    return 0
