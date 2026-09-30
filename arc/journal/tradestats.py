"""Closed-trade statistics over the scorecard's :class:`ClosedPosition` rows (E8.7c).

Pure and deterministic (no I/O, no LLM). The weekly scorecard (E7.3) decides what a
closed position and its realised P&L are; this module only aggregates them, so the
tower's Performance page and any report that calls it agree with the scorecard.

Definitions (shown in the tower's captions and in OPS.md §5.6):

- **win** = realised P&L > 0; everything else (including a scratch at 0) is a loss,
  as in :class:`~arc.journal.scorecard.PnlSummary`.
- **win rate** = wins / closed.
- **avg win / avg loss** = mean realised P&L of the wins / of the losses.
- **profit factor** = Σ wins / |Σ losses|; ``None`` with no losing dollars.
- **expectancy** = Σ realised / closed = win rate × avg win + (1 − win rate) × avg loss:
  the mean P&L per closed trade, fills only, before commissions and fees.
- **days held** = (closed_at − opened_at) in days, averaged over trades with both times.
- **hold-to-expiry hit rate** = share of trades whose D19 shadow P&L is > 0, over the
  trades whose shadow is known.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from arc.journal.scorecard import ClosedPosition

__all__ = [
    "BreakdownRow",
    "TradeRef",
    "TradeStats",
    "breakdown",
    "hold_hit_rate",
    "trade_stats",
]

_FORBID = ConfigDict(extra="forbid", frozen=True)
_DAY_S = 86_400.0


class TradeRef(BaseModel):
    """A pointer to one closed trade (its open proposal is the Trades drill-down key)."""

    model_config = _FORBID

    open_proposal_hash: str
    structure_id: str
    ticker: str
    realised_pnl: float


class TradeStats(BaseModel):
    model_config = _FORBID

    closed: int = 0
    wins: int = 0
    losses: int = 0
    realised: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = Field(default=0.0, description="Σ of the losses (≤ 0)")
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = Field(default=None, description="None: no losing dollars")
    expectancy: float | None = Field(default=None, description="Mean realised P&L per closed trade")
    avg_days_held: float | None = None
    best: TradeRef | None = None
    worst: TradeRef | None = None


def _ref(c: ClosedPosition) -> TradeRef:
    return TradeRef(
        open_proposal_hash=c.open_proposal_hash,
        structure_id=c.structure_id,
        ticker=c.ticker,
        realised_pnl=c.realised_pnl,
    )


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def trade_stats(closed: Sequence[ClosedPosition]) -> TradeStats:
    """Win/loss statistics of *closed* (see the module docstring for each definition)."""
    if not closed:
        return TradeStats()
    wins = [c.realised_pnl for c in closed if c.realised_pnl > 0]
    losses = [c.realised_pnl for c in closed if c.realised_pnl <= 0]
    gross_w, gross_l = sum(wins), sum(losses)
    held = [
        (c.closed_at - c.opened_at).total_seconds() / _DAY_S
        for c in closed
        if c.opened_at is not None
    ]
    ranked = sorted(closed, key=lambda c: (c.realised_pnl, c.closed_at, c.structure_id))
    realised = gross_w + gross_l
    return TradeStats(
        closed=len(closed),
        wins=len(wins),
        losses=len(losses),
        realised=realised,
        gross_wins=gross_w,
        gross_losses=gross_l,
        win_rate=len(wins) / len(closed),
        avg_win=_mean(wins),
        avg_loss=_mean(losses),
        profit_factor=gross_w / -gross_l if gross_l < 0 else None,
        expectancy=realised / len(closed),
        avg_days_held=_mean(held),
        best=_ref(ranked[-1]),
        worst=_ref(ranked[0]),
    )


def hold_hit_rate(closed: Iterable[ClosedPosition]) -> tuple[float | None, int]:
    """(share of known D19 shadows > 0, number of known shadows)."""
    known = [c.shadow_hold_pnl for c in closed if c.shadow_hold_pnl is not None]
    if not known:
        return None, 0
    return sum(1 for s in known if s > 0) / len(known), len(known)


class BreakdownRow(BaseModel):
    """One group of closed trades (a ticker, a structure kind, an exit reason, ...)."""

    model_config = _FORBID

    key: str = Field(description="Group value; '' when the trade has none recorded")
    count: int
    wins: int
    pnl: float = Field(description="Σ realised P&L of the group")
    win_rate: float
    share: float = Field(description="|pnl| / Σ|pnl| over all groups (the bar)")


def breakdown(items: Iterable[tuple[str | None, ClosedPosition]]) -> list[BreakdownRow]:
    """Group ``(key, trade)`` pairs; ranked by P&L (best first), then key."""
    groups: dict[str, list[float]] = defaultdict(list)
    for key, c in items:
        groups[key or ""].append(c.realised_pnl)
    total_abs = sum(abs(sum(v)) for v in groups.values())
    rows = [
        BreakdownRow(
            key=k,
            count=len(v),
            wins=sum(1 for x in v if x > 0),
            pnl=sum(v),
            win_rate=sum(1 for x in v if x > 0) / len(v),
            share=abs(sum(v)) / total_abs if total_abs else 0.0,
        )
        for k, v in groups.items()
    ]
    return sorted(rows, key=lambda r: (-r.pnl, r.key))
