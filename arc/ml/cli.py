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


def run_ml(args: argparse.Namespace) -> int:
    cmd = args.features_command
    if cmd == "list":
        return _list(args)
    if cmd == "build":
        return _build(args)
    return _parity(args)


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
