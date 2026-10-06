"""Decision trail behind a proposal: what each persona decided, and on what.

The proposal card shows *why* a trade exists, not only *what* it is. Since E7.4
the trail is a read of the decision journal (:mod:`arc.journal`) for the chain
run that produced the proposal, so the card shows exactly what was journaled:

- **Research** — rank in the shortlist, stance, confidence, regime read, thesis
- **Quant**    — confidence and rationale for the chosen structure
- **Risk**     — rating, advisory sizing vs. the D18 cap, concerns, narrative
- **Market**   — regime + volatility frozen with the proposal (MarketContext)
- **Analytics** — E6.1a card v2 numbers (costs, liquidity, moneyness, vol stats,
  exit model), stored on that same MarketContext row; the card never recomputes them

Everything is optional: a proposal without journal rows (manual run, a DB from
before migration 009) still renders, just without the trail. Read-only; no
LLM, no I/O beyond SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from arc.journal.reasons import Choice, ReasonCode, Stage
from arc.journal.store import JournalStore

if TYPE_CHECKING:
    import sqlite3

    from arc.journal.analytics import ProposalAnalytics
    from arc.journal.models import DecisionRecord, MarketContext

__all__ = ["DecisionTrail", "load_trail"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DecisionTrail:
    chain_run_id: str | None = None
    research: dict[str, Any] | None = None  # ResearchRankedItem
    shortlist_size: int = 0
    market_regime: str = ""
    quant: dict[str, Any] | None = None  # QuantStructureOut
    risk: dict[str, Any] | None = None  # RiskAssessment
    features: dict[str, Any] | None = None  # regime/vol subset of a FeatureSnapshot
    analytics: ProposalAnalytics | None = None  # E6.1a: stored with the MarketContext


def _first(
    decisions: list[DecisionRecord], stage: Stage, ticker: str, choices: set[Choice]
) -> dict[str, Any] | None:
    for d in decisions:
        if d.stage is stage and d.subject == ticker and d.choice in choices and d.payload:
            return d.payload
    return None


def _features(mc: MarketContext | None) -> dict[str, Any] | None:
    if mc is None:
        return None
    return {
        "regime": {"current": mc.regime} if mc.regime else None,
        "vol": {"iv": mc.atm_iv, "iv_rank": mc.ivr, "hv20": mc.hv20},
    }


def load_trail(conn: sqlite3.Connection, proposal_hash: str, ticker: str) -> DecisionTrail:
    """Trail for *proposal_hash*; an empty trail when the chain can't be found."""
    journal = JournalStore(conn)
    chain = journal.chain_for_proposal(proposal_hash)
    if not chain:
        return DecisionTrail()
    try:
        decisions = journal.decisions(chain_run_id=chain)
        shortlisted = [
            d for d in decisions if d.stage is Stage.SHORTLIST and d.choice is Choice.SELECTED
        ]
        read = next((d for d in decisions if d.reason_code is ReasonCode.MARKET_READ), None)
        t = ticker.upper()
        mc = journal.market_context(proposal_hash)
        return DecisionTrail(
            chain_run_id=chain,
            research=_first(decisions, Stage.SHORTLIST, t, {Choice.SELECTED}),
            shortlist_size=len(shortlisted),
            market_regime=str(read.payload.get("market_regime", "")) if read else "",
            quant=_first(decisions, Stage.STRUCTURE, t, {Choice.SELECTED}),
            risk=_first(decisions, Stage.RISK_REVIEW, t, {Choice.ASSESSED, Choice.NO_TRADE}),
            features=_features(mc),
            analytics=mc.analytics if mc else None,
        )
    except (ValueError, TypeError) as exc:  # a malformed row must not block the card
        log.warning("approvals.trail_unreadable", proposal_hash=proposal_hash, error=str(exc))
        return DecisionTrail(chain_run_id=chain)
