"""E7.18 (D84): loss & drawdown anatomy of the live-shaped backtest (report only).

Three steps, all reproducible from this file:

    # 1. the books: C500 + C125 dev on main's sim, plus each trade's daily marks
    nice -n 10 .venv/bin/python scripts/loss_anatomy.py runs --workers 6
    # 2. the point-in-time entry feature table (one row per trade, both books)
    .venv/bin/python scripts/loss_anatomy.py features
    # 3. the report: docs/RESEARCH/loss-anatomy.md + aggregate CSVs
    .venv/bin/python scripts/loss_anatomy.py report

E7.19 (D125) re-baseline on the sleeve-aligned sim: the same three steps with
``--runs-dir data/backtest/runs/baseline_d125`` (env ``ARC_ANATOMY_RUNS``) before the
subcommand; ``--intraday-proxy none`` writes the proxy-off books (``<name>_noproxy``).

Runs land in ``data/backtest/runs/anatomy/<name>/`` (dev period 2020-01-01 ->
2023-12-31, data end 2024-03-30, both profiles, incumbent rankers). The holdout
(2024-01-01 on) is read only as the settlement tail of trades opened by 2023-12-31,
as E7.14/E7.17. Nothing here writes ``data/backtest/ledger.jsonl`` or ``queue.json``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.loss_attribution import DATA  # noqa: E402

if TYPE_CHECKING:
    from arc.backtest.sim import SimRun
    from arc.backtest.sim_report import SimBuild

#: The books' root. ``ARC_ANATOMY_RUNS`` (set by ``--runs-dir``) points every step at
#: another root, e.g. the E7.19 re-baseline ``data/backtest/runs/baseline_d125``.
RUNS = Path(os.environ.get("ARC_ANATOMY_RUNS") or DATA / "backtest" / "runs" / "anatomy")
START, END, DATA_END = "2020-01-01", "2023-12-31", "2024-03-30"
#: name -> universe. ``c500_mid_gross`` is not a sim: the report re-scores c500_dev at mid.
BOOKS: dict[str, str] = {"c500_dev": "c500", "c125_dev": "c125"}
PROFILES = ("margin", "cash_debit")


def trade_key(underlying: str, entry: dt.date | str, legs: str) -> str:
    """One trade's id across trades.csv, paths.parquet and the feature table."""
    return f"{underlying}|{entry}|{legs}"


def _legs_str(trade: object) -> str:
    legs = trade.legs  # type: ignore[attr-defined]
    return " ".join(f"{'+' if lg.side > 0 else '-'}{lg.symbol}" for lg in legs)


def run_paths(b: SimBuild, run: SimRun) -> pd.DataFrame:
    """Each opened trade's per-unit daily marks (``Outcome.marks``), keyed by trade.

    The walk keeps only the trade row; the marks live on the build's candidates, so
    each row is matched back on (underlying, entry day, legs). Every trade must match.
    """
    rows: list[dict[str, object]] = []
    t = run.trades
    for r in t.itertuples():
        menu = b.built[str(r.underlying)].menus.get(run.profile, {}).get(r.entry_date) or []
        hit = [
            sc
            for sc in menu
            if sc.result is not None and _legs_str(sc.result.outcome.trade) == r.legs
        ]
        if not hit:
            msg = f"no build candidate for {r.underlying} {r.entry_date} {r.legs}"
            raise LookupError(msg)
        out = hit[0].result.outcome  # type: ignore[union-attr]
        key = trade_key(str(r.underlying), r.entry_date, str(r.legs))
        for day, v in sorted(out.marks.items()):
            rows.append({"trade": key, "day": day, "mark_unit": float(v)})
    return pd.DataFrame(rows, columns=["trade", "day", "mark_unit"])


