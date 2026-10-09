"""``arc journal``: read and review the decision journal (E7.4, D22).

- ``arc journal show <proposal-hash|prefix|chain-id>``  the full decision tree.
- ``arc journal replay <ref>``  rebuild each persona prompt from its recorded
  snapshot + inputs and check the sha256 against ``persona_calls``.
- ``arc journal gaps --since YYYY-MM-DD``  no-trade reasons, rejected
  alternatives vs the chosen structure (shadow-priced from E7.1 history),
  calibration, slippage, review root causes.
- ``arc journal review <proposal-hash> --label ... --root-cause ... --cite <id>``
  append an owner review (decision quality separate from outcome).
- ``arc journal explain <proposal-hash|run-id|chain-id> [--json]``  (E9.3) one
  document per decision: persona calls, gate, approval, orders/fills, outcome,
  reviews.
- ``arc journal counterfactual --since YYYY-MM-DD [--json]``  (E9.3) closed
  trades vs hold-to-expiry and no-trade; not-traded proposals EOD-shadowed.
- ``arc scorecard attribution --since YYYY-MM-DD --by kind,regime,persona_model``
  (E9.3) P&L attribution buckets with a ``low_sample`` flag.
- ``arc journal backfill-outcomes [--dry-run]``  (E7.4b) write the missing
  ``outcomes`` row of every closed structure; idempotent.
- ``arc journal repair-fill-signs [--dry-run]``  (E6.2g) restate fills stored with
  the wrong sign vs their signed band (fills, structure nets, tax lot, outcome,
  plus a ``reconcile:fill_sign_corrected`` decision), then the day ``pnl_snapshots``
  realised P&L of the days those lots closed; idempotent.

- ``arc funnel report --since YYYY-MM-DD --until YYYY-MM-DD``  (E13.14, D56) the
  idea funnel per stage and feed (shares ``arc.tower.data_funnel`` with the Tower).

``explain``, ``counterfactual``, ``arc scorecard attribution`` and ``arc funnel`` open
the store read-only (``mode=ro``): they never create, migrate or write it.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import TYPE_CHECKING, Any

from arc.journal.reasons import ReviewLabel, RootCause

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.config import ArcSettings

__all__ = [
    "add_funnel_parser",
    "add_journal_parser",
    "add_scorecard_parser",
    "run_funnel",
    "run_journal",
    "run_scorecard",
]

#: subcommands that open the store read-only and never migrate it (E9.3)
READ_ONLY = ("explain", "counterfactual")


def add_journal_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "journal", help="Decision journal: show, explain, replay, gaps, counterfactual, review"
    )
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

    e = jsub.add_parser(
        "explain", help="Why a decision was made: the full audit document (read-only)"
    )
    e.add_argument("ref", help="Proposal hash (or unique prefix), run-… id or chain-… id")
    e.add_argument("--json", action="store_true", help="Print the ExplainReport as JSON")
    e.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    cf = jsub.add_parser(
        "counterfactual", help="Closed trades vs hold-to-expiry / no-trade (read-only)"
    )
    cf.add_argument("--since", default=None, help="YYYY-MM-DD (ET); default: all time")
    cf.add_argument("--until", default=None, help="YYYY-MM-DD (ET, exclusive); default: now")
    cf.add_argument("--data-dir", default="data", help="E7.1 history root (options_eod)")
    cf.add_argument("--json", action="store_true", help="Print the report as JSON")
    cf.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    sc = jsub.add_parser("scorecard", help="Weekly paper scorecard (E7.3), from the audit store")
    sc.add_argument(
        "--week", default=None, help="Any date (YYYY-MM-DD, ET) in the week; default: this week"
    )
    sc.add_argument(
        "--write",
        default=None,
        metavar="DIR",
        help="Also write <DIR>/<monday>.md (e.g. docs/RESEARCH/weekly)",
    )
    sc.add_argument("--json", action="store_true", help="Print the Scorecard model as JSON")
    sc.add_argument(
        "--settle",
        action="store_true",
        help="Price D19 hold-to-expiry shadows from Alpaca daily bars (network, read-only)",
    )
    sc.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    b = jsub.add_parser(
        "backfill-outcomes",
        help="Write the missing outcome of every closed structure (idempotent, E7.4b)",
    )
    b.add_argument("--dry-run", action="store_true", help="Build the records, write nothing")
    b.add_argument("--json", action="store_true", help="Print the per-structure results as JSON")
    b.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")

    fs = jsub.add_parser(
        "repair-fill-signs",
        help="Restate fills stored with the wrong sign vs their band (idempotent, E6.2g)",
    )
    fs.add_argument("--dry-run", action="store_true", help="Print every change, write nothing")
    fs.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def add_scorecard_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """``arc scorecard attribution`` (E9.3). The weekly report stays ``arc journal scorecard``."""
    from arc.journal.views import ATTRIBUTION_DIMENSIONS

    p = sub.add_parser("scorecard", help="Scorecard views of the audit store (read-only)")
    ssub = p.add_subparsers(dest="scorecard_command", required=True)
    a = ssub.add_parser("attribution", help="Realised P&L attribution by bucket")
    a.add_argument("--since", default=None, help="YYYY-MM-DD (ET); default: all time")
    a.add_argument("--until", default=None, help="YYYY-MM-DD (ET, exclusive); default: now")
    a.add_argument(
        "--by",
        default="kind,regime,persona_model",
        help=f"Comma-separated dimensions: {', '.join(ATTRIBUTION_DIMENSIONS)}",
    )
    a.add_argument("--json", action="store_true", help="Print the report as JSON")
    a.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")


def add_funnel_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """``arc funnel report`` (E13.14, D56; folds E5.14's report half)."""
    p = sub.add_parser("funnel", help="Idea funnel: docs -> candidates -> proposals (read-only)")
    fsub = p.add_subparsers(dest="funnel_command", required=True)
    r = fsub.add_parser("report", help="Funnel counts per stage and feed for a day range")
    r.add_argument("--since", default=None, help="YYYY-MM-DD (ET, inclusive); default: --range")
    r.add_argument("--until", default=None, help="YYYY-MM-DD (ET, inclusive); default: today")
    r.add_argument(
        "--range", default="1W", choices=["1D", "1W", "1M", "3M"], help="Days when no --since"
    )
    r.add_argument("--json", action="store_true", help="Print the report as JSON")
    r.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
    r.add_argument("--config", default=None, help="routines.yaml (default: config/routines.yaml)")


def run_funnel(args: argparse.Namespace) -> int:
    from pathlib import Path

    from arc.config import get_settings
    from arc.routines.config import load_routines
    from arc.tower.data_funnel import funnel_bounds, load_funnel_report, render_funnel_table
    from arc.utils.calendar import now_et

    try:
        since = _dt.date.fromisoformat(args.since) if args.since else None
        until = _dt.date.fromisoformat(args.until) if args.until else None
        first, last = funnel_bounds(now_et().date(), args.range, since=since, until=until)
        routines = load_routines(Path(args.config)) if args.config else load_routines()
        conn = _connect_ro(args.db, get_settings())
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"arc funnel report: {exc}\n")
        return 2
    try:
        rep = load_funnel_report(conn, since=first, until=last, routines=routines)
    finally:
        conn.close()
    if args.json:
        sys.stdout.write(rep.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(render_funnel_table(rep) + "\n")
    return 0


def _day(text: str | None) -> _dt.datetime | None:
    from arc.utils.calendar import ET

    if not text:
        return None
    return _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time(), tzinfo=ET)


