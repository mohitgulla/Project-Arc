"""Input contracts for the risk-proxy gate (pydantic v2, pure data).

The gate never fetches anything. Callers (E5.2 pipeline, E6 execution) build
these snapshots from the broker, the market-data provider and the audit store,
then hand them to :func:`arc.gate.rules.evaluate`.

Units follow :mod:`arc.structures`:
  - money is ``Decimal`` dollars; option prices are per share;
  - Greeks are share-equivalents (per-share Greek x 100 x signed ratio);
    vega is per 1.00 of sigma, so vega / 100 is dollars per vol point.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.models import Greeks


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class AccountSnapshot(_Frozen):
    """Broker account state at evaluation time."""

    equity: Decimal = Field(..., description="Current equity (realized + unrealized), dollars")
    last_equity: Decimal = Field(
        ..., description="Equity at the previous session close (start-of-day basis), dollars"
    )
    halted: bool = Field(False, description="Kill switch / daily halt is active (E3.3)")
    as_of: dt.datetime = Field(..., description="When the snapshot was taken (tz-aware)")


class Position(_Frozen):
    """One open position, as the gate needs to see it."""

    underlying: str
    max_loss: Decimal = Field(..., ge=0, description="Total max loss of the position, dollars")


class ClosedLot(_Frozen):
    """A closed tax lot from the audit store (``tax_lots`` table)."""

    underlying: str
    closed_at: dt.datetime
    realized_pnl: Decimal

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ClosedLot:
        """Build from a ``TaxLotRepo`` row (``ticker``, ``closed_at``, ``realized_pnl``).

        The store writes UTC ISO-8601; a naive timestamp is read as UTC.
        """
        closed_at = dt.datetime.fromisoformat(row["closed_at"])
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=dt.UTC)
        return cls(
            underlying=row["ticker"],
            closed_at=closed_at,
            realized_pnl=Decimal(row["realized_pnl"]),
        )


class Portfolio(_Frozen):
    """Open positions, their aggregate Greeks, and recent closed lots."""

    positions: list[Position] = Field(default_factory=list)
    greeks: Greeks = Field(default_factory=Greeks, description="Net portfolio Greeks")
    closed_lots: list[ClosedLot] = Field(default_factory=list)
    legs: dict[str, int] = Field(
        default_factory=dict,
        description="Held option contracts by OCC symbol: + long / − short (closing checks)",
    )


class Quote(_Frozen):
    """Top-of-book quote for one option contract (per-share prices)."""

    bid: Decimal = Field(..., ge=0)
    ask: Decimal = Field(..., ge=0)
    as_of: dt.datetime


class MarketSnapshot(_Frozen):
    """Quotes and calendar facts the gate checks the proposal against."""

    quotes: dict[str, Quote] = Field(
        default_factory=dict, description="Quote per leg, keyed by the leg's occ_symbol"
    )
    next_earnings: dict[str, dt.date | None] = Field(
        default_factory=dict,
        description="Next earnings date per underlying. Missing key = unknown (fails closed "
        "for short premium); None = known to have no scheduled earnings.",
    )
