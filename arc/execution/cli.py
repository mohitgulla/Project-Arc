"""``arc execute``: work one approved proposal through its D24 price band (E6.2).

The same path the Investor routine takes on an ``approval`` event, for a manual
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

__all__ = ["add_execute_parser", "run_execute"]

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
    from arc.config import get_settings
    from arc.execution.ladder import ExecStatus, execute
    from arc.gate.halt import HaltSwitch
    from arc.gate.token import TokenError, gate_secret
    from arc.routines.investor import load_approved
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.store.repos import HaltRepo
    from arc.utils.calendar import is_open, now_et

    settings = get_settings()
    try:
        gate_secret(settings)
    except TokenError as exc:
        _out({"status": "refused", "detail": f"ARC_GATE_SECRET not usable: {exc}"})
        return 2

    conn = connect(args.db or settings.db_path)
    migrate(conn)
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
        from arc.broker.alpaca_paper import AlpacaPaperBroker

        broker = AlpacaPaperBroker()
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
