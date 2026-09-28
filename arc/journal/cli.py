"""``arc journal``: read and review the decision journal (E7.4, D22).

- ``arc journal show <proposal-hash|prefix|chain-id>``  the full decision tree.
- ``arc journal replay <ref>``  rebuild each persona prompt from its recorded
  snapshot + inputs and check the sha256 against ``persona_calls``.
- ``arc journal gaps --since YYYY-MM-DD``  no-trade reasons, rejected
  alternatives vs the chosen structure (shadow-priced from E7.1 history),
  calibration, slippage, review root causes.
- ``arc journal review <proposal-hash> --label ... --root-cause ... --cite <id>``
  append an owner review (decision quality separate from outcome).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import TYPE_CHECKING

from arc.journal.reasons import ReviewLabel, RootCause

if TYPE_CHECKING:
    import argparse

__all__ = ["add_journal_parser", "run_journal"]


def add_journal_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("journal", help="Decision journal: show, replay, gaps, review (E7.4)")
    jsub = p.add_subparsers(dest="journal_command", required=True)

    s = jsub.add_parser("show", help="Full decision tree for a proposal or chain run")
    s.add_argument("ref", help="Proposal hash (or unique prefix) or chain run id")
    s.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    r = jsub.add_parser("replay", help="Rebuild persona prompts and verify their sha256")
    r.add_argument("ref", help="Proposal hash (or unique prefix) or chain run id")
    r.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    g = jsub.add_parser("gaps", help="Missed-opportunity and calibration report")
    g.add_argument("--since", default=None, help="YYYY-MM-DD (ET); default: all time")
    g.add_argument("--data-dir", default="data", help="E7.1 history root (options_eod)")
    g.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    v = jsub.add_parser("review", help="Append an owner review of a proposal's decisions")
    v.add_argument("proposal", help="Proposal hash (or unique prefix)")
    v.add_argument("--label", required=True, choices=[x.value for x in ReviewLabel])
    v.add_argument("--root-cause", required=True, choices=[x.value for x in RootCause])
    v.add_argument(
        "--cite", action="append", default=[], help="Decision id the review relies on (repeat)"
    )
    v.add_argument("--notes", default="")
    v.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def _out(lines: list[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")


def run_journal(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.journal.report import ShadowPricer, gaps, replay, show_lines
    from arc.journal.store import JournalStore, ReviewCitationError
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.utils.calendar import ET, now_et

    settings = get_settings()
    conn = connect(args.db or settings.db_path)
    migrate(conn)
    cmd = args.journal_command
    try:
        if cmd == "show":
            _out(show_lines(conn, args.ref))
            return 0
        if cmd == "replay":
            results = replay(conn, args.ref)
            _out(
                [
                    f"{'OK ' if r.ok else 'BAD'} {r.persona:<9} {r.call_id} {r.detail}"
                    for r in results
                ]
                or ["no persona calls to replay"]
            )
            return 0 if results and all(r.ok for r in results) else 1
        if cmd == "gaps":
            since = (
                _dt.datetime.combine(_dt.date.fromisoformat(args.since), _dt.time(), tzinfo=ET)
                if args.since
                else None
            )
            _out(gaps(conn, since=since, pricer=ShadowPricer(args.data_dir)).lines())
            return 0
        if cmd == "review":
            from arc.journal.models import DecisionReview
            from arc.journal.reasons import Reviewer

            j = JournalStore(conn)
            _, hashes = j.resolve(args.proposal)
            if len(hashes) != 1:
                sys.stderr.write(f"arc journal review: {args.proposal!r} is not one proposal\n")
                return 2
            rid = j.add_review(
                DecisionReview(
                    proposal_hash=hashes[0],
                    label=ReviewLabel(args.label),
                    root_cause=RootCause(args.root_cause),
                    notes=args.notes,
                    reviewer=Reviewer.OWNER,
                    cites=args.cite,
                    at=now_et(),
                )
            )
            sys.stdout.write(json.dumps({"review_id": rid, "proposal_hash": hashes[0]}) + "\n")
            return 0
    except (LookupError, ReviewCitationError, ValueError) as exc:
        sys.stderr.write(f"arc journal {cmd}: {exc}\n")
        return 2
    finally:
        conn.close()
    return 2
