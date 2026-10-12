"""Options open-interest walls (E23.0, PLAN D98).

Per underlying: the strike with the largest call open interest above spot (the call
wall, a resistance proxy), the strike with the largest put open interest below spot
(the put wall, a support proxy), and the strike with the largest total open interest
(either side of spot). Open interest is summed per strike across the given expiries
(the swing-sleeve expiries in production), from the Alpaca contract OI the chain
already carries (``OptionContract.open_interest``).

A strike qualifies as a wall only when its summed OI is at least ``min_oi``. Ties go
to the strike nearest spot, then the lower strike. Pure: no network, no LLM, no clock;
context only, never a gate input.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - used at runtime in the pydantic model
import math
from collections import defaultdict
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["OiContract", "OiWalls", "oi_walls"]


class OiContract(Protocol):
    """An option contract with OI (e.g. ``arc.data.base.OptionContract``)."""

    @property
    def expiration(self) -> dt.date: ...
    @property
    def strike(self) -> float: ...
    @property
    def option_type(self) -> str: ...
    @property
    def open_interest(self) -> int | None: ...


class OiWalls(BaseModel):
    """The OI walls of one underlying (strikes in the contracts' currency)."""

    model_config = ConfigDict(extra="forbid")

    spot: float = Field(..., gt=0.0)
    expiries: list[dt.date] = Field(default_factory=list, description="Expiries summed")
    call_wall: float | None = Field(None, description="Max call-OI strike above spot")
    call_wall_oi: int | None = None
    put_wall: float | None = Field(None, description="Max put-OI strike below spot")
    put_wall_oi: int | None = None
    max_oi_strike: float | None = Field(None, description="Max total-OI strike (any side)")
    max_oi: int | None = None
    contracts_with_oi: int = Field(0, ge=0)


def _best(oi: Mapping[float, int], spot: float, min_oi: int) -> tuple[float, int] | None:
    rows = [(k, v) for k, v in oi.items() if v >= min_oi and v > 0]
    if not rows:
        return None
    return min(rows, key=lambda kv: (-kv[1], abs(kv[0] - spot), kv[0]))


def oi_walls(
    contracts: Iterable[OiContract],
    spot: float,
    expiries: Iterable[dt.date],
    *,
    min_oi: int = 0,
) -> OiWalls | None:
    """The walls of *contracts* within *expiries* (``None``: no usable spot)."""
    if not math.isfinite(spot) or spot <= 0:
        return None
    keep = set(expiries)
    calls: dict[float, int] = defaultdict(int)
    puts: dict[float, int] = defaultdict(int)
    total: dict[float, int] = defaultdict(int)
    used: set[dt.date] = set()
    n = 0
    for c in contracts:
        oi = c.open_interest
        if c.expiration not in keep or oi is None or oi <= 0:
            continue
        strike = float(c.strike)
        kind = str(c.option_type).lower()
        if kind.startswith("c"):
            if strike > spot:
                calls[strike] += int(oi)
        elif kind.startswith("p"):
            if strike < spot:
                puts[strike] += int(oi)
        else:
            continue
        # every call and put counts toward the strike's total, whichever side of spot
        total[strike] += int(oi)
        used.add(c.expiration)
        n += 1
    call = _best(calls, spot, min_oi)
    put = _best(puts, spot, min_oi)
    top = _best(total, spot, min_oi)
    return OiWalls(
        spot=spot,
        expiries=sorted(used),
        call_wall=call[0] if call else None,
        call_wall_oi=call[1] if call else None,
        put_wall=put[0] if put else None,
        put_wall_oi=put[1] if put else None,
        max_oi_strike=top[0] if top else None,
        max_oi=top[1] if top else None,
        contracts_with_oi=n,
    )
