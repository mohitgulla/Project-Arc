"""Per-contract liquidity and data-quality filters for the chain scanner.

Every rule is a pure function of one :class:`~arc.data.base.OptionContract`
and a :class:`LiquidityRules`; nothing here touches the network.

Rules (a contract must pass all of them):

- ``no_quote``: bid and ask are both present, ``ask >= bid`` and ``ask > 0``.
- ``zero_bid``: bid > 0 (a zero bid cannot be sold and usually means no market).
- ``missing_greeks``: provider delta and implied vol are present.
- ``stale_quote``: the provider did not flag the quote as stale.
- ``wide_spread``: ``ask - bid <= max(spread_max_pct * mid, spread_max_abs)``,
  i.e. PLAN §5 "spread ≤ 10% of mid **or** ≤ $0.10".
- ``low_open_interest`` / ``low_volume``: at or above the configured minimums.
  Unknown (``None``) open interest or volume fails: missing data is not liquidity.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

    from arc.config import ArcSettings
    from arc.data.base import OptionContract

__all__ = [
    "FilterReport",
    "LiquidityRules",
    "Reject",
    "apply_filters",
    "check_contract",
    "spread_ok",
]


class Reject(StrEnum):
    """Why a contract was dropped by the scanner."""

    NO_QUOTE = "no_quote"
    ZERO_BID = "zero_bid"
    MISSING_GREEKS = "missing_greeks"
    STALE_QUOTE = "stale_quote"
    WIDE_SPREAD = "wide_spread"
    LOW_OPEN_INTEREST = "low_open_interest"
    LOW_VOLUME = "low_volume"


class LiquidityRules(BaseModel):
    """Thresholds for :func:`check_contract`."""

    spread_max_pct: float = Field(0.10, ge=0.0, le=1.0)
    spread_max_abs: float = Field(0.10, ge=0.0)
    min_open_interest: int = Field(100, ge=0)
    min_volume: int = Field(10, ge=0)

    @classmethod
    def from_settings(cls, settings: ArcSettings) -> LiquidityRules:
        """Build rules from :class:`arc.config.ArcSettings`."""
        return cls(
            spread_max_pct=settings.spread_max_pct,
            spread_max_abs=settings.spread_max_abs,
            min_open_interest=settings.scanner_min_open_interest,
            min_volume=settings.scanner_min_volume,
        )


class FilterReport(BaseModel):
    """Outcome of filtering a chain: survivors plus rejection counts."""

    total: int = 0
    kept: int = 0
    rejected: dict[str, int] = Field(
        default_factory=dict, description="Count per first-failing Reject reason"
    )


def spread_ok(bid: float, ask: float, rules: LiquidityRules) -> bool:
    """PLAN §5 spread rule: spread within ``max(pct * mid, abs)``."""
    mid = (bid + ask) / 2.0
    # Tiny epsilon so a spread that is exactly on the limit is not lost to float noise.
    return (ask - bid) <= max(rules.spread_max_pct * mid, rules.spread_max_abs) + 1e-9


def check_contract(c: OptionContract, rules: LiquidityRules) -> Reject | None:
    """Return the first rule *c* fails, or ``None`` if it passes every rule."""
    if c.bid is None or c.ask is None or c.ask < c.bid or c.ask <= 0:
        return Reject.NO_QUOTE
    if c.bid <= 0:
        return Reject.ZERO_BID
    if c.greeks is None or c.greeks.delta is None or not c.implied_volatility:
        return Reject.MISSING_GREEKS
    if any(f.issue == "stale_timestamp" for f in c.quality_flags):
        return Reject.STALE_QUOTE
    if not spread_ok(c.bid, c.ask, rules):
        return Reject.WIDE_SPREAD
    if c.open_interest is None or c.open_interest < rules.min_open_interest:
        return Reject.LOW_OPEN_INTEREST
    if c.volume is None or c.volume < rules.min_volume:
        return Reject.LOW_VOLUME
    return None


def apply_filters(
    contracts: Iterable[OptionContract], rules: LiquidityRules
) -> tuple[list[OptionContract], FilterReport]:
    """Split *contracts* into survivors and a :class:`FilterReport`."""
    kept: list[OptionContract] = []
    reasons: Counter[str] = Counter()
    total = 0
    for c in contracts:
        total += 1
        why = check_contract(c, rules)
        if why is None:
            kept.append(c)
        else:
            reasons[why.value] += 1
    return kept, FilterReport(total=total, kept=len(kept), rejected=dict(sorted(reasons.items())))
