"""``arc reconcile``: run the post-market reconciliation by hand (E6.3).

Same code path as the Broker reconcile job (:func:`arc.reconcile.engine.reconcile`)
against the Alpaca **paper** broker (read-only calls). Prints the report as
JSON. Exit 0 when clean, 1 on any mismatch (a halt is raised unless
``--no-halt``). ``--intraday`` (E11.1, D71) checks only unconfirmed executions'
orders (adopting broker ids by client id) and never halts.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

    from arc.broker.base import BrokerAdapter

__all__ = ["add_reconcile_parser", "run_reconcile"]


def add_reconcile_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "reconcile", help="Reconcile the paper broker against the local store (post-market)"
    )
    p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    p.add_argument(
        "--no-halt", action="store_true", help="Report mismatches without raising a halt"
    )
    p.add_argument(
        "--intraday",
        action="store_true",
        help="Only unconfirmed executions' orders (D71); never halts, no snapshots",
    )
    p.add_argument(
        "--no-settle", action="store_true", help="Do not fetch closes to settle expired structures"
    )
    p.add_argument(
        "--now",
        default=None,
        help="Reconcile as of this tz-aware ISO time (default: now), e.g. 2026-10-05T16:30-04:00",
    )


def run_reconcile(args: argparse.Namespace, *, broker: BrokerAdapter | None = None) -> int:
    from arc.config import get_settings
    from arc.reconcile.engine import reconcile
    from arc.reconcile.performance import performance
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.utils.calendar import now_et

    settings = get_settings()
    conn = connect(args.db or settings.db_path)
    migrate(conn)
    from arc.control import effective_settings

    settings = effective_settings(conn, base=settings)  # D26 overrides
    settle = None
    if broker is None:
        from arc.broker.registry import BrokerNotAvailable
        from arc.experiments.broker import trading_broker

        try:
            broker = trading_broker(conn, settings)  # E10.2: an arm store's own account
        except BrokerNotAvailable as exc:  # E13.11: refused before any credential read
            refusal = {"status": "refused", "detail": str(exc), "broker": exc.spec.label}
            sys.stdout.write(json.dumps(refusal) + "\n")
            return 2
        if not args.no_settle and not getattr(args, "intraday", False):
            from arc.broker.reconcile_job import settle_from_market
            from arc.data.alpaca import AlpacaMarketData

            settle = settle_from_market(AlpacaMarketData())
    now = now_et()
    if getattr(args, "now", None):
        import datetime as dt

        from arc.utils.calendar import ET

        at = dt.datetime.fromisoformat(args.now)
        if at.tzinfo is None:
            sys.stderr.write("--now must carry a UTC offset\n")
            return 2
        now = at.astimezone(ET)
    if getattr(args, "intraday", False):
        report = reconcile(conn, broker, settings=settings, now=now, scope="intraday")
        out = report.model_dump(mode="json")
        out["clean"] = report.clean
        out["summary"] = report.summary()
        sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
        return 0 if report.clean else 1
    report = reconcile(
        conn, broker, settings=settings, now=now, halt=not args.no_halt, settle_price=settle
    )
    perf = performance(conn, report.day)
    out = report.model_dump(mode="json")
    out["clean"] = report.clean
    out["summary"] = report.summary()
    out["performance"] = perf.model_dump() if perf else None
    sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
    return 0 if report.clean else 1
