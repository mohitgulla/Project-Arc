"""``arc approve``: proposal cards and Approve / Reject decisions (E6.1).

- ``arc approve sweep``  post cards for new proposals, expire overdue ones.
- ``arc approve decide`` apply one click (called by the ``arc-approvals``
  Hermes plugin with the clicker's Slack user id from the platform event).
- ``arc approve reason`` journal an owner's optional reject reason (the Slack
  modal a Reject click opens; D22). The rejection itself is already recorded.
- ``arc approve list``   show approval requests (optionally for one day).

``arc routines tick`` and ``arc propose`` also run the sweep, so cards appear
right after a proposal and the TTL is enforced every tick.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.approvals.service import ApprovalService, CardPoster
    from arc.config import ArcSettings

__all__ = ["add_approve_parser", "make_service", "run_approve"]


def add_approve_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("approve", help="Proposal cards + Approve/Reject decisions (E6.1)")
    asub = p.add_subparsers(dest="approve_command", required=True)

    sw = asub.add_parser("sweep", help="Post cards for new proposals; expire overdue ones")
    sw.add_argument("--day", default=None, help="Only proposals for YYYY-MM-DD")
    sw.add_argument(
        "--no-slack", action="store_true", help="Only expire overdue requests; post nothing"
    )
    sw.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    d = asub.add_parser("decide", help="Apply an Approve/Reject click")
    d.add_argument("--proposal", required=True, help="Proposal hash (the button value)")
    d.add_argument("--user", required=True, help="Clicker's Slack user id (from the platform)")
    verdict = d.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true")
    verdict.add_argument("--reject", action="store_true")
    d.add_argument("--slack-ts", default="", help="ts of the clicked message")
    d.add_argument("--no-slack", action="store_true", help="Do not update the card in Slack")
    d.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    r = asub.add_parser("reason", help="Record the optional reason for a Reject (D22)")
    r.add_argument("proposal", help="Proposal hash (the modal's private_metadata)")
    r.add_argument("--user", required=True, help="Submitter's Slack user id (from the platform)")
    r.add_argument("--text", default="", help="Reason text; blank is fine")
    r.add_argument("--no-slack", action="store_true", help="Do not update the card in Slack")
    r.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    ls = asub.add_parser("list", help="Show approval requests")
    ls.add_argument("--day", default=None, help="YYYY-MM-DD")
    ls.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    from arc.approvals.auto import add_auto_parser

    add_auto_parser(asub)  # D34: `arc approve auto on|off|status [--env] [--confirm-live]`


def make_service(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    slack: bool,
    poster: CardPoster | None = None,
) -> ApprovalService:
    """An :class:`ApprovalService` posting to Slack (``slack=True``) or the log."""
    from arc.approvals.service import ApprovalService, LogCardPoster

    if poster is None:
        if slack:
            from arc.approvals.slack import SlackCardPoster

            poster = SlackCardPoster(conn)
        else:
            poster = LogCardPoster()
    return ApprovalService(conn, settings, poster, live=slack)


def _write(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def run_approve(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.utils.calendar import now_et

    settings = get_settings()
    conn = connect(args.db or settings.db_path)
    migrate(conn)
    cmd = args.approve_command
    if cmd == "auto":
        from arc.approvals.auto import run_auto

        return run_auto(args, base=settings, conn=conn)
    from arc.control import effective_settings

    settings = effective_settings(conn, base=settings)  # D26 overrides (approver list, TTL)

    if cmd == "list":
        svc = make_service(conn, settings, slack=False)
        _write(svc.requests(day=args.day))
        return 0

    svc = make_service(conn, settings, slack=not args.no_slack)
    if cmd == "sweep":
        if args.no_slack:  # never publish to the log from the real DB: that strands the card
            from arc.approvals.service import SweepReport

            _write(SweepReport([], [], svc.expire_due(now_et())).as_json())
            return 0
        _write(svc.sweep(now_et(), day=args.day).as_json())
        return 0

    if cmd == "reason":
        rr = svc.record_reason(args.proposal, user=args.user, text=args.text, now=now_et())
        _write(
            {
                "outcome": rr.outcome,
                "proposal_hash": rr.proposal_hash,
                "message": rr.message,
                "decision_id": rr.decision_id,
            }
        )
        return 0 if rr.accepted else 1

    res = svc.decide(
        args.proposal, user=args.user, approve=args.approve, now=now_et(), slack_ts=args.slack_ts
    )
    _write(
        {
            "outcome": str(res.outcome),
            "proposal_hash": res.proposal_hash,
            "status": str(res.status) if res.status else None,
            "message": res.message,
        }
    )
    return 0 if res.accepted else 1
