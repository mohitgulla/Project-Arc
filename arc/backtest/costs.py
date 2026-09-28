"""Explicit transaction-cost model for the backtester (PLAN §6 item 6, §7 QFX lesson).

Fill model
----------
Every leg fills at ``mid ± x · spread`` where ``x`` = :attr:`CostModel.slippage_frac`:
buys pay ``mid + x·spread``, sells receive ``max(mid − x·spread, 0)``.
``x = 0`` is a fill at mid, ``x = 0.5`` is crossing to the far touch.

Spread source
-------------
* When the row carries a closing NBBO (ThetaData EOD) the quoted
  ``ask − bid`` is used and ``mid = (bid + ask) / 2``.
* Alpaca history has **no historical quotes** (E7.1 finding), only trade
  closes. Then ``mid`` is the trade close and the spread is *estimated* as
  ``max(spread_min, spread_pct · mid)``. This estimate is an explicit,
  reported assumption; the baseline report runs a sensitivity grid over it.

Fees
----
``commission_per_contract`` is charged on every contract opened and on every
contract closed at expiry that is in the money (an OTM leg expires worthless
with no closing trade).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CostModel", "LegSide"]

LegSide = int  # +1 buy (long), -1 sell (short)


class CostModel(BaseModel):
    """Slippage + commission assumptions. All prices are per share."""

    model_config = ConfigDict(frozen=True)

    slippage_frac: float = Field(
        0.25, ge=0.0, le=1.0, description="x: fill at mid ± x·spread (0 = mid, 0.5 = touch)"
    )
    commission_per_contract: float = Field(
        0.65, ge=0.0, description="$ per contract per side (opening and ITM closing trades)"
    )
    spread_pct: float = Field(
        0.04, ge=0.0, description="Estimated spread as a fraction of mid when no quote exists"
    )
    spread_min: float = Field(
        0.03, ge=0.0, description="Estimated spread floor ($/share) when no quote exists"
    )

    def spread(self, mid: float, bid: float | None = None, ask: float | None = None) -> float:
        """Quoted spread if a valid NBBO is present, else the estimate."""
        if _valid_quote(bid, ask):
            assert bid is not None and ask is not None  # narrowed by _valid_quote
            return ask - bid
        return max(self.spread_min, self.spread_pct * max(mid, 0.0))

    def mid(self, close: float | None, bid: float | None = None, ask: float | None = None) -> float:
        """Mid from a valid NBBO, else the trade close."""
        if _valid_quote(bid, ask):
            assert bid is not None and ask is not None
            return (bid + ask) / 2.0
        if close is None or not math.isfinite(close) or close < 0:
            msg = "row has neither a valid quote nor a close price"
            raise ValueError(msg)
        return close

    def fill(self, mid: float, spread: float, side: LegSide) -> float:
        """Per-share fill for one leg: buys pay up, sells give up, never below 0."""
        if side not in (1, -1):
            msg = "side must be +1 (buy) or -1 (sell)"
            raise ValueError(msg)
        return max(mid + side * self.slippage_frac * spread, 0.0)

    def fees(self, contracts: int) -> float:
        """Dollar commission for *contracts* contracts."""
        return self.commission_per_contract * contracts


def _valid_quote(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None:
        return False
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return False
    return 0.0 <= bid <= ask and ask > 0.0
