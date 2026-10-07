"""``arc slack render``: re-render a chain's persona cards from the store, never posting.

E13.13 (D56): a read-only acceptance/debug view. The cards are rebuilt from the
stored typed payloads of one ``chain_run_id`` (``scout_read``, ``shortlist`` +
``exit_watchlist``, ``exit_case``, ``risk_exit_review``) plus every ``note`` the
chain wrote (via :func:`arc.slack.blocks.render_note`). The store is opened
``mode=ro``; nothing is written and no Slack client is constructed.

Numbers that only lived in the producing run's memory (Research's drop funnel,
Scalp evidence lines, Quant exit skip counts) are not stored, so the re-render
omits them; everything shown comes from the store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from arc.context.kinds import (
    ExitCasePayload,
    ExitWatchlistPayload,
    NotePayload,
    RiskExitReviewPayload,
    ScoutReadPayload,
    ShortlistPayload,
)
from arc.slack import blocks as B
from arc.slack.digests import quant_exit_card, research_card, risk_exit_card, scout_card

if TYPE_CHECKING:
    import argparse
    import sqlite3

__all__ = ["add_slack_parser", "render_chain", "run_slack"]


def add_slack_parser(sub: Any) -> None:
    p = sub.add_parser("slack", help="Slack card tools (read-only)")
    ssub = p.add_subparsers(dest="slack_command", required=True)
    r = ssub.add_parser(
        "render", help="Render a chain's persona cards from the store (no post, read-only)"
    )
    r.add_argument("--db", default=None, help="Store path (opened read-only)")
    r.add_argument("--chain", required=True, help="chain_run_id (or a run_id of a lone run)")
    r.add_argument("--out", default=None, help="Write the JSON here instead of stdout")


def _rows(conn: sqlite3.Connection, chain: str, kind: str) -> list[sqlite3.Row]:
    """Latest entry per subject of *kind* in *chain* (by ``chain_run_id`` or ``run_id``)."""
    rows = conn.execute(
        """SELECT * FROM context_entries
           WHERE kind = ? AND (chain_run_id = ? OR (chain_run_id IS NULL AND run_id = ?))
           ORDER BY valid_from, id""",
        (kind, chain, chain),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        latest[r["subject"]] = r
    return list(latest.values())


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(row["payload"])
    return out


def _card(view: B.CardView) -> dict[str, Any]:
    return {"text": view.text, "blocks": view.blocks}


def render_chain(conn: sqlite3.Connection, chain: str) -> dict[str, Any]:
    """``{"chain": .., "cards": {name: {text, blocks}}, "notes": [..], "skipped": [..]}``."""
    cards: dict[str, Any] = {}
    skipped: list[str] = []
    run_of: dict[str, str | None] = {}

    for r in _rows(conn, chain, "scout_read"):
        try:
            read = ScoutReadPayload.model_validate(_payload(r))
        except ValidationError:
            skipped.append(f"scout_read {r['id']}")
            continue
        n = len(read.discovery)
        cards["scout"] = _card(
            scout_card(
                read=read,
                max_discovery=max(n, 20),
                min_discovery_alert=0,
                candidates=len(read.ticker_calls),
                run_id=r["run_id"],
                chain_run_id=r["chain_run_id"],
            )
        )

    shortlist = _rows(conn, chain, "shortlist")
    if shortlist:
        r = shortlist[-1]
        try:
            sl = ShortlistPayload.model_validate(_payload(r))
        except ValidationError:
            skipped.append(f"shortlist {r['id']}")
        else:
            watch = _rows(conn, chain, "exit_watchlist")
            exits = (
                ExitWatchlistPayload.model_validate(_payload(watch[-1])).items if watch else None
            )
            # candidates active when Research ran (the card's "ranked / candidates")
            cands = conn.execute(
                "SELECT count(DISTINCT subject) FROM context_entries WHERE kind = 'candidate' "
                "AND valid_from <= ? AND (expires_at IS NULL OR expires_at > ?)",
                (r["valid_from"], r["valid_from"]),
            ).fetchone()[0]
            cards["research"] = _card(
                research_card(
                    sl,
                    candidates=max(int(cands or 0), len(sl.shortlist)),
                    budget=sl.budget,
                    exits=exits,
                    run_id=r["run_id"],
                    chain_run_id=r["chain_run_id"],
                )
            )
            run_of["research"] = r["run_id"]

    cases = []
    for r in _rows(conn, chain, "exit_case"):
        try:
            cases.append(ExitCasePayload.model_validate(_payload(r)))
        except ValidationError:
            skipped.append(f"exit_case {r['id']}")
    if cases:
        cards["quant.exit"] = _card(quant_exit_card(cases, shadow=True, chain_run_id=chain))
    reviews = _rows(conn, chain, "risk_exit_review")
    if reviews and cases:
        rv = RiskExitReviewPayload.model_validate(_payload(reviews[-1]))
        cards["risk.exit"] = _card(
            risk_exit_card(
                cases,
                {v.structure_id: v for v in rv.verdicts},
                unavailable=rv.unavailable,
                chain_run_id=chain,
            )
        )

    notes: list[dict[str, Any]] = []
    for r in conn.execute(
        """SELECT * FROM context_entries
           WHERE kind = 'note' AND (chain_run_id = ? OR (chain_run_id IS NULL AND run_id = ?))
           ORDER BY valid_from, id""",
        (chain, chain),
    ).fetchall():
        try:
            note = NotePayload.model_validate(_payload(r))
        except ValidationError:
            skipped.append(f"note {r['id']}")
            continue
        notes.append(
            {
                "id": r["id"],
                "subject": r["subject"],
                "payload": note.model_dump(mode="json"),
                "blocks": B.render_note(note),
            }
        )
    return {"chain": chain, "cards": cards, "notes": notes, "skipped": skipped}


def run_slack(args: argparse.Namespace) -> int:
    from arc.store.db import connect_ro

    try:
        conn = connect_ro(args.db)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    try:
        out = render_chain(conn, args.chain)
    finally:
        conn.close()
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n")
        sys.stderr.write(
            f"wrote {args.out}: {len(out['cards'])} card(s), {len(out['notes'])} note(s)\n"
        )
    else:
        sys.stdout.write(text + "\n")
    return 0
