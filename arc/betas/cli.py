"""``arc betas`` (E3.6, D62): daily beta vs SPY.

    arc betas refresh [--tickers A,B,...] [--day YYYY-MM-DD] [--db P]
    arc betas show [--tickers A,B,...] [--db P]

``refresh`` defaults to today's active list + open underlyings (+ SPY/QQQ/IWM).
``show`` prints each ticker's latest row and the beta the caps use today. Every
command takes ``--db`` (default ``data/arc.db``); rehearse on a scratch copy.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import sqlite3


def add_betas_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("betas", help="Daily beta vs SPY for the beta-weighted delta cap (E3.6)")
    bsub = p.add_subparsers(dest="betas_command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=None, help="Audit DB (default data/arc.db)")
        sp.add_argument(
            "--tickers",
            default=None,
            help="Comma list (default: active list + open underlyings + SPY/QQQ/IWM)",
        )

    r = bsub.add_parser("refresh", help="Fetch 1y of daily closes and store today's betas")
    r.add_argument(
        "--day", type=_dt.date.fromisoformat, default=None, help="Row day (default: today, ET)"
    )
    common(r)
    s = bsub.add_parser("show", help="Latest stored beta per ticker and the beta used today")
    common(s)


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    from arc.store.identity import open_store

    return open_store(Path(args.db) if args.db else None)  # D70: binds to ARC_ENV


def _tickers(raw: str | None, conn: sqlite3.Connection, now: _dt.datetime) -> list[str]:
    from arc.control.effective import effective_settings
    from arc.universe.tiers import active_tickers, open_underlyings

    if raw:
        return list(dict.fromkeys(t.strip().upper() for t in raw.split(",") if t.strip()))
    settings = effective_settings(conn)
    return list(dict.fromkeys([*active_tickers(conn, settings, now), *open_underlyings(conn)]))


def run_betas(args: argparse.Namespace) -> int:
    from arc.utils.calendar import now_et

    now = now_et()
    conn = _open(args)
    if args.betas_command == "refresh":
        return _refresh(args, conn, now)
    return _show(args, conn, now)


def _refresh(
    args: argparse.Namespace, conn: sqlite3.Connection, now: _dt.datetime
) -> int:  # pragma: no cover - live network (pasted on the card)
    from arc.betas.refresh import refresh_betas
    from arc.control.effective import effective_settings
    from arc.data.alpaca import AlpacaMarketData
    from arc.ingest.finnhub import DbRateLimiter
    from arc.iv.alpaca_history import RATE_STATE_KEY

    settings = effective_settings(conn)
    limiter = DbRateLimiter(
        conn, calls_per_minute=settings.alpaca_data_calls_per_minute, key=RATE_STATE_KEY
    )
    day = args.day or now.date()
    res = refresh_betas(
        conn,
        AlpacaMarketData(),
        _tickers(args.tickers, conn, now),
        day,
        now=now,
        take=limiter.acquire,
    )
    for r in sorted(res.rows, key=lambda x: (x.beta is None, -(x.beta or 0), x.ticker)):
        beta = "   n/a" if r.beta is None else f"{r.beta:6.2f}"
        sys.stdout.write(f"{r.ticker:<7} {beta}  n={r.n_days:<3d} as_of {r.as_of}\n")
    for t, err in sorted(res.errors.items()):
        sys.stdout.write(f"{t:<7} ERROR {err}\n")
    sys.stdout.write(
        f"{len(res.rows)} rows for {day} · {len(res.errors)} errors · "
        f"{res.calls} Alpaca data requests\n"
    )
    return 0 if res.rows else 1


def _show(args: argparse.Namespace, conn: sqlite3.Connection, now: _dt.datetime) -> int:
    from arc.betas.store import betas_used, latest_rows

    rows = latest_rows(conn)
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        rows = [r for r in rows if r["ticker"] in want]
    used = betas_used(conn, [str(r["ticker"]) for r in rows], now.date())
    sys.stdout.write(f"{'ticker':<7} {'beta':>6} {'used':>5} {'n':>4}  day        source\n")
    for r in rows:
        u = used[str(r["ticker"])]
        beta = "   n/a" if r["beta"] is None else f"{float(r['beta']):6.2f}"
        sys.stdout.write(
            f"{r['ticker']:<7} {beta} {u.beta:5.2f} {int(r['n_days']):4d}  {r['day']} {u.source}\n"
        )
    if not rows:
        sys.stdout.write("betas is empty (run `arc betas refresh`)\n")
    return 0
