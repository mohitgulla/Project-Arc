"""Tiered per-underlying cap (D116, card E3.7). Pure: no clock, no I/O, no LLM.

The per-underlying cap depends on how much of the book is already deployed:

    deployed = Σ open max loss + Σ reserved in-flight open max loss   (pre-trade)
    cap_pct  = max_alloc_pct          if deployed <  alloc_tier_threshold_pct × equity
               max_alloc_pct_reduced  otherwise (exact boundary -> reduced)

``deployed`` never counts the trade being checked, so one trade may carry the book
from just below the threshold to threshold + base cap (59% -> at most 74%).

The same helper drives the gate rule ``per_underlying_limit``, pipeline sizing, the
persona prompt lines and the Tower, so they can never disagree.

The reduced tier is never above the base tier: an orphaned (pre-D116) override that
lowered ``max_alloc_pct`` under ``max_alloc_pct_reduced`` caps both tiers at it.
Missing or non-positive equity takes the reduced tier (fail closed; the gate rule
also fails such a proposal outright).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

if TYPE_CHECKING:
    from arc.gate.inputs import Portfolio

__all__ = ["AllocTier", "TierConfig", "alloc_pct_for", "alloc_tier", "deployed_max_loss"]

_ZERO = Decimal(0)


class TierConfig(Protocol):
    """The three settings the tier reads (``ArcSettings`` satisfies it)."""

    @property
    def max_alloc_pct(self) -> float: ...

    @property
    def max_alloc_pct_reduced(self) -> float: ...

    @property
    def alloc_tier_threshold_pct(self) -> float: ...


class AllocTier(NamedTuple):
    """The active per-underlying cap and why."""

    pct: Decimal  # active cap as a share of equity
    tier: Literal["base", "reduced"]
    deployed: Decimal  # dollars, pre-trade
    deployed_pct: Decimal | None  # deployed / equity; None when equity <= 0
    threshold_pct: Decimal

    def describe(self) -> str:
        """``15% tier, deployed 42% < 60%`` / ``10% tier, deployed 63% ≥ 60%``."""
        rel = "<" if self.tier == "base" else "≥"
        dep = "n/a" if self.deployed_pct is None else f"{self.deployed_pct:.0%}"
        return f"{self.pct:.0%} tier, deployed {dep} {rel} {self.threshold_pct:.0%}"


def _d(x: float | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def deployed_max_loss(portfolio: Portfolio, reserved: Decimal | None = None) -> Decimal:
    """Σ max loss of the open positions + the max loss reserved by in-flight opens.

    *reserved* defaults to ``portfolio.reserved_max_loss`` (built outside the gate by
    :func:`arc.pipeline.market.reserved_open_max_loss`); a negative value counts as 0.
    """
    held = sum((p.max_loss for p in portfolio.positions), _ZERO)
    extra = portfolio.reserved_max_loss if reserved is None else reserved
    return held + max(extra, _ZERO)


def alloc_tier(deployed: Decimal, equity: Decimal, cfg: TierConfig) -> AllocTier:
    """The tier for a book with *deployed* max loss (pre-trade) at *equity*."""
    base = _d(cfg.max_alloc_pct)
    reduced = min(_d(cfg.max_alloc_pct_reduced), base)
    threshold = _d(cfg.alloc_tier_threshold_pct)
    if equity <= 0:
        return AllocTier(reduced, "reduced", deployed, None, threshold)
    pct = deployed / equity
    if deployed < threshold * equity:
        return AllocTier(base, "base", deployed, pct, threshold)
    return AllocTier(reduced, "reduced", deployed, pct, threshold)


def alloc_pct_for(deployed: Decimal, equity: Decimal, cfg: TierConfig) -> Decimal:
    """The active per-underlying cap, as a share of equity."""
    return alloc_tier(deployed, equity, cfg).pct
