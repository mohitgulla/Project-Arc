"""``arc backtest`` CLI: run the baseline + D4 report on cached EOD history."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.costs import load_cost_model
from arc.backtest.report import DEFAULT_R, closes_for, run_report
from arc.data.history.store import ParquetHistoryStore

if TYPE_CHECKING:
    import argparse


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
    rk.add_argument("--workers", type=int, default=1, help="Parallel ticker processes")
    rk.add_argument("--no-charts", action="store_true")
    rk.add_argument(
        "--offline", action="store_true", help="Use cached underlying closes only (no Alpaca)"
    )


def run_rank_cli(args: argparse.Namespace) -> int:
    from arc.backtest.rank_report import run_rank_report
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings
    from arc.scanner.rank import Ranker

    cfg = load_ranking_file(args.config)
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
    closes = closes_for(tickers, args.start, args.end, args.data_dir, source)
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
    )
    sys.stdout.write(f"report: {args.out / 'report.md'}\n")
    return 0


def run_backtest_cli(args: argparse.Namespace) -> int:
    if getattr(args, "backtest_command", None) == "rank":
        return run_rank_cli(args)
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
    )
    sys.stdout.write(f"report: {args.out / 'report.md'}\n")
    return 0
