"""``arc backtest`` CLI: run the baseline + D4 report on cached EOD history."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.costs import load_cost_model
from arc.backtest.report import DEFAULT_R, closes_for, label_closes_for, run_report
from arc.data.history.store import ParquetHistoryStore

if TYPE_CHECKING:
    import argparse

    from arc.models import StructureKind


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def add_backtest_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("backtest", help="Cost-aware baseline + D4 backtest report (offline)")
    p.add_argument("--tickers", default="SPY", help="Comma-separated underlyings")
    p.add_argument("--start", type=_date, default=None, help="First entry session (required)")
    p.add_argument("--end", type=_date, default=None, help="Last entry session (required)")
    p.add_argument("--provider", default="alpaca", help="History provider partition")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("data/backtest"))
    p.add_argument(
        "--slippage", type=float, default=None, help="x in mid ± x·spread (default: costs.yaml)"
    )
    p.add_argument(
        "--fee",
        type=float,
        default=None,
        help="Commission $ per contract per side (default: config/costs.yaml)",
    )
    p.add_argument("--spread-pct", type=float, default=None, help="Est. spread / mid (no quote)")
    p.add_argument("--spread-min", type=float, default=None, help="Est. spread floor $/share")
    p.add_argument("--rate", type=float, default=DEFAULT_R, help="Flat risk-free rate")
    p.add_argument("--train-months", type=int, default=6)
    p.add_argument("--test-months", type=int, default=2)
    p.add_argument("--no-sensitivity", action="store_true")
    p.add_argument(
        "--offline", action="store_true", help="Use cached underlying closes only (no Alpaca)"
    )
    p.add_argument(
        "--exit-policy",
        choices=["hold_to_expiry", "policy", "d19_rules"],
        default="hold_to_expiry",
        help="hold_to_expiry (default) or policy / d19_rules (same thing) = config/exits.yaml "
        "rules (E2.4, D19, D23)",
    )
    p.add_argument(
        "--regime-model",
        choices=["v1", "v2"],
        default="v1",
        help="E17.3: trend/vol labels for the by-regime tables (default v1)",
    )
    p.add_argument(
        "--label-history-days",
        type=int,
        default=0,
        help="E17.3: label regimes on raw closes from this many days before --start "
        "(0 = the trading closes)",
    )
    p.add_argument(
        "--label-adjusted",
        action="store_true",
        help="E17.3: with --label-history-days, label on split-adjusted closes",
    )
    bs = p.add_subparsers(dest="backtest_command", required=False)
    rk = bs.add_parser(
        "rank",
        help="E7.5 ranking backtest: every ranker per account profile (config/ranking.yaml)",
    )
    rk.add_argument(
        "--profile",
        action="append",
        choices=["margin", "cash_debit", "cash_long_only"],
        help="Account profile (repeatable; default: margin and cash_debit)",
    )
    rk.add_argument(
        "--rankers",
        default=None,
        help="Comma-separated rankers (default: config/ranking.yaml); the incumbent always runs",
    )
    rk.add_argument("--from", dest="start", type=_date, required=True, help="First entry session")
    rk.add_argument("--to", dest="end", type=_date, required=True, help="Last entry session")
    rk.add_argument("--tickers", default=None, help="Comma-separated (default: ranking.yaml)")
    rk.add_argument("--provider", default="alpaca", help="History provider partition")
    rk.add_argument("--data-dir", type=Path, default=Path("data"))
    rk.add_argument("--out", type=Path, default=Path("data/backtest/rank"))
    rk.add_argument("--config", type=Path, default=None, help="Ranking config file")
    rk.add_argument(
        "--experiment",
        type=Path,
        action="append",
        default=[],
        help="E7.5a: partial ranking file deep-merged over --config (repeatable), "
        "e.g. config/experiments/e75a_regime_menu.yaml",
    )
    rk.add_argument(
        "--slippage-from-scorecard",
        type=Path,
        default=None,
        metavar="DB",
        help="E7.5a: use the realised entry slippage per structure kind measured by the "
        "E7.3 scorecard in this audit DB (opened read-only) for the base run",
    )
    rk.add_argument(
        "--scorecard-days",
        type=int,
        default=90,
        help="Scorecard window for --slippage-from-scorecard, days back from now",
    )
    rk.add_argument(
        "--scorecard-min-fills",
        type=int,
        default=5,
        help="Fills a structure kind needs before its measured slippage is used",
    )
    rk.add_argument(
        "--entry-filter",
        choices=["none", "anti_chase", "anti_chase_vwap"],
        default=None,
        help="E16.3: override backtest.entry_filter (default: config/ranking.yaml, none). "
        "A filter needs split-adjusted daily OHLC (cached in <data-dir>/underlying_ohlc/, "
        "fetched from Alpaca unless --offline)",
    )
    rk.add_argument("--workers", type=int, default=1, help="Parallel ticker processes")
    rk.add_argument(
        "--regime-model",
        choices=["v1", "v2"],
        default=None,
        help="E17.3: override backtest.regime_model (labels for the stance menu, the "
        "sub-periods and trade rows; default: config/ranking.yaml, v1)",
    )
    rk.add_argument(
        "--label-history-days",
        type=int,
        default=0,
        help="E17.3: label regimes on raw closes from this many calendar days before "
        "--from (v2 needs ~470 for a full vol-percentile window, like the live step); "
        "0 = label on the trading closes (from - 60d)",
    )
    rk.add_argument(
        "--label-adjusted",
        action="store_true",
        help="E17.3: with --label-history-days, label on split-adjusted closes "
        "(<data-dir>/underlying_ohlc/) so a split is not a crash day",
    )
    rk.add_argument("--no-charts", action="store_true")
    rk.add_argument(
        "--offline", action="store_true", help="Use cached underlying closes only (no Alpaca)"
    )
    cmp_ = bs.add_parser(
        "rank-compare",
        help="E7.5a: experiment vs baseline `backtest rank` output, same ranker, D25 rule",
    )
    cmp_.add_argument("--baseline", type=Path, required=True, help="Baseline --out dir")
    cmp_.add_argument(
        "--experiment", type=Path, action="append", required=True, help="Experiment --out dir"
    )
    cmp_.add_argument("--config", type=Path, default=None, help="Ranking config (rule, seed)")
    cmp_.add_argument("--out", type=Path, default=None, help="Write compare.csv + compare.md here")


def measured_slippage(db: Path, *, days: int, min_fills: int) -> dict[StructureKind, float]:
    """Realised entry slippage x per structure kind from the E7.3 scorecard (read-only)."""
    import sqlite3

    from arc.backtest.ranking import slippage_from_scorecard
    from arc.journal.scorecard import slippage_since
    from arc.utils.calendar import now_et

    if not db.exists():
        msg = f"audit store not found: {db}"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        now = now_et()
        by_kind = slippage_since(conn, start=now - dt.timedelta(days=days), end=now)
    finally:
        conn.close()
    return slippage_from_scorecard(by_kind, min_fills=min_fills)


def run_rank_cli(args: argparse.Namespace) -> int:
    from arc.backtest.rank_report import run_rank_report
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings
    from arc.scanner.rank import Ranker

    cfg = load_ranking_file(args.config, args.experiment)
    if args.slippage_from_scorecard is not None:
        measured = measured_slippage(
            args.slippage_from_scorecard,
            days=args.scorecard_days,
            min_fills=args.scorecard_min_fills,
        )
        sys.stdout.write(
            "measured slippage x: "
            + (", ".join(f"{k} {v:g}" for k, v in measured.items()) or "none (too few fills)")
            + "\n"
        )
        bt = cfg.backtest
        merged = {**bt.slippage_by_kind, **measured}
        cfg = cfg.model_copy(
            update={"backtest": bt.model_copy(update={"slippage_by_kind": merged})}
        )
    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else list(cfg.backtest.tickers)
    )
    rankers = (
        [Ranker(r.strip()) for r in args.rankers.split(",") if r.strip()]
        if args.rankers
        else list(cfg.ranking.rankers)
    )
    source = None
    if not args.offline:  # pragma: no cover - network
        from arc.backtest.underlying import AlpacaBarsSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        source = AlpacaBarsSource()
    if args.entry_filter is not None:
        bt = cfg.backtest
        cfg = cfg.model_copy(
            update={"backtest": bt.model_copy(update={"entry_filter": args.entry_filter})}
        )
    if getattr(args, "regime_model", None) is not None:
        bt = cfg.backtest
        cfg = cfg.model_copy(
            update={"backtest": bt.model_copy(update={"regime_model": args.regime_model})}
        )
    closes = closes_for(tickers, args.start, args.end, args.data_dir, source)
    label_closes = label_closes_for(
        tickers,
        args.start,
        args.end,
        args.data_dir,
        source,
        getattr(args, "label_history_days", 0),
        adjusted=getattr(args, "label_adjusted", False),
        offline=args.offline,
    )
    ohlc = None
    if cfg.backtest.entry_filter != "none":
        ohlc = ohlc_for(tickers, args.start, args.end, args.data_dir, offline=args.offline)
    run_rank_report(
        store=ParquetHistoryStore(args.data_dir),
        closes_by_ticker=closes,
        tickers=tickers,
        start=args.start,
        end=args.end,
        profiles=args.profile or ["margin", "cash_debit"],
        rankers=rankers,
        out_dir=args.out,
        cfg=cfg,
        cost=load_cost_model(),
        settings=ArcSettings(),
        provider=args.provider,
        workers=args.workers,
        charts=not args.no_charts,
        ohlc_by_ticker=ohlc,
        label_closes_by_ticker=label_closes,
    )
    sys.stdout.write(f"report: {args.out / 'report.md'}\n")
    return 0


def ohlc_for(
    tickers: list[str], start: dt.date, end: dt.date, data_dir: Path, *, offline: bool
) -> dict[str, object]:
    """E16.3: split-adjusted daily OHLC from ``start - 60d`` (indicator warm-up) to *end*."""
    from arc.backtest.entry_filter import WARMUP_DAYS, OhlcStore, load_ohlc

    source = None
    if not offline:  # pragma: no cover - network
        from arc.backtest.entry_filter import AlpacaOhlcSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        source = AlpacaOhlcSource()
    store = OhlcStore(data_dir)
    lo = start - dt.timedelta(days=WARMUP_DAYS * 2)
    return {t: load_ohlc(store, t, lo, end, source=source) for t in tickers}


def run_rank_compare_cli(args: argparse.Namespace) -> int:
    import pandas as pd

    from arc.backtest.experiments import compare_dirs
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.report import format_table

    cfg = load_ranking_file(args.config)
    df = pd.concat(
        [compare_dirs(args.baseline, e, cfg, name=e.name) for e in args.experiment],
        ignore_index=True,
    )
    md = format_table(df) if len(df) else "_no common (profile, ranker) runs_\n"
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.out / "compare.csv", index=False)
        (args.out / "compare.md").write_text(md)
    sys.stdout.write(md)
    return 0


def run_backtest_cli(args: argparse.Namespace) -> int:
    if getattr(args, "backtest_command", None) == "rank":
        return run_rank_cli(args)
    if getattr(args, "backtest_command", None) == "rank-compare":
        return run_rank_compare_cli(args)
    if args.start is None or args.end is None:
        sys.stderr.write("arc backtest: --start and --end are required\n")
        return 2
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    source = None
    if not args.offline:  # pragma: no cover - network
        from arc.backtest.underlying import AlpacaBarsSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        source = AlpacaBarsSource()
    closes = closes_for(tickers, args.start, args.end, args.data_dir, source)
    # One cost model (D23): config/costs.yaml, with explicit CLI flags overriding it.
    overrides = {
        k: v
        for k, v in (
            ("slippage_frac", args.slippage),
            ("commission_per_contract", args.fee),
            ("spread_pct", args.spread_pct),
            ("spread_min", args.spread_min),
        )
        if v is not None
    }
    cost = load_cost_model().model_copy(update=overrides)
    run_report(
        store=ParquetHistoryStore(args.data_dir),
        closes_by_ticker=closes,
        tickers=tickers,
        start=args.start,
        end=args.end,
        out_dir=args.out,
        provider=args.provider,
        cost=cost,
        r=args.rate,
        train_months=args.train_months,
        test_months=args.test_months,
        sensitivity=not args.no_sensitivity,
        exit_policy=args.exit_policy,
        regime_model=args.regime_model,
        label_closes_by_ticker=label_closes_for(
            tickers,
            args.start,
            args.end,
            args.data_dir,
            source,
            args.label_history_days,
            adjusted=args.label_adjusted,
            offline=args.offline,
        ),
    )
    sys.stdout.write(f"report: {args.out / 'report.md'}\n")
    return 0
