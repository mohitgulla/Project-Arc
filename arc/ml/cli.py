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
    ls2 = lsub.add_parser("stats", help="Counts, label base rates, mean r_net per x")
    ls2.add_argument("--out", type=Path, default=None, help="Ledger root (default data/ml/ledger)")
    ls2.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ls2.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ls2.add_argument("--reason", default=None, help="Required to read holdout (logged)")
    lr = lsub.add_parser("report", help="Write docs/RESEARCH/candidate-ledger.md (dev rows)")
    lr.add_argument("--out", type=Path, default=None, help="Ledger root (default data/ml/ledger)")
    lr.add_argument("--data", type=Path, default=DEFAULT_DATA)
    lr.add_argument("--doc", type=Path, default=REPO / "docs" / "RESEARCH" / "candidate-ledger.md")


def run_ml(args: argparse.Namespace) -> int:
    if args.ml_command == "m1":
        return _m1(args)
    if args.ml_command == "ledger":
        return _ledger(args)
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


def _m1(args: argparse.Namespace) -> int:
    from arc.ml.m1.report import render_card, render_report

    if args.m1_command == "report":
        vdir = Path(args.model)
        Path(args.out).write_text(render_report(vdir))
        if args.card:
            Path(args.card).write_text(render_card(vdir))
        sys.stdout.write(f"wrote {args.out}\n")
        return 0
    from arc.ml.m1.train import NUM_ROUNDS, train_m1

    data = Path(args.data)
    feats = Path(args.features) if args.features else data / "ml" / "features"
    out = Path(args.out) if args.out else data / "ml" / "models" / "m1"

    def progress(s: str) -> None:
        sys.stderr.write(s + "\n")

    run = train_m1(
        data,
        feats,
        out,
        code_sha=_git_sha(),
        tickers=_split(args.tickers) if args.tickers else None,
        rounds=args.rounds or NUM_ROUNDS,
        progress=progress,
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
        from arc.ml.ledger.build import load_ledger, version_dir
        from arc.ml.ledger.report import render_report

        vdir = out if (out / "manifest.json").exists() else version_dir(out)
        manifest = json.loads((vdir / "manifest.json").read_text())
        Path(args.doc).write_text(render_report(load_ledger(vdir), manifest))
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
