"""Exchange-valid option price increments (D66, card E6.2h).

Every limit Arc sends must sit on the exchange grid for that order:

- **multi-leg** (``mleg``) net price: ``ticks.mleg`` ($0.01) at any price;
- **single-leg** (one ratio-1 leg, sent as a simple order; see
  :func:`arc.broker.alpaca_paper.build_mleg_request`), by ``abs(price)``:

  - underlying in ``ticks.penny_all_underlyings`` (SPY, QQQ, IWM): $0.01;
  - Penny Program class (Alpaca ``ppind`` true): $0.01 below $3, $0.05 at $3+;
  - otherwise, **including an unknown ppind**: $0.05 below $3, $0.10 at $3+
    (fail toward the wider grid).

A :class:`TickGrid` is the set of valid prices for one order. :meth:`TickGrid.snap`
rounds onto the grid that applies *at the result* (a price near $3 may move
onto the wider grid), and is idempotent on prices already on the grid.

Sign convention as everywhere in Arc: per-share net price, ``+`` debit, ``−``
credit. ``up`` rounds toward ``+∞`` (the marketable side for the buyer of the
combo: pay more debit, take less credit), ``down`` toward ``−∞``.

Pure: no clock, no I/O, no network. The ``ppind`` flags are looked up by the
data layer and reach the gate only through ``MarketSnapshot.penny_program``.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.structures import parse_occ

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.config import TickRules
    from arc.models import Leg

__all__ = [
    "Direction",
    "TickGrid",
    "is_single_leg",
    "leg_penny",
    "legs_grid",
    "max_decimal_places_ok",
    "order_grid",
    "order_tick",
]

Direction = Literal["up", "down"]
_CENT = Decimal("0.01")


def _ceil_to(x: Decimal, tick: Decimal) -> Decimal:
    return (x / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def max_decimal_places_ok(price: Decimal) -> bool:
    """True when *price* has at most 2 decimal places (Alpaca rejects 3 or more)."""
    return price == price.quantize(_CENT)


class TickGrid(BaseModel):
    """The valid limit prices for one order.

    ``below`` applies while ``abs(price) < boundary``, ``above`` at or beyond it.
    ``boundary=None`` is a flat grid (``below`` everywhere). ``label`` names the
    rule in gate messages, e.g. ``single-leg penny``.
    """

    model_config = ConfigDict(frozen=True)

    below: Decimal = Field(..., gt=0)
    above: Decimal = Field(..., gt=0)
    boundary: Decimal | None = Field(None, gt=0)
    label: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> TickGrid:
        if self.below % _CENT or self.above % _CENT:
            msg = f"grid increments must be whole cents ({self.below}, {self.above})"
            raise ValueError(msg)
        if self.boundary is not None and (self.boundary % self.below or self.boundary % self.above):
            msg = f"grid boundary {self.boundary} must be on both increments"
            raise ValueError(msg)
        return self

    @classmethod
    def flat(cls, tick: Decimal, label: str = "") -> TickGrid:
        """One increment at every price (multi-leg net; SPY/QQQ/IWM single-leg)."""
        return cls(below=tick, above=tick, boundary=None, label=label)

    def tick_at(self, price: Decimal) -> Decimal:
        """The increment that applies at *price* (by ``abs(price)``)."""
        if self.boundary is not None and abs(price) >= self.boundary:
            return self.above
        return self.below

    def describe(self, price: Decimal) -> str:
        """``single-leg penny ≥$3`` style label of the rule applied at *price*."""
        if self.boundary is None:
            return self.label
        side = "≥" if abs(price) >= self.boundary else "<"
        return f"{self.label} {side}${self.boundary:.0f}"

    def on_grid(self, price: Decimal) -> bool:
        """True when *price* is a whole multiple of the increment at its own level."""
        return price % self.tick_at(price) == 0

    def snap(self, price: Decimal, direction: Direction) -> Decimal:
        """The nearest grid price at or beyond *price* in *direction* (idempotent on the grid).

        ``up`` = smallest valid price ``>= price``; ``down`` = largest ``<= price``.
        """
        if direction == "down":
            return -self._snap_up(-price)
        return self._snap_up(price)

    def _snap_up(self, x: Decimal) -> Decimal:
        if self.boundary is None:
            return _ceil_to(x, self.below)
        b = self.boundary
        # Inner zone (-b, b) on ``below``: the smallest such multiple >= x, if any.
        inner = max(_ceil_to(x, self.below), -b + self.below)
        # Outer zone |p| >= b on ``above``: the smallest such multiple >= x
        # (b itself is on ``above``, so it is the outer candidate inside the zone).
        outer = _ceil_to(x, self.above)
        if abs(outer) < b:
            outer = b
        return min(inner, outer) if inner < b else outer


def is_single_leg(legs: Sequence[Leg]) -> bool:
    """One ratio-1 leg: sent to the broker as a simple order, priced on the class grid."""
    return len(legs) == 1 and legs[0].ratio == 1


def leg_penny(legs: Sequence[Leg]) -> dict[str, bool | None]:
    """``occ_symbol → penny_program`` from the legs themselves (the execution side)."""
    return {leg.occ_symbol: leg.penny_program for leg in legs}


def order_grid(legs: Sequence[Leg], ticks: TickRules, penny: Mapping[str, bool | None]) -> TickGrid:
    """The price grid for an order of *legs*.

    *penny* maps OCC symbol → Penny Program flag (``ppind``); a missing key or
    ``None`` is unknown and falls back to the standard (wider) grid.
    """
    if not is_single_leg(legs):
        return TickGrid.flat(ticks.mleg, "mleg net")
    (leg,) = legs
    root = parse_occ(leg.occ_symbol).root.upper()
    if root in ticks.penny_all_underlyings:
        return TickGrid.flat(ticks.penny_all, f"single-leg {root}")
    if penny.get(leg.occ_symbol) is True:
        return TickGrid(
            below=ticks.penny_below,
            above=ticks.penny_above,
            boundary=ticks.boundary,
            label="single-leg penny",
        )
    return TickGrid(
        below=ticks.standard_below,
        above=ticks.standard_above,
        boundary=ticks.boundary,
        label="single-leg standard",
    )


def order_tick(
    legs: Sequence[Leg], price: Decimal, ticks: TickRules, penny: Mapping[str, bool | None]
) -> Decimal:
    """The increment a limit at *price* must be a multiple of for an order of *legs*."""
    return order_grid(legs, ticks, penny).tick_at(price)


def legs_grid(legs: Sequence[Leg], ticks: TickRules) -> TickGrid:
    """The grid from the legs' own ``penny_program`` flags (execution and submit side).

    The gate reads the same flags from ``MarketSnapshot.penny_program``; both come
    from the chain contracts the proposal was priced on.
    """
    return order_grid(legs, ticks, leg_penny(legs))
