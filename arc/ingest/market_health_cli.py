"""``arc market-health`` (E16.4, D76): the put/call backfill and a read-only show.

    arc market-health backfill-pc [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--pace S] [--db P]
    arc market-health show [--json] [--db P]

``backfill-pc`` walks Cboe's dated daily statistics (``<day>_daily_options``, one
request per session, ``--pace`` seconds apart) for every session in the range not
yet in ``pc_history`` / ``options_daily`` and stores them in ``pc_history``. It saves
every 20 sessions, so an interrupted run resumes. Default range: the last 380
calendar days up to the last completed session (a 1-year percentile needs 252 + 5).
``show`` prints the newest ``market_health`` entry and its rendered line. Every
command takes ``--db`` (default ``data/arc.db``); rehearse on a scratch copy.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

DEFAULT_BACKFILL_DAYS = 380


def add_market_health_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("market-health", help="Market-health read: put/call backfill + show (E16.4)")
    msub = p.add_subparsers(dest="mh_command", required=True)
    b = msub.add_parser("backfill-pc", help="Backfill the Cboe put/call history (resumable)")
    b.add_argument("--since", type=_dt.date.fromisoformat, default=None)
    b.add_argument("--until", type=_dt.date.fromisoformat, default=None)
    b.add_argument("--pace", type=float, default=0.5, help="Seconds between requests")
    b.add_argument("--db", default=None, help="Audit DB (default data/arc.db)")
    s = msub.add_parser("show", help="Newest market_health entry + its Research line")
    s.add_argument("--json", action="store_true")
    s.add_argument("--db", default=None, help="Audit DB (default data/arc.db)")


def run_market_health(args: argparse.Namespace) -> int:
    from arc.utils.calendar import now_et

    if args.mh_command == "show":
        return _show(args)
    return _backfill(args, now_et())  # pragma: no cover - live network (pasted on the card)


def _show(args: argparse.Namespace) -> int:
    import sqlite3

    from arc.features.market_health import market_health_line
    from arc.ingest.market_health import latest_payload
    from arc.store.db import DEFAULT_DB_PATH

    path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not path.is_file():
        sys.stderr.write(f"store not found: {path}\n")
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        payload = latest_payload(conn, "market_health", "market")
    finally:
        conn.close()
    if payload is None:
        sys.stdout.write("no market_health entry yet\n")
        return 0
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(market_health_line(payload) + "\n")
    if payload.get("labels"):
        sys.stdout.write(f"labels: {', '.join(payload['labels'])}\n")
    for m in payload.get("missing") or []:
        sys.stdout.write(f"missing: {m}\n")
    return 0


def _backfill(
    args: argparse.Namespace, now: _dt.datetime
) -> int:  # pragma: no cover - live network (pasted on the card)
    from arc.ingest.cboe_daily import fetch_daily_options
    from arc.ingest.market_health import backfill_pc_history
    from arc.store.identity import open_store
    from arc.utils.calendar import completed_session

    until = args.until or completed_session(now)
    since = args.since or (until - _dt.timedelta(days=DEFAULT_BACKFILL_DAYS))
    conn = open_store(Path(args.db) if args.db else None)  # D70: binds to ARC_ENV
    res = backfill_pc_history(
        conn,
        start=since,
        end=until,
        now=now,
        fetch=lambda d: fetch_daily_options(d, now=now),
        pace_s=args.pace,
    )
    conn.commit()
    sys.stdout.write(f"{since} .. {until}: {res.summary()}\n")
    for day, why in sorted(res.skipped.items()):
        sys.stdout.write(f"  skipped {day}: {why}\n")
    return 0
