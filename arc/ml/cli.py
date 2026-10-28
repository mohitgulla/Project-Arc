"""``arc ml`` (E27.1, D126): the ML feature store.

    arc ml features list [--json]
    arc ml features build --universe c500 --from 2020-01-02 --to 2026-10-09 [--data D] [--out O]
    arc ml features parity --day D [--tickers A,B,...] [--db P] [--data D]

``build`` writes ``data/ml/features/v<schema>/date=<d>.parquet`` from the history store
(resumable). ``parity`` computes one day from the live store (read-only) and from the
history store and prints the max abs diff per feature against its tolerance.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATA = REPO / "data"


def add_ml_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("ml", help="ML outcome-model programme: feature store (E27.1)")
    msub = p.add_subparsers(dest="ml_command", required=True)
    f = msub.add_parser("features", help="Point-in-time feature store")
    fsub = f.add_subparsers(dest="features_command", required=True)

    ls = fsub.add_parser("list", help="Print the feature registry")
    ls.add_argument("--json", action="store_true", help="JSON instead of a table")

    b = fsub.add_parser("build", help="Build the history feature panels (resumable)")
    b.add_argument("--universe", default="c500", help="Checkpoint (c125/c250/c500)")
    b.add_argument("--from", dest="start", type=_dt.date.fromisoformat, required=True)
    b.add_argument("--to", dest="end", type=_dt.date.fromisoformat, required=True)
    b.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    b.add_argument("--out", type=Path, default=None, help="Default <data>/ml/features")
    b.add_argument("--tickers", default=None, help="Comma list (overrides --universe)")

    q = fsub.add_parser("parity", help="Live vs history features on one real day")
    q.add_argument("--day", type=_dt.date.fromisoformat, required=True)
    q.add_argument("--tickers", default="AAPL,MU,NVDA,SPY,QQQ,IWM")
    q.add_argument("--db", type=Path, default=None, help="Live store (default data/arc.db)")
    q.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    q.add_argument("--sessions", type=int, default=1, help="Sessions ending --day")
    q.add_argument("--all", action="store_true", help="Also list matching features")

    m = msub.add_parser("m1", help="Model M1: move & vol distribution forecast (E27.2)")
    m1sub = m.add_subparsers(dest="m1_command", required=True)
    t = m1sub.add_parser("train", help="Walk-forward eval + keep rule + final fit (dev only)")
    t.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    t.add_argument("--features", type=Path, default=None, help="Default <data>/ml/features")
    t.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models/m1")
    t.add_argument("--tickers", default=None, help="Comma list (default: every stored ticker)")
    t.add_argument("--rounds", type=int, default=None, help="Boosting rounds (default fixed)")
    t.add_argument("--report", type=Path, default=None, help="Also write the research report")
    t.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help="Training-config overlay YAML (params, rounds, features add/drop, calibration, "
        "targets; E27.7)",
    )
    t.add_argument("--study", default=None, help="ML-<n> stamped in the manifest (E27.7)")
    r = m1sub.add_parser("report", help="Render docs/RESEARCH/ml-m1.md from a trained version")
    r.add_argument("--model", type=Path, required=True, help="data/ml/models/m1/<version>")
    r.add_argument("--out", type=Path, default=REPO / "docs" / "RESEARCH" / "ml-m1.md")
    r.add_argument("--card", type=Path, default=None, help="Also write model_card.md here")

    lg = msub.add_parser("ledger", help="Candidate outcome ledger (E7.21)")
    lsub = lg.add_subparsers(dest="ledger_command", required=True)
    lb = lsub.add_parser("build", help="Every menu candidate × day with labels (resumable)")
    lb.add_argument("--universe", default="c125", help="Checkpoint (c125/c250/c500)")
    lb.add_argument("--from", dest="start", type=_dt.date.fromisoformat, required=True)
    lb.add_argument("--to", dest="end", type=_dt.date.fromisoformat, required=True)
    lb.add_argument(
        "--data-end",
        type=_dt.date.fromisoformat,
        default=None,
        help="Last session labels may read (default: --to + 75 days)",
    )
    lb.add_argument("--profiles", default="margin,cash_debit,cash_long_only")
    lb.add_argument("--workers", type=int, default=1, help="Processes (≤ 6 on the host)")
    lb.add_argument("--sample-frac", type=float, default=None, help="Off-band keep fraction")
    lb.add_argument("--tickers", default=None, help="Comma list (overrides --universe)")
    lb.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    lb.add_argument("--out", type=Path, default=None, help="Default <data>/ml/ledger")
    lb.add_argument("--ranking", type=Path, default=REPO / "config" / "ranking.yaml")
    lb.add_argument("--no-cache", action="store_true", help="Skip the shared menu cache")
    lb.add_argument(
        "--ignore-window",
        action="store_true",
        help="Don't yield to the E26.3 runner (21:30–08:30 ET or data/backtest/queue.json)",
    )
    ls2 = lsub.add_parser("stats", help="Counts, label base rates, mean r_net per x")
    ls2.add_argument("--out", type=Path, default=None, help="Ledger root (default data/ml/ledger)")
    ls2.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ls2.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ls2.add_argument("--reason", default=None, help="Required to read holdout (logged)")
    lr = lsub.add_parser("report", help="Write docs/RESEARCH/candidate-ledger.md (dev rows)")
    lr.add_argument("--out", type=Path, default=None, help="Ledger root (default data/ml/ledger)")
    lr.add_argument("--data", type=Path, default=DEFAULT_DATA)
    lr.add_argument("--doc", type=Path, default=REPO / "docs" / "RESEARCH" / "candidate-ledger.md")

    m2 = msub.add_parser("m2", help="Model M2: calibrated candidate outcome model (E27.3)")
    m2sub = m2.add_subparsers(dest="m2_command", required=True)
    t2 = m2sub.add_parser("train", help="Walk-forward eval + replays + m2_promote + register (dev)")
    t2.add_argument("--family", default="credit,debit", help="credit, debit or both")
    t2.add_argument(
        "--overlay", type=Path, default=None, help="Training-config overlay YAML (D133)"
    )
    t2.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    t2.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")
    t2.add_argument("--tickers", default=None, help="Comma list (default: every ledger ticker)")
    t2.add_argument("--workers", type=int, default=6, help="Sim build processes (≤ 6)")
    t2.add_argument("--no-replay", action="store_true", help="Skip the decision-value replays")
    t2.add_argument("--no-register", action="store_true", help="Don't add to models.json")
    h2 = m2sub.add_parser("holdout", help="Score the 2024+ holdout once with a registered version")
    h2.add_argument("family", choices=["credit", "debit"])
    h2.add_argument("version")
    h2.add_argument("--reason", required=True, help="Why (logged in holdout_reads.jsonl)")
    h2.add_argument("--actor", default=None, help="Owner id (re-reads only)")
    h2.add_argument("--ack", default=None, help="'OWNER UPDATE: holdout re-read ok' (re-reads)")
    h2.add_argument("--data", type=Path, default=DEFAULT_DATA)
    h2.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")
    h2.add_argument("--workers", type=int, default=6)
    h2.add_argument("--no-replay", action="store_true")
    r2 = m2sub.add_parser("report", help="Render the model cards + docs/RESEARCH/ml-m2.md")
    r2.add_argument("--version", action="append", required=True, help="<family>/<version> (repeat)")
    r2.add_argument("--data", type=Path, default=DEFAULT_DATA)
    r2.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")
    r2.add_argument("--doc", type=Path, default=REPO / "docs" / "RESEARCH" / "ml-m2.md")
    r2.add_argument("--cards", type=Path, default=REPO / "docs" / "RESEARCH" / "models")
    r2.add_argument("--extra", type=Path, default=None, help="Extra markdown appended to the doc")

    dlp = msub.add_parser("dl", help="Deep-learning challengers vs M2 (E27.6, research only)")
    dlsub = dlp.add_subparsers(dest="dl_command", required=True)
    dt_ = dlsub.add_parser("train", help="Challengers × families × dev folds + replays + rule")
    dt_.add_argument("--family", default="credit,debit", help="credit, debit or both")
    dt_.add_argument(
        "--challenger", default="mlp,ft,temporal,surface", help="Comma list (stack is automatic)"
    )
    dt_.add_argument("--data", type=Path, default=DEFAULT_DATA, help="History data dir")
    dt_.add_argument("--out", type=Path, default=None, help="Default <data>/ml/dl")
    dt_.add_argument("--tickers", default=None, help="Comma list (default: every ledger ticker)")
    dt_.add_argument("--workers", type=int, default=6, help="Sim build processes (≤ 6)")
    dt_.add_argument("--no-replay", action="store_true", help="Skip the decision-value replays")
    dt_.add_argument("--no-m1", action="store_true", help="Skip the surface → M1 test")
    dr_ = dlsub.add_parser("report", help="Render docs/RESEARCH/ml-dl-challengers.md from a run")
    dr_.add_argument("run", type=Path, help="Run dir (<data>/ml/dl/dl-<hash>)")
    dr_.add_argument(
        "--doc", type=Path, default=REPO / "docs" / "RESEARCH" / "ml-dl-challengers.md"
    )
    dr_.add_argument("--extra", type=Path, default=None, help="Extra markdown appended to the doc")

    rg = msub.add_parser("registry", help="Model registry: list / verify (E27.3)")
    rgsub = rg.add_subparsers(dest="registry_command", required=True)
    rl = rgsub.add_parser("list", help="models.json per family")
    rl.add_argument("--data", type=Path, default=DEFAULT_DATA)
    rl.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")
    rv = rgsub.add_parser("verify", help="Re-hash every file of a version")
    rv.add_argument("family")
    rv.add_argument("version")
    rv.add_argument("--data", type=Path, default=DEFAULT_DATA)
    rv.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")

    pr = msub.add_parser(
        "promote", help="Owner only: shadow (or a rolling-gate-passed candidate, D140) -> champion"
    )
    pr.add_argument("family", choices=["credit", "debit"])
    pr.add_argument("version")
    pr.add_argument("--actor", required=True, help="Must be the owner's Slack id")
    pr.add_argument("--reason", default="", help="Logged with the promotion")
    pr.add_argument("--data", type=Path, default=DEFAULT_DATA)
    pr.add_argument("--out", type=Path, default=None, help="Default <data>/ml/models")

    from arc.ml.study_cli import add_ml_study_parser

    add_ml_study_parser(msub)

    mo = msub.add_parser("monitor", help="Live shadow-score coverage / calibration (E27.4)")
    mo.add_argument("--db", type=Path, default=None, help="Store (read-only; default data/arc.db)")
    mo.add_argument("--day", type=_dt.date.fromisoformat, default=None, help="Default today ET")
    mo.add_argument("--days", type=int, default=20, help="Window length in calendar days")
    mo.add_argument("--json", action="store_true", help="Print the ml_health record as JSON")
    mo.add_argument("--data", type=Path, default=DEFAULT_DATA)

    rf = msub.add_parser(
        "refresh", help="Nightly live panel / M1 / drift / parity / health (E27.4)"
    )
    rf.add_argument("--db", type=Path, required=True, help="Store (read-only)")
    rf.add_argument("--day", type=_dt.date.fromisoformat, default=None, help="Default today ET")
    rf.add_argument("--tickers", default=None, help="Comma list (default: the day's active list)")
    rf.add_argument("--data", type=Path, default=DEFAULT_DATA, help="Output data dir")
    rf.add_argument(
        "--history-data", type=Path, default=None, help="History cache (default --data)"
    )
    rf.add_argument("--no-extend-store", action="store_true", help="Don't write history panels")

    rt = msub.add_parser(
        "retrain", help="Monthly rolling-holdout retrain -> candidate only (E27.4, D140)"
    )
    rt.add_argument("--data", type=Path, default=DEFAULT_DATA)
    rt.add_argument("--families", default="credit,debit")
    rt.add_argument("--tickers", default=None, help="Comma subset (smoke runs)")
    rt.add_argument("--extend-ledger", action="store_true", help="Extend the ledger first")
    rt.add_argument("--workers", type=int, default=6)
    rt.add_argument("--no-m1", action="store_true", help="Reuse the newest M1 instead")
    rt.add_argument("--plan", action="store_true", help="Print the rolling window and exit")
    rt.add_argument("--holdout-months", type=int, default=None)
    rt.add_argument("--min-gap-days", type=int, default=None)
    rt.add_argument("--code-sha", default=None, help="Default: the repo HEAD")
    rt.add_argument("--json", action="store_true", help="Print {summary, metrics} as one JSON line")


def run_ml(args: argparse.Namespace) -> int:
    if args.ml_command == "study":
        from arc.ml.study_cli import run_ml_study

        return run_ml_study(args)
    if args.ml_command == "dl":
        return _dl(args)
    if args.ml_command == "m2":
        return _m2(args)
    if args.ml_command == "registry":
        return _registry(args)
    if args.ml_command == "promote":
        return _promote(args)
    if args.ml_command == "m1":
        return _m1(args)
    if args.ml_command == "ledger":
        return _ledger(args)
    if args.ml_command == "monitor":
        return _monitor(args)
    if args.ml_command == "refresh":
        return _refresh(args)
    if args.ml_command == "retrain":
        return _retrain(args)
    cmd = args.features_command
    if cmd == "list":
        return _list(args)
    if cmd == "build":
        return _build(args)
    return _parity(args)


def _git_sha() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip()


def _models_root(args: argparse.Namespace) -> Path:
    return Path(args.out) if args.out else Path(args.data) / "ml" / "models"


def _owner_id() -> str:
    from arc.config import ArcSettings

    return ArcSettings().owner_slack_user_id


def _m2(args: argparse.Namespace) -> int:
    from arc.utils.calendar import now_et

    root = _models_root(args)

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    if args.m2_command == "report":
        from arc.ml.m2.report import write_reports

        pairs = [tuple(v.split("/", 1)) for v in args.version]
        paths = write_reports(
            root, pairs, doc=Path(args.doc), cards=Path(args.cards), extra=args.extra
        )
        sys.stdout.write("".join(f"wrote {p}\n" for p in paths))
        return 0
    if args.m2_command == "holdout":
        from arc.ml.m2.pipeline import holdout_run
        from arc.ml.registry import HoldoutReadRefusedError

        try:
            res = holdout_run(
                args.family,
                args.version,
                data_dir=Path(args.data),
                out_root=root,
                code_sha=_git_sha(),
                now=now_et(),
                reason=args.reason,
                actor=args.actor,
                owner=_owner_id(),
                ack=args.ack,
                workers=args.workers,
                replay=not args.no_replay,
                say=progress,
            )
        except HoldoutReadRefusedError as e:
            sys.stderr.write(f"refused: {e}\n")
            return 2
        sys.stdout.write(f"{args.family}/{args.version}: holdout read #{res['reads']} logged\n")
        return 0
    from arc.ml.m2.config import FAMILIES, load_config
    from arc.ml.m2.pipeline import dev_run

    fams = [f.strip() for f in args.family.split(",") if f.strip()]
    bad = [f for f in fams if f not in FAMILIES]
    if bad:
        sys.stderr.write(f"unknown family {bad}; expected {list(FAMILIES)}\n")
        return 2
    cfg = load_config(Path(args.overlay) if args.overlay else None)
    out = dev_run(
        fams,
        cfg,
        data_dir=Path(args.data),
        out_root=root,
        code_sha=_git_sha(),
        now=now_et(),
        tickers=_split(args.tickers) if args.tickers else None,
        workers=args.workers,
        replay=not args.no_replay,
        register=not args.no_register,
        say=progress,
    )
    for fam, r in out.items():
        sys.stdout.write(
            f"{fam}: {r['vdir']} m2_promote {'PASSED' if r['rule_passed'] else 'failed'} "
            f"({r['timings']['total_s']:.0f}s, peak rss {r['timings']['peak_rss_mb']:.0f} MB)\n"
        )
    return 0


def _dl(args: argparse.Namespace) -> int:
    from arc.utils.calendar import now_et

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    if args.dl_command == "report":
        from arc.ml.dl.report import write_doc

        p = write_doc(Path(args.run), Path(args.doc), extra=args.extra)
        sys.stdout.write(f"wrote {p}\n")
        return 0
    from arc.ml.dl.config import CHALLENGERS, DLConfig
    from arc.ml.dl.run import dev_run
    from arc.ml.m2.config import FAMILIES

    fams = [f.strip() for f in args.family.split(",") if f.strip()]
    chals = [c.strip() for c in args.challenger.split(",") if c.strip()]
    bad = [f for f in fams if f not in FAMILIES] + [c for c in chals if c not in CHALLENGERS]
    if bad:
        sys.stderr.write(f"unknown family/challenger {bad}\n")
        return 2
    out = dev_run(
        fams,
        DLConfig(),
        data_dir=Path(args.data),
        out_root=Path(args.out) if args.out else None,
        code_sha=_git_sha(),
        now=now_et(),
        challengers=chals,
        tickers=_split(args.tickers) if args.tickers else None,
        workers=args.workers,
        do_replay=not args.no_replay,
        m1_surface=not args.no_m1,
        say=progress,
    )
    sys.stdout.write(f"wrote {out}\n")
    return 0


def _registry(args: argparse.Namespace) -> int:
    from arc.ml.registry import RegistryError, load_index, verify

    root = _models_root(args)
    if args.registry_command == "verify":
        try:
            files = verify(root, args.family, args.version)
        except RegistryError as e:
            sys.stderr.write(f"FAILED: {e}\n")
            return 1
        sys.stdout.write(f"{args.family}/{args.version}: {len(files)} files match their sha256\n")
        return 0
    for fam in (
        sorted(p.name for p in root.iterdir() if (p / "models.json").is_file())
        if root.is_dir()
        else []
    ):
        idx = load_index(root, fam)
        sys.stdout.write(f"{fam}: champion {idx.champion or '-'}\n")
        for ver, e in sorted(idx.versions.items(), key=lambda kv: kv[1].created):
            ok = "pass" if e.rule_passed else "fail"
            sys.stdout.write(f"  {ver}  {e.status:<9} {e.rule} {ok}  {e.created}\n")
    return 0


def _promote(args: argparse.Namespace) -> int:
    from arc.ml.registry import RegistryError, promote
    from arc.utils.calendar import now_et

    try:
        promote(
            _models_root(args),
            args.family,
            args.version,
            actor=args.actor,
            owner=_owner_id(),
            now=now_et(),
            reason=args.reason,
        )
    except RegistryError as e:
        sys.stderr.write(f"refused: {e}\n")
        return 2
    sys.stdout.write(f"{args.family}: {args.version} is now champion\n")
    return 0


def _m1(args: argparse.Namespace) -> int:
    from arc.ml.m1.report import render_card, render_report

    if args.m1_command == "report":
        vdir = Path(args.model)
        Path(args.out).write_text(render_report(vdir))
        if args.card:
            Path(args.card).write_text(render_card(vdir))
        sys.stdout.write(f"wrote {args.out}\n")
        return 0
    from arc.ml.m1.train import LGB_PARAMS, NUM_ROUNDS, model_features, train_m1

    data = Path(args.data)
    feats = Path(args.features) if args.features else data / "ml" / "features"
    out = Path(args.out) if args.out else data / "ml" / "models" / "m1"

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    kw: dict[str, object] = {}
    if args.overlay is not None:
        import yaml

        from arc.ml.features.compute import M1_HORIZONS
        from arc.ml.study import M1Overlay

        try:
            ov = M1Overlay.model_validate(yaml.safe_load(Path(args.overlay).read_text()) or {})
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"arc ml m1 train: --overlay: {exc}\n")
            return 2
        base = model_features()
        fl = [f for f in base if f not in set(ov.features.drop)]
        kw = {
            "params": {**LGB_PARAMS, **ov.params},
            "features": fl + [f for f in ov.features.add if f not in fl],
            "calibration": ov.calibration.method,
            "cal_sessions": ov.calibration.window_sessions,
            "lgbm_targets": ov.target_set(M1_HORIZONS),
            "extra": ov.extra(),
        }
        if ov.rounds and not args.rounds:
            kw["rounds"] = ov.rounds
    run = train_m1(
        data,
        feats,
        out,
        code_sha=_git_sha(),
        tickers=_split(args.tickers) if args.tickers else None,
        rounds=int(kw.pop("rounds", None) or args.rounds or NUM_ROUNDS),  # type: ignore[call-overload]
        progress=progress,
        study=args.study,
        **kw,  # type: ignore[arg-type]
    )
    (run.model_dir / "model_card.md").write_text(render_card(run.model_dir))
    if args.report:
        Path(args.report).write_text(render_report(run.model_dir))
    sys.stdout.write(
        f"{run.model_dir}: {run.rows['dataset']} rows, "
        f"{run.timings['total_s']:.0f}s; keep: "
        + ", ".join(f"{r.kind} h{r.h}={r.source}" for r in run.decisions.itertuples())
        + "\n"
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    from arc.ml.features.registry import FEATURES, schema_version

    if args.json:
        sys.stdout.write(json.dumps([f.model_dump(mode="json") for f in FEATURES], indent=2) + "\n")
        return 0
    w = sys.stdout.write
    w(f"feature schema v{schema_version()} · {len(FEATURES)} features\n")
    w(f"{'name':<26}{'group':<16}{'dtype':<10}{'win':>5}  {'live':<5} {'nulls':<18} description\n")
    for f in FEATURES:
        live = "yes" if f.available_live else "no"
        w(
            f"{f.name:<26}{f.group:<16}{f.dtype:<10}{f.window:>5}  {live:<5} "
            f"{f.null_policy:<18} {f.description}\n"
        )
    return 0


def _split(raw: str) -> list[str]:
    return list(dict.fromkeys(t.strip().upper() for t in raw.split(",") if t.strip()))


def _ledger(args: argparse.Namespace) -> int:
    from arc.ml.ledger.build import ledger_stats

    data = Path(args.data)
    out = Path(args.out) if args.out else data / "ml" / "ledger"
    if args.ledger_command == "stats":
        sys.stdout.write(ledger_stats(out, split=args.split, reason=args.reason).rstrip() + "\n")
        return 0
    if args.ledger_command == "report":
        from arc.ml.ledger.build import iter_ledger, version_dir, xtag
        from arc.ml.ledger.report import ReportAccumulator, report_columns

        vdir = out if (out / "manifest.json").exists() else version_dir(out)
        manifest = json.loads((vdir / "manifest.json").read_text())
        xs = [xtag(float(x)) for x in manifest.get("xs", [0.0, 0.25, 0.5])]
        acc = ReportAccumulator(xs)
        for profile, year, part in iter_ledger(vdir, columns=report_columns(xs)):
            acc.add(part)
            sys.stderr.write(f"report: {profile} {year} {len(part):,} rows\n")
        Path(args.doc).write_text(acc.render(manifest))
        sys.stdout.write(f"wrote {args.doc}\n")
        return 0
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.ml.features.adapters.history import universe_symbols
    from arc.ml.ledger.build import build_ledger
    from arc.routines.config import RangeEdgesSettings
    from arc.routines.strategy import load_strategy_data

    tickers = _split(args.tickers) if args.tickers else universe_symbols(data, args.universe)[0]
    data_end = args.data_end or args.end + _dt.timedelta(days=75)
    edges = RangeEdgesSettings.model_validate(load_strategy_data(None).get("range_edges") or {})

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    from arc.utils.calendar import now_et

    queue = data / "backtest" / "queue.json"

    def stop() -> bool:
        """The card's compute window: yield to the E26.3 runner (21:30–08:30 ET or its
        queue file), unless --ignore-window."""
        if args.ignore_window:
            return False
        t = now_et().time()
        night = t >= _dt.time(21, 30) or t < _dt.time(8, 30)
        return night or queue.exists()

    res = build_ledger(
        data_dir=data,
        out_root=out,
        universe=args.universe,
        start=args.start,
        end=args.end,
        data_end=data_end,
        profiles=[p.strip() for p in args.profiles.split(",") if p.strip()],
        cfg=load_ranking_file(args.ranking),
        cost=load_cost_model(),
        exits=load_exit_config(),
        settings=ArcSettings(),
        tickers=tickers,
        workers=args.workers,
        sample_frac=args.sample_frac,
        cache_dir=None if args.no_cache else data,
        range_rule=edges.rule() if edges.enabled else None,
        progress=progress,
        stop=stop,
    )
    sys.stdout.write(
        f"{res.vdir}: {res.jobs} (ticker, years) jobs, {res.rows} rows total, "
        f"{res.manifest['bytes'] / 1e9:.2f} GB, {res.seconds:.0f}s\n"
    )
    return 0


def _build(args: argparse.Namespace) -> int:
    from arc.ml.features.adapters.history import load_membership, universe_symbols
    from arc.ml.features.store import build_store, version_dir

    data = Path(args.data)
    syms, etfs = universe_symbols(data, args.universe)
    tickers = _split(args.tickers) if args.tickers else syms
    out = Path(args.out) if args.out else data / "ml" / "features"
    t0 = time.monotonic()

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    res = build_store(
        data,
        out,
        tickers,
        args.start,
        args.end,
        etfs=etfs,
        members=load_membership(data),
        universe=args.universe,
        progress=progress,
    )
    sys.stdout.write(
        f"{version_dir(out)}: {res['dates_written']} dates written, "
        f"{res['dates_skipped']} skipped, {res['rows']} rows, "
        f"{time.monotonic() - t0:.0f}s\n"
    )
    return 0


def _parity(args: argparse.Namespace) -> int:
    import sqlite3

    import pandas as pd

    from arc.ml.features.adapters.history import history_inputs, universe_symbols
    from arc.ml.features.adapters.live import live_inputs
    from arc.ml.features.compute import compute_panel
    from arc.ml.features.parity import compare_panels, input_diffs
    from arc.ml.features.registry import panel_feature_names, registry
    from arc.utils.calendar import sessions_between

    data = Path(args.data)
    db = Path(args.db) if args.db else REPO / "data" / "arc.db"
    tickers = _split(args.tickers)
    back = sessions_between(args.day - _dt.timedelta(days=args.sessions * 2 + 10), args.day)
    days = back[-args.sessions :]
    try:
        _, etfs = universe_symbols(data)
    except (FileNotFoundError, ValueError):
        etfs = frozenset()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # read-only: never writes
    try:
        li = live_inputs(conn, tickers, days[0], days[-1], data_dir=data, etfs=etfs)
    finally:
        conn.close()
    hi = history_inputs(data, tickers, days[0], days[-1], members={}, etfs=etfs)
    live = compute_panel(li, days, tickers)
    hist = compute_panel(hi, days, tickers)
    reg = registry()
    feats = [f for f in panel_feature_names() if reg[f].available_live]
    rep = compare_panels(live, hist, feats)
    w = sys.stdout.write
    w(
        f"parity {days[0]}..{days[-1]} · {len(tickers)} tickers · live rows {len(live)} · "
        f"history rows {len(hist)} · {len(feats)} live features\n"
    )
    show = rep if args.all else rep[(~rep["ok"]) | (rep["tol"] > 1e-9)]  # noqa: PLR2004
    with pd.option_context("display.width", 200, "display.max_rows", 500):
        w(show.to_string(index=False) + "\n")
    exact = rep[rep["tol"] <= 1e-9]  # noqa: PLR2004
    w(
        f"exact features: {int(exact['ok'].sum())}/{len(exact)} within 1e-9; "
        f"over tolerance: {int((~rep['ok']).sum())}\n"
    )
    inp = input_diffs(li.ohlc, hi.ohlc, days[-1])
    if len(inp):
        w("input OHLC rows that differ between the stores (window ending --day):\n")
        with pd.option_context("display.width", 200, "display.max_rows", 500):
            w(inp.to_string(index=False) + "\n")
    else:
        w("input OHLC: identical in both stores over the window\n")
    return 0 if bool(rep["ok"].all()) else 1


def _monitor(args: argparse.Namespace) -> int:
    import json
    import sqlite3

    from arc.ml.monitor import health, render
    from arc.ml.refresh import latest_health
    from arc.pipeline.market import ETF_UNDERLYINGS
    from arc.utils.calendar import now_et

    day = args.day or now_et().date()
    db = Path(args.db) if args.db else REPO / "data" / "arc.db"
    nightly = latest_health(args.data, day) or {}
    extra = {k: nightly[k] for k in ("score_drift", "serving", "last_retrain") if k in nightly}
    if nightly:
        extra["nightly_day"] = nightly.get("day")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # read-only: never writes
    try:
        rec = health(
            conn,
            day=day,
            since=day - _dt.timedelta(days=args.days),
            drift=nightly.get("feature_drift"),
            parity=nightly.get("parity"),
            extra=extra,
            etfs=frozenset(ETF_UNDERLYINGS),
        )
    finally:
        conn.close()
    if args.json:
        sys.stdout.write(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n")
    else:
        sys.stdout.write(render(rec))
    return 0


def _refresh(args: argparse.Namespace) -> int:
    import sqlite3

    from arc.config import ArcSettings
    from arc.ml.refresh import refresh_tickers, run_refresh
    from arc.ml.serve import family_for_profile
    from arc.pipeline.market import ETF_UNDERLYINGS
    from arc.utils.calendar import ET, is_session, now_et, previous_session, session_close

    settings = ArcSettings()
    now = now_et()
    # default: the latest completed session (today only once it has closed)
    today = now.date()
    done_today = is_session(today) and now >= session_close(today)
    day = args.day or (today if done_today else previous_session(today))
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # read-only
    conn.row_factory = sqlite3.Row
    try:
        names = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers
            else refresh_tickers(conn, settings, _dt.datetime.combine(day, _dt.time(16), ET))
        )
        res = run_refresh(
            conn,
            data_dir=args.data,
            day=day,
            tickers=names,
            family=family_for_profile(settings.account_profile),
            etfs=frozenset(ETF_UNDERLYINGS),
            history_data_dir=args.history_data,
            extend_store=not args.no_extend_store,
        )
    finally:
        conn.close()
    sys.stdout.write(res.summary() + "\n")
    return 0 if res.health_path else 1


def _retrain(args: argparse.Namespace) -> int:
    from arc.ml import retrain as rt
    from arc.utils.calendar import now_et

    families = [f.strip() for f in args.families.split(",") if f.strip()]
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    ledger_root = args.data / "ml" / "ledger"
    if args.plan:
        last, end = rt.ledger_dates(ledger_root)
        gap = rt.gap_days(ledger_root, years=[end.year - 1, end.year])
        w = rt.plan_window(end, gap, last_decision=last)
        sys.stdout.write(json.dumps(w.to_json(), indent=2) + "\n")
        return 0
    say = lambda m: sys.stderr.write(m + "\n")  # noqa: E731
    if args.extend_ledger:
        from arc.ml.refresh import history_cache_end

        end = history_cache_end(args.data, now_et().date())
        if end is None:
            sys.stderr.write("no history cache: run history.topup first\n")
            return 2
        info = rt.extend_ledger(
            args.data,
            data_end=end,
            ranking_file=REPO / "config" / "ranking.yaml",
            workers=args.workers,
            tickers=tickers,
            say=say,
        )
        sys.stdout.write(json.dumps(info) + "\n")
        if not info["complete"]:
            return 3
    res = rt.run_retrain(
        data_dir=args.data,
        now=now_et(),
        code_sha=args.code_sha or _git_sha(),
        families=families,
        retrain_m1=not args.no_m1,
        tickers=tickers,
        months=args.holdout_months or rt.HOLDOUT_MONTHS,
        min_gap_days=args.min_gap_days or rt.MIN_GAP_DAYS,
        say=say,
    )
    if args.json:
        out = {"summary": res.summary(), "metrics": res.metrics()}
        sys.stdout.write(json.dumps(out, default=str) + "\n")
    else:
        sys.stdout.write(res.summary() + "\n")
    return 1 if any(f.error for f in res.families.values()) else 0
