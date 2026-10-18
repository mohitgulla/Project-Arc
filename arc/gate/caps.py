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

D131 (E28.3): :func:`open_risk` reads the same ``deployed`` sum for the portfolio
open-risk cap (rule ``open_risk_cap``: deployed + new max loss ≤ ``max_open_risk_pct``
× equity), so the tier, the cap, sizing, prompts and the Tower share one number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

if TYPE_CHECKING:
    from arc.gate.inputs import Portfolio

__all__ = [
    "AllocTier",
    "OpenRisk",
    "TierConfig",
    "alloc_pct_for",
    "alloc_tier",
    "deployed_max_loss",
    "open_risk",
    "tier_open_risk",
]

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


# ---------------------------------------------------------------------------
# D131 (E28.3): portfolio open-risk cap
# ---------------------------------------------------------------------------


class OpenRisk(NamedTuple):
    """The book's open risk against ``max_open_risk_pct`` (D131, rule ``open_risk_cap``).

    ``deployed`` is :func:`deployed_max_loss` (open + reserved in-flight opens,
    pre-trade), the same number the E3.7 tier reads. ``headroom`` is the max loss a new
    open may still add (floored at 0); ``None`` when equity is unknown or non-positive
    (the rule then fails closed and sizing proposes nothing).
    """

    cap_pct: Decimal
    deployed: Decimal
    deployed_pct: Decimal | None
    headroom: Decimal | None

    def describe(self) -> str:
        """``open risk 12% / 40%`` (``open risk n/a / 40%`` without equity)."""
        dep = "n/a" if self.deployed_pct is None else f"{self.deployed_pct:.0%}"
        return f"open risk {dep} / {self.cap_pct:.0%}"

    @property
    def shown(self) -> bool:
        """Whether prompts/lines carry it: a cap of 100% is the rollback (lines unchanged)."""
        return self.cap_pct < 1

    def clause(self) -> str:
        """``, open risk 12% / 40%`` for a prompt/line; ``""`` at the 100% rollback."""
        return f", {self.describe()}" if self.shown else ""


def open_risk(deployed: Decimal, equity: Decimal, cap_pct: float | Decimal) -> OpenRisk:
    """The open-risk position of a book with *deployed* max loss (pre-trade) at *equity*."""
    cap = _d(cap_pct)
    if equity <= 0:
        return OpenRisk(cap, deployed, None, None)
    return OpenRisk(cap, deployed, deployed / equity, max(cap * equity - deployed, _ZERO))


def tier_open_risk(tier: AllocTier, equity: Decimal, cap_pct: float | Decimal) -> OpenRisk:
    """:func:`open_risk` on an :class:`AllocTier`'s own deployed sum (one number, two caps).

    A tier built for an unvalued book (``deployed_pct`` None, ``deployed`` = equity)
    keeps ``deployed_pct`` None: the line shows ``n/a`` and the headroom is 0.
    """
    risk = open_risk(tier.deployed, equity, cap_pct)
    return risk if tier.deployed_pct is not None else risk._replace(deployed_pct=None)
