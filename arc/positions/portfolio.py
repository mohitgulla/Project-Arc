"""Typed portfolio-context and market-guard models (E5.9, D33). Leaf: no pipeline imports.

:class:`PortfolioContext` is the payload of the ``portfolio_context`` context kind
(:mod:`arc.context.kinds`); :class:`MarketGuard` rides on the ``shortlist`` payload.
The builders live in :mod:`arc.pipeline.portfolio_context` and
:mod:`arc.pipeline.market_guard`.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic field
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.journal.reasons import ReasonCode
from arc.models import Greeks, Stance

__all__ = [
    "BUCKET_DISPLAY",
    "ExpiryBucket",
    "GreekUsage",
    "MarketGuard",
    "VixReading",
    "PortfolioAccount",
    "PortfolioAggregates",
    "PortfolioContext",
    "PortfolioFlag",
    "PortfolioPosition",
    "PortfolioThesis",
    "bucket_display",
    "expiry_bucket",
    "relabel_buckets",
]

_FORBID = ConfigDict(extra="forbid")

PortfolioFlag = Literal[
    "over_concentrated_sector",
    "stance_skew",
    "expiry_cluster",
    "delta_near_cap",
    "vega_near_cap",
]
ExpiryBucket = Literal["0-7", "8-21", "22-45", "46+"]
_BUCKETS: tuple[tuple[int, ExpiryBucket], ...] = ((7, "0-7"), (21, "8-21"), (45, "22-45"))

# E3.4a (Analyst A-4): prompts show the expiry concentration buckets in weeks, never
# as day ranges next to "DTE", so a bucket label ("22-45") cannot be read back as an
# entry window. The stored ``ExpiryBucket`` values (context payloads) are unchanged.
BUCKET_DISPLAY: dict[str, str] = {"0-7": "0-1w", "8-21": "1-3w", "22-45": "3-6w", "46+": "6w+"}
_LEGACY_BUCKET = re.compile(r"(?<![\d.])(0-7|8-21|22-45|46\+)(?![\d])")


def bucket_display(label: str) -> str:
    """The week-based prompt label of an expiry bucket."""
    return BUCKET_DISPLAY.get(label, label)


def relabel_buckets(block: str) -> str:
    """Rewrite day-range bucket labels in a rendered portfolio block to week labels.

    The renderer already writes week labels; this covers blocks recorded before
    E3.4a (``persona_calls.prompt_inputs.portfolio_block``) so a replay or a carried
    block never shows ``22-45`` / ``46+`` to a persona.
    """
    return _LEGACY_BUCKET.sub(lambda m: BUCKET_DISPLAY[m.group(1)], block)


def expiry_bucket(dte: int) -> ExpiryBucket:
    """DTE -> one of four buckets (``0-7``, ``8-21``, ``22-45``, ``46+``)."""
    for upper, name in _BUCKETS:
        if dte <= upper:
            return name
    return "46+"


# ---------------------------------------------------------------------------
# Models (extra="forbid")
# ---------------------------------------------------------------------------


class PortfolioAccount(BaseModel):
    model_config = _FORBID

    equity: float
    day_pnl: float | None = Field(None, description="equity − last_equity")
    open_pnl_total: float = Field(0.0, description="Sum of open positions' mark P&L ($)")
    cash: float
    buying_power: float
    halted: bool
    order_budget_tier: str


class PortfolioThesis(BaseModel):
    model_config = _FORBID

    director: str = Field("", description="The Director's thesis when the position was proposed")
    scout_catalyst: str | None = None
    scout_catalyst_date: str | None = None
    scout_stance: Stance | None = None
    scout_confidence: float | None = None


class PortfolioPosition(BaseModel):
    """One open structure as the Director sees it (money in $ for all contracts)."""

    model_config = _FORBID

    structure_id: str
    ticker: str
    sector: str
    kind: str | None
    stance: Stance
    dte: int
    expiry_bucket: ExpiryBucket
    contracts: int
    entry_net: float = Field(..., description="Per share, + debit / − credit")
    mark_pnl_per_share: float | None = None
    mark_pnl_total: float | None = None
    pct_of_max_gain: float | None = None
    pct_of_max_loss: float | None = None
    max_loss_total: float
    max_loss_pct_equity: float
    remaining_ev: float | None = Field(None, description="$ per unit (E6.4 review)")
    greeks: Greeks = Field(default_factory=Greeks, description="Contribution, all contracts")
    thesis: PortfolioThesis = Field(default_factory=PortfolioThesis)
    exit_pending: bool = False
    signals: list[str] = Field(default_factory=list, description="Fired exit signals (kinds)")
    review_source: Literal["position_review", "computed", "none"] = "none"
    opened_at: str


class GreekUsage(BaseModel):
    model_config = _FORBID

    net: float
    cap: float
    pct_used: float | None = Field(None, description="|net| / cap; None when the cap is 0")


class PortfolioAggregates(BaseModel):
    model_config = _FORBID

    total_max_loss: float
    by_underlying: dict[str, float] = Field(default_factory=dict, description="share of max loss")
    by_sector: dict[str, float] = Field(default_factory=dict)
    by_stance: dict[str, float] = Field(default_factory=dict)
    by_expiry_bucket: dict[str, float] = Field(default_factory=dict)
    hhi_underlying: float = Field(0.0, description="Σ share² over underlyings (1 = one name)")
    delta: GreekUsage
    vega: GreekUsage
    gamma: float
    theta: float
    greeks_source: Literal["market", "as_opened"] = "market"
    positions: int
    max_positions: int
    flags: list[PortfolioFlag] = Field(default_factory=list)
    flagged_sectors: list[str] = Field(default_factory=list)
    flagged_stances: list[str] = Field(default_factory=list)
    flagged_expiry_buckets: list[str] = Field(default_factory=list)
    at_cap_underlyings: list[str] = Field(
        default_factory=list, description="Names at the per-underlying max-loss cap"
    )


class PortfolioContext(BaseModel):
    """The Director's view of the book (E5.9). ``empty`` = behave as today."""

    model_config = _FORBID

    as_of: _dt.datetime
    empty: bool
    account: PortfolioAccount
    positions: list[PortfolioPosition] = Field(default_factory=list)
    aggregates: PortfolioAggregates | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Market-conditions guard (arc.pipeline.market_guard evaluates it)
# ---------------------------------------------------------------------------


class VixReading(BaseModel):
    model_config = _FORBID

    value: float = Field(..., gt=0)
    as_of: str
    source: Literal["vol_term", "market"]
    structure: Literal["contango", "flat", "backwardation"] | None = None


class MarketGuard(BaseModel):
    """What the guard decided and why (journaled, shown on the Director card)."""

    model_config = _FORBID

    opens_allowed: bool
    reason_code: Literal["market_unclear", "market_data_missing"] | None = None
    reasons: list[str] = Field(default_factory=list)
    vix: VixReading | None = None
    regime: str | None = None
    regime_stickiness: float | None = None
    checked: list[str] = Field(default_factory=list, description="Checks that ran")

    @property
    def code(self) -> ReasonCode | None:
        return ReasonCode(self.reason_code) if self.reason_code else None

    def summary(self) -> str:
        if self.opens_allowed:
            return "market guard: clear" + (f" (VIX {self.vix.value:.1f})" if self.vix else "")
        return f"market guard: no new opens ({'; '.join(self.reasons)})"
