"""``arc budget status``: the D32 daily options order budget as of now (read-only).

Prints used/limit/tier/remaining plus the local vs broker counts. ``--db`` opens
an in-memory copy of the audit DB (nothing is written); the broker cross-check
calls the paper account's order list unless ``--no-broker`` is given.

``--arm <name>`` (D69, E15.3): the budget of one experiment arm. The arm store is
looked up in the control store (``--db``) by ``<experiment id>:<arm>`` or a bare arm
name that only one experiment uses. On a shared account it prints the arm line (its
slice of the budget, counted from its tagged orders) and the account line (every
option order on the account against the account guard).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import sqlite3
    from pathlib import Path

__all__ = ["add_budget_parser", "resolve_arm_store", "run_budget"]


def add_budget_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("budget", help="Daily options order budget (E6.5, D32)")
    bsub = p.add_subparsers(dest="budget_command", required=True)
    s = bsub.add_parser("status", help="Used/limit/tier for one ET day (read-only)")
    s.add_argument("--db", default=None, help="Audit DB (default data/arc.db); opened read-only")
    s.add_argument("--day", default=None, help="ET day YYYY-MM-DD (default: today)")
    s.add_argument(
        "--no-broker", action="store_true", help="Skip the broker cross-check (local count only)"
    )
    s.add_argument(
        "--arm",
        default=None,
        help="Experiment arm (<experiment id>:<arm> or a unique arm name): its budget "
        "slice and the shared account's guard (D69)",
    )
    s.add_argument("--json", action="store_true", help="Emit the OrderBudget as JSON")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def resolve_arm_store(control: sqlite3.Connection, name: str) -> tuple[str, Path]:
    """``(key, store path)`` of arm *name* recorded in the control store.

    *name* is a full ``<experiment id>:<arm>`` key or a bare arm name used by exactly
    one experiment. Raises ``LookupError`` (with the known keys) otherwise.
    """
    from arc.experiments.arms import arm_stores

    stores = arm_stores(control)
    if name in stores:
        return name, stores[name]
    hits = [k for k in stores if k.rpartition(":")[2] == name]
    if len(hits) == 1:
        return hits[0], stores[hits[0]]
    known = ", ".join(sorted(stores)) or "none"
    why = "is ambiguous" if hits else "is not an arm of this store"
    msg = f"arm {name!r} {why} (known: {known})"
    raise LookupError(msg)


def run_budget(args: argparse.Namespace) -> int:
    from arc.budget.orders import OrderBudgetConfig, budget_state, count_orders
    from arc.control.effective import effective_settings
    from arc.pipeline.runner import open_db
    from arc.utils.calendar import ET, now_et

    if args.budget_command != "status":  # pragma: no cover - argparse enforces the choice
        return 2
    conn = open_db(args.db, copy=True)
    arm_name = getattr(args, "arm", None)
    if arm_name:
        try:
            key, path = resolve_arm_store(conn, arm_name)
        except LookupError as exc:
            _err(f"arc budget status: {exc}")
            return 2
        if not path.is_file():
            _err(f"arc budget status: arm store {path} for {key} not found")
            return 2
        conn.close()
        conn = open_db(path, copy=True)
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
    count = count_orders(conn, broker, day, account_max=cfg.account_max)
    budget = budget_state(count.used, cfg, day=day, count=count)
    if args.json:
        extra = {"broker_source": broker_note} | ({"arm": arm_name} if arm_name else {})
        _out(json.dumps(budget.model_dump(mode="json") | extra, indent=2))
        return 0
    _out(f"order budget for {day.isoformat()} (ET): {budget.summary()}")
    if arm_name:
        _out(
            f"  arm {arm_name}: used {budget.used} of {budget.limit} "
            f"(slice; restrictive at {budget.restrict_at}, opens stop at {budget.open_limit})"
        )
        if cfg.account_max is None:
            _out("  account: dedicated (the arm's own account; no shared guard)")
        else:
            seen = "n/a (broker not read)" if count.account is None else str(count.account)
            state = "; GUARD REACHED, every arm stops" if count.account_blocked else ""
            _out(f"  account: {seen} of {cfg.account_max} option orders today{state}")
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
