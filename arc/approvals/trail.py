"""Decision trail behind a proposal: what each persona decided, and on what.

The proposal card shows *why* a trade exists, not only *what* it is. The trail
is read back from the append-only context store (D16) for the chain run that
produced the proposal, so the card shows exactly what the personas wrote:

- **Director** — rank in the shortlist, stance, confidence, regime read, thesis
- **Quant**    — confidence and rationale for the chosen structure
- **Risk**     — rating, advisory sizing vs. the D18 cap, concerns, narrative
- **Market**   — regime + volatility snapshot the Director saw

Everything is optional: a proposal whose chain entries are missing (manual run,
pruned DB) still renders, just without the trail. Read-only; no LLM, no I/O
beyond SQLite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import sqlite3

__all__ = ["DecisionTrail", "load_trail"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DecisionTrail:
    chain_run_id: str | None = None
    director: dict[str, Any] | None = None  # DirectorRankedItem
    shortlist_size: int = 0
    market_regime: str = ""
    quant: dict[str, Any] | None = None  # QuantStructureOut
    risk: dict[str, Any] | None = None  # RiskAssessment
    features: dict[str, Any] | None = None  # FeatureSnapshot


def _latest(
    conn: sqlite3.Connection, chain: str, kind: str, subject: str | None = None
) -> dict[str, Any] | None:
    sql = "SELECT payload FROM context_entries WHERE chain_run_id = ? AND kind = ?"
    params: tuple[str, ...] = (chain, kind)
    if subject is not None:
        sql += " AND subject = ?"
        params += (subject,)
    row = conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", params).fetchone()
    return json.loads(row[0]) if row else None


def _pick(items: list[dict[str, Any]], ticker: str, structure_type: str | None) -> dict | None:
    mine = [i for i in items if str(i.get("ticker", "")).upper() == ticker.upper()]
    if structure_type:
        exact = [i for i in mine if i.get("structure_type") == structure_type]
        if exact:
            return exact[0]
    return mine[0] if mine else None


def load_trail(conn: sqlite3.Connection, proposal_hash: str, ticker: str) -> DecisionTrail:
    """Trail for *proposal_hash*; an empty trail when the chain can't be found."""
    row = conn.execute(
        """SELECT r.chain_run_id FROM proposals p
           JOIN routine_runs r ON r.run_id = p.run_id
           WHERE p.proposal_hash = ?""",
        (proposal_hash,),
    ).fetchone()
    chain = row[0] if row else None
    if not chain:
        return DecisionTrail()
    try:
        shortlist = _latest(conn, chain, "shortlist") or {}
        structures = _latest(conn, chain, "structures") or {}
        review = _latest(conn, chain, "risk_review") or {}
        features = _latest(conn, chain, "regime", ticker)
        items = list(shortlist.get("shortlist", []))
        quant = _pick(list(structures.get("structures", [])), ticker, None)
        stype = quant.get("structure_type") if quant else None
        return DecisionTrail(
            chain_run_id=chain,
            director=_pick(items, ticker, None),
            shortlist_size=len(items),
            market_regime=str(shortlist.get("market_regime", "")),
            quant=quant,
            risk=_pick(list(review.get("assessments", [])), ticker, stype),
            features=features,
        )
    except (ValueError, TypeError) as exc:  # a malformed entry must not block the card
        log.warning("approvals.trail_unreadable", proposal_hash=proposal_hash, error=str(exc))
        return DecisionTrail(chain_run_id=chain)
