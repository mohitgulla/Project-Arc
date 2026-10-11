"""``arc history`` CLI: download options history to parquet and report coverage."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.config import get_settings
from arc.data.history.alpaca import ALPACA_OPTIONS_HISTORY_START
from arc.data.history.download import (
    PLAN_DEFAULT_ROWS_PER_DAY,
    PLAN_DEFAULT_SECONDS_PER_REQUEST,
    RunLedger,
    download,
    format_plan,
    format_results,
    plan_download,
)
from arc.data.history.store import ParquetHistoryStore, format_coverage
from arc.data.history.thetadata import (
    THETA_DEFAULT_URL,
    TIERS_WITH_OI,
    ThetaTier,
    clamp_concurrency,
    tier_earliest_date,
)
from arc.utils.calendar import now_et, previous_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.data.history.base import HistoricalDataProvider
    from arc.data.history.download import DownloadPlan
    from arc.data.history.thetadata import RequestLog

log = structlog.get_logger()

PROVIDERS = ("alpaca", "thetadata")
DEFAULT_DATA_DIR = Path("data")
_HERMES_ENV = Path.home() / ".hermes" / ".env"
_TICKER_COLUMNS = ("ticker", "symbol", "underlying")


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


def _with_oi(args: argparse.Namespace) -> bool:
    """``--with-oi``/``--no-oi``; default on for tier ≥ value (D84)."""
    tier = ThetaTier(getattr(args, "theta_tier", "free"))
    flag = getattr(args, "with_oi", None)
    return tier in TIERS_WITH_OI if flag is None else bool(flag)


def _make_provider(
    name: str,
    args: argparse.Namespace,
    on_request: Callable[[RequestLog], None] | None = None,
) -> HistoricalDataProvider:
    if name == "alpaca":
        from arc.data.history.alpaca import AlpacaHistoryProvider

        _load_alpaca_env()
        return AlpacaHistoryProvider()
    from arc.data.history.thetadata import ThetaDataEodProvider

    return ThetaDataEodProvider(
        base_url=args.theta_url,
        min_interval_s=args.theta_interval,
        tier=getattr(args, "theta_tier", "free"),
        with_oi=_with_oi(args),
        chunk_days=getattr(args, "theta_chunk_days", 7),
        on_request=on_request,
    )


def read_tickers_file(path: Path) -> list[str]:
    """Tickers from *path* in file order (priority first), de-duplicated.

    Plain text: one ticker per line (blank lines and ``#`` comments skipped).
    ``.csv`` / ``.parquet`` (the E7.7 universe): the first of the columns
    ``ticker`` / ``symbol`` / ``underlying``, in row order.
    """
    suffix = path.suffix.lower()
    if suffix in (".csv", ".parquet"):
        import pandas as pd

        df = pd.read_parquet(path) if suffix == ".parquet" else pd.read_csv(path)
        cols = {c.lower(): c for c in df.columns}
        col = next((cols[c] for c in _TICKER_COLUMNS if c in cols), None)
        if col is None:
            msg = f"{path}: no ticker column (expected one of {', '.join(_TICKER_COLUMNS)})"
            raise ValueError(msg)
        raw = [str(v) for v in df[col].tolist() if isinstance(v, str) and v.strip()]
    else:
        raw = [line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        u = t.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_tickers(args: argparse.Namespace) -> list[str]:
    """``--tickers`` then ``--tickers-file`` (file order), de-duplicated, cut at ``--max-tickers``.

    With neither flag: today's active list (D51).
    """
    names: list[str] = []
    if args.tickers:
        names.extend(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if getattr(args, "tickers_file", None):
        names.extend(read_tickers_file(args.tickers_file))
    if not names:
        from arc.universe.tiers import active_tickers_ro

        names = list(active_tickers_ro(get_settings(), now_et()))  # D51: today's active list
    uniq = list(dict.fromkeys(names))
    cap = getattr(args, "max_tickers", None)
    return uniq[:cap] if cap else uniq


def add_history_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("history", help="Historical options data (download / coverage)")
    hs = p.add_subparsers(dest="history_command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--provider", choices=PROVIDERS, action="append")
        sp.add_argument("--tickers", help="Comma-separated; default = configured universe")
        sp.add_argument(
            "--tickers-file",
            type=Path,
            help="One ticker per line, or a universe .csv/.parquet; processed in file order",
        )
        sp.add_argument("--max-tickers", type=int, help="Stop after the first N names (D84: 500)")
        sp.add_argument("--start", type=_date, help="Default: provider earliest date")
        sp.add_argument("--end", type=_date, help="Default: previous session")
        sp.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        sp.add_argument("--theta-url", default=THETA_DEFAULT_URL)
        sp.add_argument("--theta-interval", type=float, default=0.0, help="Seconds between calls")
        sp.add_argument(
            "--theta-tier",
            choices=[t.value for t in ThetaTier],
            default=ThetaTier.FREE.value,
            help="ThetaData subscription: sets the earliest date, concurrency cap and OI access",
        )

    dl = hs.add_parser("download", help="Fetch uncached sessions into parquet")
    common(dl)
    dl.add_argument("--max-dte", type=int, default=60)
    dl.add_argument("--refresh", action="store_true", help="Re-fetch cached sessions")
    dl.add_argument("--chunk", type=int, default=10, help="Sessions per provider request (alpaca)")
    dl.add_argument(
        "--theta-chunk-days",
        type=int,
        default=7,
        help="Starting calendar days per ThetaData request (adapts per ticker, ≤ 28)",
    )
    dl.add_argument(
        "--with-oi",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Fetch open interest (ThetaData; default on for tier ≥ value)",
    )
    dl.add_argument(
        "--concurrency",
        type=int,
        help="Tickers in flight (ThetaData default/cap = tier limit: free 1, value 2)",
    )
    dl.add_argument("--progress-every", type=int, default=25, help="Requests per progress line")
    dl.add_argument(
        "--plan", action="store_true", help="Print a request/rows/GB/ETA estimate, no network"
    )
    dl.add_argument(
        "--plan-rows-per-day",
        type=float,
        default=PLAN_DEFAULT_ROWS_PER_DAY,
        help="--plan guess for tickers with no cached sessions",
    )
    dl.add_argument(
        "--plan-sec-per-request",
        type=float,
        default=PLAN_DEFAULT_SECONDS_PER_REQUEST,
        help="--plan seconds per request (D84 assumed 2-5 s)",
    )

    cov = hs.add_parser("coverage", help="Per-ticker/date coverage report")
    common(cov)


def _concurrency(name: str, args: argparse.Namespace) -> int:
    if name != "thetadata":
        return max(1, args.concurrency or 1)
    tier = ThetaTier(args.theta_tier)
    n = clamp_concurrency(tier, args.concurrency)
    if args.concurrency and n != args.concurrency:
        sys.stdout.write(f"concurrency {args.concurrency} capped to {n} (tier {tier})\n")
    return n


def _earliest(name: str, args: argparse.Namespace) -> dt.date:
    if name == "alpaca":
        return ALPACA_OPTIONS_HISTORY_START
    return tier_earliest_date(ThetaTier(args.theta_tier), now_et().date())


def _plan(
    name: str,
    args: argparse.Namespace,
    store: ParquetHistoryStore,
    tickers: list[str],
    end: dt.date,
) -> DownloadPlan:
    start = max(args.start or _earliest(name, args), _earliest(name, args))
    with_oi = name == "thetadata" and _with_oi(args)
    return plan_download(
        store,
        name,
        tickers,
        start,
        end,
        tier=args.theta_tier if name == "thetadata" else "-",
        with_oi=with_oi,
        concurrency=_concurrency(name, args),
        chunk_days=args.theta_chunk_days if name == "thetadata" else args.chunk,
        max_chunk_days=28 if name == "thetadata" else args.chunk,
        default_rows_per_day=args.plan_rows_per_day,
        seconds_per_request=args.plan_sec_per_request,
        refresh=args.refresh,
    )


def _run_download(
    name: str,
    args: argparse.Namespace,
    store: ParquetHistoryStore,
    tickers: list[str],
    end: dt.date,
) -> int:
    plan = _plan(name, args, store, tickers, end)
    if args.plan:
        sys.stdout.write(format_plan(plan) + "\n\n")
        return 0
    run_id = f"{now_et():%Y%m%dT%H%M%S}-{name}"
    ledger = RunLedger(
        args.data_dir / "history_runs" / f"{run_id}.jsonl",
        out=lambda line: sys.stdout.write(line + "\n"),
        progress_every=args.progress_every,
        total_requests=plan.requests or None,
    )
    with_oi = name == "thetadata" and _with_oi(args)
    ledger.event(
        "run_start",
        provider=name,
        tier=args.theta_tier if name == "thetadata" else None,
        tickers=len(tickers),
        start=plan.start,
        end=end,
        with_oi=with_oi,
        concurrency=plan.concurrency,
        planned_requests=plan.requests,
    )
    provider = _make_provider(name, args, on_request=ledger.request)
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
        concurrency=plan.concurrency,
        need_oi=with_oi,
        ledger=ledger,
    )
    for r in results:
        log.info("history.download_result", **r.model_dump(mode="json"))
    sys.stdout.write(ledger.progress_line() + "\n")
    sys.stdout.write(format_results(results) + "\n")
    sys.stdout.write(f"ledger: {ledger.path}\n\n")
    return 1 if any(r.error for r in results) else 0


def run_history(args: argparse.Namespace) -> int:
    tickers = resolve_tickers(args)
    end = args.end or previous_session(now_et().date() + dt.timedelta(days=1))
    store = ParquetHistoryStore(args.data_dir)
    rc = 0
    for name in args.provider or list(PROVIDERS):
        if args.history_command == "download":
            rc = max(rc, _run_download(name, args, store, tickers, end))
            if args.plan:
                continue
        # Coverage is reported after every download too.
        if name == "alpaca":
            default_start = ALPACA_OPTIONS_HISTORY_START
        elif args.theta_tier == ThetaTier.FREE.value:
            default_start = end - dt.timedelta(days=365)
        else:
            default_start = _earliest(name, args)
        start = args.start or default_start
        summaries, detail = store.coverage(name, tickers, start, end)
        report_dir = args.data_dir / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        detail_path = report_dir / f"{name}_by_date.csv"
        detail.to_csv(detail_path, index=False)
        sys.stdout.write(format_coverage(summaries) + "\n")
        sys.stdout.write(f"per-date detail: {detail_path}\n\n")
    return rc