def _connect_ro(db: str | None, settings: ArcSettings) -> sqlite3.Connection:
    from arc.store.db import connect_ro
    from arc.store.identity import check_store_env, store_path

    path = store_path(settings, db)
    conn = connect_ro(path)
    try:
        check_store_env(conn, settings.env, path=str(path))  # D70: never the other env's store
    except Exception:
        conn.close()
        raise
    return conn


def run_scorecard(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.journal.views import attribution, attribution_lines
    from arc.utils.calendar import now_et

    try:
        since, until = _day(args.since), _day(args.until) or now_et()
        conn = _connect_ro(args.db, get_settings())
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"arc scorecard {args.scorecard_command}: {exc}\n")
        return 2
    try:
        rep = attribution(conn, since=since, until=until, by=args.by)
    except ValueError as exc:
        sys.stderr.write(f"arc scorecard attribution: {exc}\n")
        return 2
    finally:
        conn.close()
    if args.json:
        sys.stdout.write(rep.model_dump_json(indent=2) + "\n")
    else:
        _out(attribution_lines(rep))
    return 0


def _run_read_only(args: argparse.Namespace, settings: ArcSettings) -> int:
    from arc.journal.report import ShadowPricer, counterfactual
    from arc.journal.views import counterfactual_lines, explain, explain_lines
    from arc.utils.calendar import now_et

    cmd = args.journal_command
    try:
        since = _day(getattr(args, "since", None))
        until = _day(getattr(args, "until", None)) or now_et()
        conn = _connect_ro(args.db, settings)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"arc journal {cmd}: {exc}\n")
        return 2
    try:
        if cmd == "explain":
            rep: Any = explain(conn, args.ref)
            lines = explain_lines(rep)
        else:
            rep = counterfactual(conn, since=since, until=until, pricer=ShadowPricer(args.data_dir))
            lines = counterfactual_lines(rep)
    except (LookupError, ValueError) as exc:
        sys.stderr.write(f"arc journal {cmd}: {exc}\n")
        return 2
    finally:
        conn.close()
    if args.json:
        sys.stdout.write(rep.model_dump_json(indent=2) + "\n")
    else:
        _out(lines)
    return 0


