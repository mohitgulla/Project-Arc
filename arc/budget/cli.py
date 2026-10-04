"""``arc budget status``: the D32 daily options order budget as of now (read-only).

Prints used/limit/tier/remaining plus the local vs broker counts. ``--db`` opens
an in-memory copy of the audit DB (nothing is written); the broker cross-check
calls the paper account's order list unless ``--no-broker`` is given.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

__all__ = ["add_budget_parser", "run_budget"]


def add_budget_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("budget", help="Daily options order budget (E6.5, D32)")
    bsub = p.add_subparsers(dest="budget_command", required=True)
    s = bsub.add_parser("status", help="Used/limit/tier for one ET day (read-only)")
    s.add_argument("--db", default=None, help="Audit DB (default data/arc.db); opened read-only")
    s.add_argument("--day", default=None, help="ET day YYYY-MM-DD (default: today)")
    s.add_argument(
        "--no-broker", action="store_true", help="Skip the broker cross-check (local count only)"
    )
    s.add_argument("--json", action="store_true", help="Emit the OrderBudget as JSON")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def run_budget(args: argparse.Namespace) -> int:
    from arc.budget.orders import OrderBudgetConfig, budget_state, count_orders
    from arc.control.effective import effective_settings
    from arc.pipeline.runner import open_db
    from arc.utils.calendar import ET, now_et

    if args.budget_command != "status":  # pragma: no cover - argparse enforces the choice
        return 2
    conn = open_db(args.db, copy=True)
    settings = effective_settings(conn)  # D26: the same overrides the gate sees
    cfg = OrderBudgetConfig.from_settings(settings)
    day = dt.date.fromisoformat(args.day) if args.day else now_et().astimezone(ET).date()
    broker = None
    broker_note = "skipped (--no-broker)"
    if not args.no_broker:
        try:
            from arc.experiments.broker import trading_broker

            broker = trading_broker(conn, settings)  # E10.2: the store's own account
            broker_note = "alpaca paper"
        except Exception as exc:  # noqa: BLE001 - report, count locally
            broker_note = f"unavailable ({type(exc).__name__}: {exc})"
    count = count_orders(conn, broker, day)
    budget = budget_state(count.used, cfg, day=day, count=count)
    if args.json:
        _out(json.dumps(budget.model_dump(mode="json") | {"broker_source": broker_note}, indent=2))
        return 0
    _out(f"order budget for {day.isoformat()} (ET): {budget.summary()}")
    _out(
        f"  used {budget.used} = max(local {count.local}, broker "
        f"{'n/a' if count.broker is None else count.broker}) + reserved {count.reserved}"
    )
    _out(f"  broker cross-check: {broker_note}" + ("; MISMATCH" if count.mismatch else ""))
    _out(
        f"  limit {budget.limit}, restrictive at {budget.restrict_at}, "
        f"opens stop at {budget.open_limit} (close reserve {budget.close_reserve})"
    )
    _out(f"  remaining: opens {budget.remaining_opens}, total {budget.remaining_total}")
    return 0
