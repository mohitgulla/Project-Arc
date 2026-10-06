"""``arc reconcile``: run the post-market reconciliation by hand (E6.3).

Same code path as the Broker reconcile job (:func:`arc.reconcile.engine.reconcile`)
against the Alpaca **paper** broker (read-only calls). Prints the report as
JSON. Exit 0 when clean, 1 on any mismatch (a halt is raised unless
``--no-halt``).
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
        "--no-settle", action="store_true", help="Do not fetch closes to settle expired structures"
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
        from arc.experiments.broker import trading_broker

        broker = trading_broker(conn, settings)  # E10.2: an arm store's own account
        if not args.no_settle:
            from arc.broker.reconcile_job import settle_from_market
            from arc.data.alpaca import AlpacaMarketData

            settle = settle_from_market(AlpacaMarketData())
    report = reconcile(
        conn, broker, settings=settings, now=now_et(), halt=not args.no_halt, settle_price=settle
    )
    perf = performance(conn, report.day)
    out = report.model_dump(mode="json")
    out["clean"] = report.clean
    out["summary"] = report.summary()
    out["performance"] = perf.model_dump() if perf else None
    sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
    return 0 if report.clean else 1
