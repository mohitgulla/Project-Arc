"""``arc iv`` (E4.12, D55): IV history backfill, Option Strategist import, validation.

    arc iv backfill --tickers SPY,QQQ|watch --since 2024-03-01 [--until D] [--db P]
    arc iv import-optionstrategist [--file saved.html] [--db P]
    arc iv validate [--tickers ...|watch] [--db P]
    arc iv import-csv [--dir data/iv_history] [--db P]
    arc iv status [--db P]

Every command takes ``--db`` (default ``data/arc.db``); rehearse on a scratch copy.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import sqlite3
    from collections.abc import Callable

    from arc.config import ArcSettings


def add_iv_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "iv", help="IV history: backfill, Option Strategist import, validate (E4.12)"
    )
    isub = p.add_subparsers(dest="iv_command", required=True)

    def db(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=None, help="Audit DB (default data/arc.db)")

    b = isub.add_parser("backfill", help="Rebuild 30-DTE IV from Alpaca option daily bars")
    b.add_argument(
        "--tickers",
        required=True,
        help="Comma list, or `watch` (today's watch list + open underlyings + SPY/QQQ)",
    )
    b.add_argument("--since", type=_dt.date.fromisoformat, required=True, help="YYYY-MM-DD")
    b.add_argument(
        "--until", type=_dt.date.fromisoformat, default=None, help="YYYY-MM-DD (default: today)"
    )
    db(b)

    o = isub.add_parser(
        "import-optionstrategist",
        help="Import the Option Strategist weekly file (ad hoc, internal use only)",
    )
    o.add_argument("--file", default=None, help="A saved copy of the page (default: fetch it)")
    o.add_argument("--tickers", default="watch", help="Rows to print: comma list or `watch`")
    db(o)

    v = isub.add_parser("validate", help="Our IV series vs the latest Option Strategist file")
    v.add_argument("--tickers", default="watch", help="Comma list, `watch`, or `all`")
    v.add_argument("--no-hv", action="store_true", help="Skip the HV20 comparison (no network)")
    db(v)

    c = isub.add_parser("import-csv", help="One-time import of legacy data/iv_history/*.csv")
    c.add_argument("--dir", default=None, help="Directory (default scanner_iv_history_dir)")
    db(c)

    s = isub.add_parser("status", help="Rows per source and per ticker")
    db(s)


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    from arc.store.identity import open_store

    return open_store(Path(args.db) if args.db else None)  # D70: binds to ARC_ENV


def _tickers(
    raw: str, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> list[str]:
    from arc.universe.tiers import open_underlyings, watch_tickers

    if raw.strip().lower() == "watch":
        names = [*watch_tickers(conn, settings, now), *open_underlyings(conn), "SPY", "QQQ"]
    else:
        names = [t.strip() for t in raw.split(",") if t.strip()]
    return list(dict.fromkeys(t.upper() for t in names))


def run_iv(args: argparse.Namespace) -> int:
    from arc.control.effective import effective_settings
    from arc.utils.calendar import now_et

    now = now_et()
    conn = _open(args)
    settings = effective_settings(conn)
    cmd = args.iv_command
    if cmd == "backfill":
        return _backfill(args, conn, settings, now)
    if cmd == "import-optionstrategist":
        return _import_os(args, conn, settings, now)
    if cmd == "validate":
        return _validate(args, conn, settings, now)
    if cmd == "import-csv":
        from arc.iv.store import import_csv_dir

        directory = Path(args.dir) if args.dir else settings.scanner_iv_history_dir
        got = import_csv_dir(conn, directory, now=now)
        sys.stdout.write(
            f"imported {sum(got.values())} rows from {len(got)} file(s) in {directory}\n"
            if got
            else f"no CSV files in {directory} (nothing to import)\n"
        )
        return 0
    # status
    rows = conn.execute(
        "SELECT source, ticker, COUNT(*), MIN(day), MAX(day) FROM iv_daily "
        "GROUP BY source, ticker ORDER BY source, ticker"
    ).fetchall()
    for r in rows:
        sys.stdout.write(f"{r[0]:<17} {r[1]:<7} {r[2]:>5}  {r[3]} .. {r[4]}\n")
    if not rows:
        sys.stdout.write("iv_daily is empty\n")
    return 0


def _backfill(
    args: argparse.Namespace, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> int:  # pragma: no cover - live network (pasted on the card)
    from arc.ingest.finnhub import DbRateLimiter
    from arc.iv.alpaca_history import RATE_STATE_KEY, AlpacaOptionHistory
    from arc.iv.backfill import backfill, format_report
    from arc.utils.calendar import previous_session, sessions_between

    until = args.until or now.date()
    if until >= now.date():  # today's bars are partial until the close: stop at yesterday
        until = previous_session(now.date())
    sessions = sessions_between(args.since, until)
    if not sessions:
        sys.stderr.write("arc iv backfill: no sessions in range\n")
        return 2
    tickers = _tickers(args.tickers, conn, settings, now)
    limiter = DbRateLimiter(
        conn, calls_per_minute=settings.alpaca_data_calls_per_minute, key=RATE_STATE_KEY
    )
    history = AlpacaOptionHistory(limiter)
    rep = backfill(
        conn,
        history,
        tickers,
        sessions,
        now=now,
        r=settings.scanner_risk_free_rate,
        dividend_yields=settings.iv_dividend_yields,
        progress=_progress,
    )
    sys.stdout.write(format_report(rep) + f"\nAlpaca data requests: {history.calls}\n")
    return 0


def _progress(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _import_os(
    args: argparse.Namespace, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> int:
    from arc.iv import optionstrategist as osf
    from arc.iv.store import IvStore

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:  # pragma: no cover - network
        from arc.ingest.options_data import http_get

        text = osf.fetch(lambda u: http_get(u, "Mozilla/5.0 (Project Arc; internal research)"))
    rows = osf.parse(text)
    if not rows:
        sys.stderr.write("arc iv import-optionstrategist: no rows parsed (layout changed?)\n")
        return 1
    n = IvStore(conn).upsert([r.to_iv_row() for r in rows], now=now)
    days = sorted({r.day for r in rows})
    sys.stdout.write(
        f"stored {n} optionstrategist rows (dates {days[0]} .. {days[-1]}; internal use only)\n"
    )
    want = set(_tickers(args.tickers, conn, settings, now))
    sys.stdout.write(
        f"{'ticker':<7} {'date':<10} {'cur_iv':>6} {'days':>5} {'pct':>4} {'hv20':>5}\n"
    )
    matched = 0
    for r in sorted(rows, key=lambda x: x.ticker):
        if r.ticker in want:
            matched += 1
            sys.stdout.write(
                f"{r.ticker:<7} {r.day} {r.cur_iv * 100:6.2f} {r.days:5d} "
                f"{r.percentile * 100:4.0f} {r.hv20 * 100:5.1f}\n"
            )
    sys.stdout.write(f"{matched}/{len(want)} watch names in the file\n")
    return 0


def _validate(
    args: argparse.Namespace, conn: sqlite3.Connection, settings: ArcSettings, now: _dt.datetime
) -> int:
    from arc.iv.validate import format_validation, validate

    tickers = (
        None
        if args.tickers.strip().lower() == "all"
        else _tickers(args.tickers, conn, settings, now)
    )
    closes: Callable[[str, _dt.date], list[float]] | None = None
    if not args.no_hv:  # pragma: no cover - network
        from arc.data.alpaca import AlpacaMarketData

        market = AlpacaMarketData()

        def _closes(t: str, day: _dt.date) -> list[float]:
            bars = market.history_bars(t, day - _dt.timedelta(days=60), day)
            return [b.close for b in bars]

        closes = _closes

    v = validate(conn, tickers=tickers, closes=closes)
    sys.stdout.write(format_validation(v) + "\n")
    return 0 if v.day is not None else 1
