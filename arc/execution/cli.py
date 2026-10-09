"""``arc execute``: work one approved proposal through its D24 price band (E6.2).

The same path the Broker job takes on an ``approval`` event, for a manual
run: load the approved proposal and its gate decision, then
:func:`arc.execution.ladder.execute` (every attempt via ``submit()``).

Fails closed, non-zero exit:

- ``ARC_GATE_SECRET`` missing/short → exit 2 (no tokenless "success", Sentinel S-4);
- ``--token`` differs from the token the gate stored for this proposal → exit 2;
- no approval request / gate decision, or the market is closed → exit 1;
- an outcome other than ``filled`` → exit 1 (``already_executed`` → 0).

The ``arc-gate`` hook separately verifies ``--token`` before this command runs.
"""

from __future__ import annotations

import hmac
import json
import sys
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import argparse

    from arc.broker.base import BrokerAdapter

__all__ = ["add_execute_parser", "add_reattach_parser", "run_execute", "run_reattach"]

log = structlog.get_logger(__name__)


def add_execute_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "execute", help="Work an approved proposal through its D24 price band (paper only)"
    )
    p.add_argument("--proposal", required=True, help="Proposal hash (full or unique prefix)")
    p.add_argument("--token", required=True, help="The gate token stored for this proposal")
    p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def _out(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def _resolve(conn: object, prefix: str) -> str | None:
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT proposal_hash FROM approval_requests WHERE proposal_hash LIKE ?", (prefix + "%",)
    ).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else None


def run_execute(args: argparse.Namespace, *, broker: BrokerAdapter | None = None) -> int:
    from arc.approvals.service import approval_record
    from arc.broker.ladder_job import load_approved
    from arc.config import get_settings
    from arc.execution.ladder import ExecStatus, execute
    from arc.gate.halt import HaltSwitch
    from arc.gate.token import TokenError, gate_secret
    from arc.store.repos import HaltRepo
    from arc.utils.calendar import is_open, now_et

    settings = get_settings()
    try:
        gate_secret(settings)
    except TokenError as exc:
        _out({"status": "refused", "detail": f"ARC_GATE_SECRET not usable: {exc}"})
        return 2

    from arc.store.identity import open_store

    conn = open_store(args.db, settings=settings)  # D70: before any broker is built
    from arc.control import effective_settings

    settings = effective_settings(conn, base=settings)  # D26 overrides (ladder, caps)
    phash = _resolve(conn, args.proposal)
    if phash is None:
        _out({"status": "refused", "detail": f"no unique approval request for {args.proposal!r}"})
        return 1
    proposal, decision, kind, ticker, sid = load_approved(conn, phash)
    if decision is None or not decision.token:
        _out({"status": "refused", "detail": "no gate token stored for this proposal"})
        return 1
    if not hmac.compare_digest(decision.token.encode(), str(args.token).encode()):
        _out({"status": "refused", "detail": "--token is not this proposal's gate token"})
        return 2
    if not is_open(now_et()):
        _out({"status": "refused", "detail": "market closed: orders are only worked in RTH"})
        return 1
    if kind == "close" and sid is None:
        _out({"status": "refused", "detail": "exit has no open structure to close"})
        return 1

    if broker is None:
        from arc.broker.registry import BrokerNotAvailable
        from arc.experiments.broker import trading_broker

        try:  # E10.2: an arm store trades only its own account
            broker = trading_broker(conn, settings)
        except BrokerNotAvailable as exc:  # E13.11: refused before any credential read
            _out({"status": "refused", "detail": str(exc)})
            return 2
    out = execute(
        proposal,
        decision,
        approval_record(conn, phash),
        conn=conn,
        broker=broker,
        config=settings,
        halt=HaltSwitch(HaltRepo(conn)),
        clock=now_et,
        sleep=time.sleep,
        kind=kind,
        structure_id=sid,
        ticker=ticker,
    )
    _out(
        {
            "proposal_hash": phash,
            "status": str(out.status),
            "band": out.band.model_dump(mode="json"),
            "attempts": [
                {
                    "step": a.step,
                    "limit": str(a.limit_price),
                    "client_order_id": a.client_order_id,
                    "broker_order_id": a.broker_order_id,
                    "status": a.status,
                    "filled_qty": a.filled_qty,
                }
                for a in out.attempts
            ],
            "filled_qty": out.filled_qty,
            "fill_price": out.fill_price,
            "structure_id": out.structure_id,
            "detail": out.detail,
        }
    )
    return 0 if out.status in (ExecStatus.FILLED, ExecStatus.ALREADY) else 1


# -- E11.2 (D72): `arc reattach` ------------------------------------------------


def add_reattach_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """``arc reattach``: adopt working executions whose ladder process died.

    A separate top-level command (not ``arc execute reattach``): ``arc execute``
    is the token-gated order path the ``arc-gate`` hook blocks without ``--token``,
    and re-attach never sends an order.
    """
    p = sub.add_parser(
        "reattach",
        help="Adopt a dead ladder's working order: record fills, cancel the rest (D72)",
    )
    p.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    p.add_argument("--lock-dir", default="data/locks", help="The dispatcher's lock dir")
    p.add_argument(
        "--dry-run", action="store_true", help="List orphans only: no broker call, no writes"
    )
    p.add_argument("--json", action="store_true", help="Print the full report as JSON")


def run_reattach(args: argparse.Namespace, *, broker: BrokerAdapter | None = None) -> int:
    """Exit 0 when nothing was adopted or every adoption ended cleanly; 1 when any
    ended ``unconfirmed`` or errored; 2 when the broker is refused."""
    import uuid

    from arc.config import get_settings
    from arc.execution.reattach import reattach
    from arc.routines.locks import LockManager, NullLocks
    from arc.store.identity import open_store
    from arc.utils.calendar import now_et

    settings = get_settings()
    conn = open_store(args.db, settings=settings)  # D70: binds the store to ARC_ENV
    from arc.control import effective_settings

    settings = effective_settings(conn, base=settings)
    if broker is None and not args.dry_run:
        from arc.broker.registry import BrokerNotAvailable
        from arc.experiments.broker import trading_broker

        try:
            broker = trading_broker(conn, settings)  # E10.2: an arm store's own account
        except BrokerNotAvailable as exc:
            _out({"status": "refused", "detail": str(exc)})
            return 2
    locks = NullLocks() if args.dry_run else LockManager(args.lock_dir)
    report = reattach(
        conn,
        broker,
        locks,
        now=now_et(),
        settings=settings,
        run_id=f"cli-reattach-{uuid.uuid4().hex[:12]}",
        clock=now_et,
        sleep=time.sleep,
        dry_run=args.dry_run,
    )
    if args.json:
        _out(report.model_dump(mode="json"))
    else:
        sys.stdout.write(report.summary() + "\n")
        for o in report.orphans:
            live = o.liveness
            sys.stdout.write(
                f"  orphan {o.proposal_hash[:12]} {o.ticker} {o.kind}: run {live.run_id} "
                f"pid {live.pid} heartbeat {live.heartbeat_age_s}s, "
                f"{len(o.orders)} open order(s)\n"
            )
        for a in report.adopted:
            sys.stdout.write(f"  {a.alert}\n")
    return 1 if report.unconfirmed or report.errors else 0
