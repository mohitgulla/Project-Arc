"""Close-to-reallocate scorer (E6.4 §2, D19). Pure: no I/O, no LLM, no broker.

Inputs are the current :class:`~arc.positions.evaluate.PositionReview` rows and
the new proposals the gate rejected **only** for capacity
(:func:`arc.gate.rules.capacity_rejection`: per-underlying budget, settled cash,
max open positions). For each (new, open) pair::

    edge = new.ev_per_bp − open.remaining_ev_per_bp − switching_cost_per_bp

- ``new.ev_per_bp``: the new structure's managed net EV (after entry + exit
  spread and fees, E2.4 model) per $ of buying power it needs,
- ``open.remaining_ev_per_bp``: what holding the open position is still worth
  vs closing it now (after costs), per $ of buying power it holds,
- ``switching_cost_per_bp``: the open position's close cost (half-spread
  slippage + fees vs mid) per $ of its buying power. The open's remaining EV
  already nets that cost, so it is counted twice on purpose: a swap must clear it.

A pair is suggested only when all hold:

1. ``edge >= realloc_min_edge × max(|new.ev_per_bp|, |open.remaining_ev_per_bp|)``
   and ``edge > 0`` (``ARC_REALLOC_MIN_EDGE``, default 20% relative),
2. ``new.pop >= open.remaining_pop − realloc_pop_tolerance`` (default 5pp),
3. closing the open actually frees what the new one lacks: a
   ``per_underlying_limit`` rejection needs a same-underlying close; a settled-cash
   rejection needs a close that returns cash (a long-value position),
4. churn: at most ``realloc_max_swaps_per_ticker_per_day`` per ticker (either side)
   and ``realloc_max_swaps_per_day`` in total, counting swaps already made today.

Pairs are taken greedily by edge, each open position and each new proposal at
most once. Every scored pair comes back with its outcome for the journal.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.gate.rules import CapacityRejection, RuleCode
from arc.positions.evaluate import PositionReview  # noqa: TC001 - pydantic field

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CapacityCandidate",
    "PairOutcome",
    "ReallocRules",
    "ScoredPair",
    "SwapSuggestion",
    "score_swaps",
]

_FORBID = ConfigDict(extra="forbid", frozen=True)

PairOutcome = Literal[
    "suggested",
    "edge_below_min",
    "pop_below_open",
    "frees_nothing",
    "no_open_numbers",
    "churn_ticker",
    "churn_day",
    "already_paired",
]


class ReallocRules(BaseModel):
    """The D19 thresholds (from ``ArcSettings.realloc_*``)."""

    model_config = _FORBID

    min_edge: float = Field(0.20, ge=0.0)
    pop_tolerance: float = Field(0.05, ge=0.0, le=1.0)
    max_per_day: int = Field(2, ge=0)
    max_per_ticker_per_day: int = Field(1, ge=0)


class CapacityCandidate(BaseModel):
    """A new proposal the gate failed for capacity only."""

    model_config = _FORBID

    source_ref: str = Field(..., description="Capacity-rejected proposal hash or decision id")
    ticker: str
    kind: str | None = None
    rejected_for: CapacityRejection
    violation_codes: list[str] = Field(..., min_length=1)
    net_ev: float = Field(..., description="Managed net EV, $ per unit (after all costs)")
    pop: float = Field(..., ge=0.0, le=1.0, description="Managed PoP after costs")
    buying_power: float = Field(..., gt=0.0, description="$ per unit")

    @property
    def ev_per_bp(self) -> float:
        return self.net_ev / self.buying_power


class SwapSuggestion(BaseModel):
    """Close ``close_structure_id`` so the capacity-blocked ``source_ref`` fits (D19)."""

    model_config = _FORBID

    close_structure_id: str
    close_ticker: str
    source_ref: str
    open_ticker: str
    rejected_for: CapacityRejection
    new_ev_per_bp: float
    open_remaining_ev_per_bp: float
    switching_cost_per_bp: float
    edge: float
    min_edge_required: float
    new_pop: float
    open_remaining_pop: float
    detail: str


class ScoredPair(BaseModel):
    model_config = _FORBID

    close_structure_id: str
    close_ticker: str
    source_ref: str
    open_ticker: str
    outcome: PairOutcome
    edge: float | None = None
    detail: str = ""


def _frees(c: CapacityCandidate, r: PositionReview) -> bool:
    if c.rejected_for is CapacityRejection.ORDER_BUDGET:
        return False  # D32: a close spends orders; it frees none
    codes = set(c.violation_codes)
    if RuleCode.PER_UNDERLYING.value in codes and r.ticker != c.ticker:
        return False
    return not (RuleCode.ACCOUNT_CASH.value in codes and r.current_value <= 0)


def score_swaps(
    reviews: Sequence[PositionReview],
    candidates: Sequence[CapacityCandidate],
    rules: ReallocRules,
    *,
    swaps_today: int = 0,
    ticker_swaps_today: Mapping[str, int] | None = None,
) -> tuple[list[SwapSuggestion], list[ScoredPair]]:
    """Score every (candidate, open) pair; return (suggestions, all scored pairs)."""
    per_ticker: Counter[str] = Counter(ticker_swaps_today or {})
    day_count = swaps_today
    scored: list[tuple[float, CapacityCandidate, PositionReview, float, float, float]] = []
    out: list[ScoredPair] = []

    def record(c: CapacityCandidate, r: PositionReview, o: PairOutcome, **kw: object) -> None:
        out.append(
            ScoredPair(
                close_structure_id=r.structure_id,
                close_ticker=r.ticker,
                source_ref=c.source_ref,
                open_ticker=c.ticker,
                outcome=o,
                **kw,  # type: ignore[arg-type]
            )
        )

    for c in candidates:
        for r in reviews:
            if r.exit_pending:
                continue  # already being closed
            if r.remaining_ev_per_bp is None or r.remaining_pop is None or not r.buying_power:
                record(c, r, "no_open_numbers", detail="no remaining EV/PoP/BP for the open")
                continue
            if not _frees(c, r):
                record(
                    c,
                    r,
                    "frees_nothing",
                    detail=f"closing {r.ticker} does not free {c.rejected_for}",
                )
                continue
            switch = max(-r.close_now_net, 0.0) / r.buying_power
            edge = c.ev_per_bp - r.remaining_ev_per_bp - switch
            need = rules.min_edge * max(abs(c.ev_per_bp), abs(r.remaining_ev_per_bp))
            if edge <= 0 or edge < need:
                record(
                    c, r, "edge_below_min", edge=round(edge, 6),
                    detail=f"edge {edge:+.4f}/$BP < required {need:+.4f}",
                )  # fmt: skip
                continue
            if c.pop < r.remaining_pop - rules.pop_tolerance:
                record(
                    c, r, "pop_below_open", edge=round(edge, 6),
                    detail=f"new PoP {c.pop:.0%} < open {r.remaining_pop:.0%} − "
                    f"{rules.pop_tolerance:.0%}",
                )  # fmt: skip
                continue
            scored.append((edge, c, r, switch, need, r.remaining_ev_per_bp))

    suggestions: list[SwapSuggestion] = []
    used_open: set[str] = set()
    used_new: set[str] = set()
    for edge, c, r, switch, need, rem in sorted(
        scored, key=lambda t: (-t[0], t[1].source_ref, t[2].structure_id)
    ):
        if r.structure_id in used_open or c.source_ref in used_new:
            record(c, r, "already_paired", edge=round(edge, 6))
            continue
        if day_count >= rules.max_per_day:
            record(c, r, "churn_day", edge=round(edge, 6), detail=f"{day_count} swaps today")
            continue
        tickers = {c.ticker, r.ticker}
        if any(per_ticker[t] >= rules.max_per_ticker_per_day for t in tickers):
            record(c, r, "churn_ticker", edge=round(edge, 6), detail="ticker swapped today")
            continue
        detail = (
            f"swap {r.ticker} -> {c.ticker}: new {c.ev_per_bp:+.4f}/$BP vs open "
            f"{rem:+.4f}/$BP, switching {switch:.4f}/$BP, edge {edge:+.4f} "
            f"(>= {need:.4f}); PoP {c.pop:.0%} vs {r.remaining_pop:.0%}"
        )
        suggestions.append(
            SwapSuggestion(
                close_structure_id=r.structure_id,
                close_ticker=r.ticker,
                source_ref=c.source_ref,
                open_ticker=c.ticker,
                rejected_for=c.rejected_for,
                new_ev_per_bp=round(c.ev_per_bp, 6),
                open_remaining_ev_per_bp=round(rem, 6),
                switching_cost_per_bp=round(switch, 6),
                edge=round(edge, 6),
                min_edge_required=round(need, 6),
                new_pop=c.pop,
                open_remaining_pop=float(r.remaining_pop or 0.0),
                detail=detail,
            )
        )
        record(c, r, "suggested", edge=round(edge, 6), detail=detail)
        used_open.add(r.structure_id)
        used_new.add(c.source_ref)
        day_count += 1
        for t in tickers:
            per_ticker[t] += 1
    return suggestions, out
