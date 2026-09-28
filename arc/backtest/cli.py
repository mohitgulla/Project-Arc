"""``arc backtest`` CLI: run the baseline + D4 report on cached EOD history."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from arc.backtest.costs import CostModel
from arc.backtest.report import DEFAULT_R, closes_for, run_report
from arc.data.history.store import ParquetHistoryStore

if TYPE_CHECKING:
    import argparse


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def add_backtest_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("backtest", help="Cost-aware baseline + D4 backtest report (offline)")
    p.add_argument("--tickers", default="SPY", help="Comma-separated underlyings")
    p.add_argument("--start", type=_date, required=True, help="First entry session")
    p.add_argument("--end", type=_date, required=True, help="Last entry session")
    p.add_argument("--provider", default="alpaca", help="History provider partition")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("data/backtest"))
    p.add_argument("--slippage", type=float, default=0.25, help="x in mid ± x·spread")
    p.add_argument("--fee", type=float, default=0.65, help="$ per contract per side")
    p.add_argument("--spread-pct", type=float, default=0.04, help="Est. spread / mid (no quote)")
    p.add_argument("--spread-min", type=float, default=0.03, help="Est. spread floor $/share")
    p.add_argument("--rate", type=float, default=DEFAULT_R, help="Flat risk-free rate")
    p.add_argument("--train-months", type=int, default=6)
    p.add_argument("--test-months", type=int, default=2)
    p.add_argument("--no-sensitivity", action="store_true")
    p.add_argument(
        "--offline", action="store_true", help="Use cached underlying closes only (no Alpaca)"
    )
    p.add_argument(
        "--exit-policy",
        choices=["hold_to_expiry", "policy"],
        default="hold_to_expiry",
        help="hold_to_expiry (default) or policy = config/exits.yaml rules (E2.4, D23)",
    )


def run_backtest_cli(args: argparse.Namespace) -> int:
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    source = None
    if not args.offline:  # pragma: no cover - network
        from arc.backtest.underlying import AlpacaBarsSource
        from arc.data.history.cli import _load_alpaca_env

        _load_alpaca_env()
        source = AlpacaBarsSource()
    closes = closes_for(tickers, args.start, args.end, args.data_dir, source)
    cost = CostModel(
        slippage_frac=args.slippage,
        commission_per_contract=args.fee,
        spread_pct=args.spread_pct,
        spread_min=args.spread_min,
    )
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