def _run_book(name: str, workers: int, intraday_proxy: str | None = None) -> None:
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim import (
        GATE_RULES_APPLIED,
        GATE_RULES_NOT_APPLIED,
        NOT_REPLAYED,
        SKIP_REASONS,
    )
    from arc.backtest.sim_report import (
        SimManifest,
        _git_sha,
        _sha,
        _write_run,
        build_sim,
        peak_rss_mb,
        render_report,
        run_summary,
        sleeve_frame,
        walk,
    )
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config

    t0 = time.monotonic()
    universe = BOOKS[name]
    out = RUNS / (name if intraday_proxy in (None, "range") else f"{name}_noproxy")
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_ranking_file()
    if intraday_proxy is not None:
        sim0 = cfg.backtest.sim.model_copy(update={"intraday_proxy": intraday_proxy})
        cfg = cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"sim": sim0})})
    cost, exits = load_cost_model(), load_exit_config()
    start, end, data_end = (dt.date.fromisoformat(x) for x in (START, END, DATA_END))
    b = build_sim(
        data_dir=DATA,
        universe=universe,
        start=start,
        end=end,
        data_end=data_end,
        profiles=list(PROFILES),
        rankers=[],
        cfg=cfg,
        cost=cost,
        exits=exits,
        settings=ArcSettings(),
        workers=workers,
    )
    t1 = time.monotonic()
    runs = []
    for p in PROFILES:
        for r in b.rankers[p]:
            run = walk(b, profile=p, ranker=r, cfg=cfg, end=end)
            d = out / p / r.value
            _write_run(run, d)
            run_paths(b, run).to_parquet(d / "paths.parquet", index=False)
            runs.append(run)
    t_walk = time.monotonic() - t1
    sim = cfg.backtest.sim
    hits = sum(r.cache_hits for r in b.built.values())
    misses = sum(r.cache_misses for r in b.built.values())
    m = SimManifest(
        git_sha=_git_sha(),
        config_hashes={
            "ranking": _sha(cfg.model_dump(mode="json")),
            "exits": _sha(exits.model_dump(mode="json")),
            "costs": _sha(cost.model_dump(mode="json")),
            "sim": _sha(sim.model_dump(mode="json")),
        },
        universe=universe,
        universe_version=b.universe_version,
        provider=cfg.backtest.data.provider,
        fallback_provider=cfg.backtest.data.fallback_provider,
        partitions=b.partitions,
        start=start,
        end=end,
        data_end=data_end,
        profiles=list(PROFILES),
        rankers={p: [r.value for r in v] for p, v in b.rankers.items()},
        equity=sim.equity,
        tickers=len(b.names),
        workers=workers,
        cache_hits=hits,
        cache_misses=misses,
        cache_hit_rate=(hits / (hits + misses)) if hits + misses else None,
        runtime_s={
            "build": round(b.build_s, 1),
            "walk": round(t_walk, 1),
            "total": round(time.monotonic() - t0, 1),
        },
        peak_rss_mb=peak_rss_mb(),
        gate_rules_applied=list(GATE_RULES_APPLIED),
        gate_rules_not_applied=list(GATE_RULES_NOT_APPLIED),
        not_replayed=NOT_REPLAYED,
    )
    summaries = pd.DataFrame([run_summary(r, sim.equity) for r in runs])
    skips = pd.DataFrame(
        [{"profile": r.profile, "ranker": r.ranker, **r.skip_counts} for r in runs],
        columns=["profile", "ranker", *SKIP_REASONS],
    )
    sleeves = sleeve_frame(runs)
    summaries.to_csv(out / "summary.csv", index=False)
    skips.to_csv(out / "skip_counts.csv", index=False)
    sleeves.to_csv(out / "sleeves.csv", index=False)
    (out / "manifest.json").write_text(m.model_dump_json(indent=1))
    (out / "report.md").write_text(
        render_report(m, summaries, skips, sim.model_dump(mode="json"), sleeves)
    )
    sys.stdout.write(f"== {name}: done {m.runtime_s}\n{summaries.to_string()}\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E7.18 loss & drawdown anatomy")
    ap.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Books root (default data/backtest/runs/anatomy; E7.19: .../baseline_d125)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("runs", help="C500 + C125 dev books with per-trade marks")
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--only", action="append", choices=sorted(BOOKS))
    r.add_argument("--skip-done", action="store_true", help="Skip books with a manifest")
    r.add_argument(
        "--intraday-proxy",
        choices=["none", "range"],
        default=None,
        help="E7.19: override backtest.sim.intraday_proxy (none -> <name>_noproxy)",
    )
    f = sub.add_parser("features", help="Point-in-time entry feature table")
    f.add_argument("--only", action="append", choices=sorted(BOOKS))
    rep = sub.add_parser("report")
    rep.add_argument("--out", type=Path, default=ROOT / "docs" / "RESEARCH")
    args = ap.parse_args(argv)
    os.environ.setdefault("ARC_ENV", "paper")
    if args.runs_dir is not None and args.runs_dir.resolve() != RUNS.resolve():
        # every module reads RUNS at import: re-exec with the env set (no global mutation)
        import subprocess

        env = {**os.environ, "ARC_ANATOMY_RUNS": str(args.runs_dir.resolve())}
        rest = list(argv if argv is not None else sys.argv[1:])
        i = rest.index("--runs-dir")
        del rest[i : i + 2]
        return subprocess.call([sys.executable, __file__, *rest], env=env)
    if args.cmd == "runs":
        if (DATA / "backtest" / "queue.json").exists():
            sys.stderr.write("refused: data/backtest/queue.json exists (E26.3 runner owns it)\n")
            return 3
        for name in args.only or list(BOOKS):
            dest = name if args.intraday_proxy in (None, "range") else f"{name}_noproxy"
            if args.skip_done and (RUNS / dest / "manifest.json").exists():
                continue
            _run_book(name, args.workers, args.intraday_proxy)
        return 0
    if args.cmd == "features":
        from scripts.loss_anatomy_features import write_features

        for name in args.only or list(BOOKS):
            write_features(name)
        return 0
    from scripts.loss_anatomy_report import write_report

    write_report(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
