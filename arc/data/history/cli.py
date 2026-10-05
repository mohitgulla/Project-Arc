"""``arc history`` CLI: download options history to parquet and report coverage."""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.config import get_settings
from arc.data.history.alpaca import ALPACA_OPTIONS_HISTORY_START
from arc.data.history.download import download
from arc.data.history.store import ParquetHistoryStore, format_coverage
from arc.data.history.thetadata import THETA_DEFAULT_URL
from arc.utils.calendar import now_et, previous_session

if TYPE_CHECKING:
    import argparse

    from arc.data.history.base import HistoricalDataProvider

log = structlog.get_logger()

PROVIDERS = ("alpaca", "thetadata")
DEFAULT_DATA_DIR = Path("data")
_HERMES_ENV = Path.home() / ".hermes" / ".env"


def _load_alpaca_env() -> None:
    """Populate ALPACA_API_KEY/SECRET from ~/.hermes/.env if not already set (paper keys)."""
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        return
    if not _HERMES_ENV.is_file():
        return
    from dotenv import dotenv_values

    for k, v in dotenv_values(_HERMES_ENV).items():
        if k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") and v and not os.environ.get(k):
            os.environ[k] = v


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _make_provider(name: str, args: argparse.Namespace) -> HistoricalDataProvider:
    if name == "alpaca":
        from arc.data.history.alpaca import AlpacaHistoryProvider

        _load_alpaca_env()
        return AlpacaHistoryProvider()
    from arc.data.history.thetadata import ThetaDataEodProvider

    return ThetaDataEodProvider(base_url=args.theta_url, min_interval_s=args.theta_interval)


def add_history_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("history", help="Historical options data (download / coverage)")
    hs = p.add_subparsers(dest="history_command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--provider", choices=PROVIDERS, action="append")
        sp.add_argument("--tickers", help="Comma-separated; default = configured universe")
        sp.add_argument("--start", type=_date, help="Default: provider earliest date")
        sp.add_argument("--end", type=_date, help="Default: previous session")
        sp.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        sp.add_argument("--theta-url", default=THETA_DEFAULT_URL)
        sp.add_argument("--theta-interval", type=float, default=0.0, help="Seconds between calls")

    dl = hs.add_parser("download", help="Fetch uncached sessions into parquet")
    common(dl)
    dl.add_argument("--max-dte", type=int, default=60)
    dl.add_argument("--refresh", action="store_true", help="Re-fetch cached sessions")
    dl.add_argument("--chunk", type=int, default=10, help="Sessions per provider request")

    cov = hs.add_parser("coverage", help="Per-ticker/date coverage report")
    common(cov)


def run_history(args: argparse.Namespace) -> int:
    from arc.universe.tiers import active_tickers_ro

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else active_tickers_ro(get_settings(), now_et())  # D51: today's active list
    )
    end = args.end or previous_session(now_et().date() + dt.timedelta(days=1))
    store = ParquetHistoryStore(args.data_dir)
    rc = 0
    for name in args.provider or list(PROVIDERS):
        if args.history_command == "download":
            provider = _make_provider(name, args)
            start = args.start or provider.earliest_date()
            results = download(
                provider,
                store,
                tickers,
                start,
                end,
                max_dte=args.max_dte,
                refresh=args.refresh,
                max_sessions_per_request=args.chunk,
            )
            for r in results:
                log.info("history.download_result", **r.model_dump())
                if r.error:
                    rc = 1
        # Coverage is reported after every download too.
        default_start = (
            ALPACA_OPTIONS_HISTORY_START if name == "alpaca" else end - dt.timedelta(days=365)
        )
        start = args.start or default_start
        summaries, detail = store.coverage(name, tickers, start, end)
        report_dir = args.data_dir / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        detail_path = report_dir / f"{name}_by_date.csv"
        detail.to_csv(detail_path, index=False)
        sys.stdout.write(format_coverage(summaries) + "\n")
        sys.stdout.write(f"per-date detail: {detail_path}\n\n")
    return rc