def _out(lines: list[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")


def _scorecard(conn: sqlite3.Connection, args: argparse.Namespace, settings: ArcSettings) -> int:
    """``arc journal scorecard``: print (and optionally write) the weekly scorecard."""
    from arc.journal.scorecard import build_scorecard, render_markdown, week_window
    from arc.routines.scorecard import budget_limits, report_path
    from arc.utils.calendar import ET, now_et

    now = now_et()
    at = (
        _dt.datetime.combine(_dt.date.fromisoformat(args.week), _dt.time(12), tzinfo=ET)
        if args.week
        else now
    )
    settle = None
    if args.settle:
        from arc.broker.reconcile_job import settle_from_market
        from arc.data.alpaca import AlpacaMarketData

        settle = settle_from_market(AlpacaMarketData())
    start, end = week_window(at)
    from arc.control import effective_settings
    from arc.journal.scorecard import auto_approve_gate

    eff = effective_settings(conn, base=settings)  # D26: the gate switch as the sweep sees it
    sc = build_scorecard(
        conn,
        start=start,
        end=end,
        now=now,
        limits=budget_limits(settings),
        settle_price=settle,
        auto_approve=auto_approve_gate(conn, eff, now=now),
    )
    text = render_markdown(sc)
    if args.write:
        path = report_path(args.write, start.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        sys.stderr.write(f"wrote {path}\n")
    sys.stdout.write(sc.model_dump_json(indent=2) + "\n" if args.json else text)
    return 0


def _backfill(conn: sqlite3.Connection, *, dry_run: bool, as_json: bool) -> int:
    """``arc journal backfill-outcomes``: one closed outcome per closed structure (E7.4b)."""
    from collections import Counter
    from dataclasses import asdict

    from arc.journal.outcomes import backfill_outcomes

    results = backfill_outcomes(conn, dry_run=dry_run)
    if as_json:
        sys.stdout.write(json.dumps([asdict(r) for r in results], indent=2) + "\n")
        return 0
    lines = [
        f"{r.action:<11} {r.ticker:<6} {r.structure_id} {r.proposal_hash[:12]}"
        + (f" {r.status} exit {r.exit_fill} P&L {r.realised_pnl}" if r.exit_fill else "")
        + (f" {r.status}" if r.action == "exists" else "")
        + (f" ({r.detail})" if r.detail else "")
        for r in results
    ]
    counts = Counter(r.action for r in results)
    summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no closed structures"
    _out([*lines, f"{'dry run: ' if dry_run else ''}{summary}"])
    return 0


def _repair_fill_signs(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """``arc journal repair-fill-signs``: restate sign-mismatched fills (E6.2g)."""
    from decimal import Decimal

    from arc.journal.fill_signs import repair_fill_signs, restate_pnl_snapshots
    from arc.utils.calendar import now_et

    reps = repair_fill_signs(conn, now=now_et(), dry_run=dry_run)
    lines = [line for r in reps for line in r.lines()]
    delta = sum((r.delta for r in reps), start=Decimal(0))
    # a dry run rolled the structures back, so its snapshot step sees no new corrections
    snaps = restate_pnl_snapshots(conn, dry_run=dry_run)
    lines += [s.line() for s in snaps]
    verb = "would change" if dry_run else "changed"
    lines.append(
        f"{'dry run: ' if dry_run else ''}{verb} {len(reps)} structure(s), "
        f"{sum(len(r.fixes) for r in reps)} execution(s), {len(snaps)} pnl snapshot(s); "
        f"realised delta {delta:+.2f}"
    )
    _out(lines)
    return 0


def run_journal(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.journal.report import ShadowPricer, gaps, replay, show_lines
    from arc.journal.store import JournalStore, ReviewCitationError
    from arc.utils.calendar import ET, now_et

    settings = get_settings()
    if args.journal_command in READ_ONLY:
        return _run_read_only(args, settings)
    from arc.store.identity import open_store

    conn = open_store(args.db, settings=settings)
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
        if cmd == "scorecard":
            return _scorecard(conn, args, settings)
        if cmd == "backfill-outcomes":
            return _backfill(conn, dry_run=args.dry_run, as_json=args.json)
        if cmd == "repair-fill-signs":
            return _repair_fill_signs(conn, dry_run=args.dry_run)
    except (LookupError, ReviewCitationError, ValueError) as exc:
        sys.stderr.write(f"arc journal {cmd}: {exc}\n")
        return 2
    finally:
        conn.close()
    return 2
