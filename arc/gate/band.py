"""Price band for bounded price improvement (D24; card E6.2).

An approved proposal is worked as a ladder of limit prices: attempt 0 at the
mid limit, then ``max_steps`` improvement steps toward the far side of the
combo NBBO, never past the band's worst price. The gate checks the *whole*
band and mints one ``arc2`` token bound to it (:mod:`arc.gate.token`), so one
approval authorises every attempt.

Sign convention (as everywhere in Arc): per-share net price, ``+`` = debit,
``-`` = credit. For the buyer of the combo a *worse* price is always
numerically higher (pay more debit, receive less credit), so ``lo`` is the
start (mid) and ``hi`` is the worst price: ``lo <= hi``.

Pure: no clock, no I/O.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["MAX_BAND_STEPS", "PriceBand", "band_from_nbbo"]

MAX_BAND_STEPS = 9
_CENT = Decimal("0.01")


def _floor_to(x: Decimal, tick: Decimal) -> Decimal:
    return (x / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


class PriceBand(BaseModel):
    """``[lo .. hi]`` per-share limit prices plus the number of improvement steps."""

    model_config = ConfigDict(frozen=True)

    lo: Decimal = Field(..., allow_inf_nan=False, description="Start limit (mid), per share")
    hi: Decimal = Field(..., allow_inf_nan=False, description="Worst limit after max_steps")
    max_steps: int = Field(..., ge=0, le=MAX_BAND_STEPS)

    @model_validator(mode="after")
    def _ordered(self) -> PriceBand:
        if self.hi < self.lo:
            msg = f"band hi {self.hi} is below lo {self.lo} (hi must be the worse price)"
            raise ValueError(msg)
        if self.lo % _CENT or self.hi % _CENT:
            msg = "band prices must be whole cents"
            raise ValueError(msg)
        return self

    @property
    def attempts(self) -> int:
        """Orders the ladder may send: the mid attempt plus every step."""
        return self.max_steps + 1

    def contains(self, price: Decimal) -> bool:
        return self.lo <= price <= self.hi

    def ladder(self, tick: Decimal) -> tuple[Decimal, ...]:
        """Price for attempt ``k`` (0 = mid): ``lo + (hi − lo)·k/N`` floored to ``tick``.

        The last attempt is exactly ``hi``. Floors toward ``lo`` so no attempt is
        worse than its share of the band.
        """
        if tick <= 0:
            msg = f"tick must be positive, got {tick}"
            raise ValueError(msg)
        n = self.max_steps
        if n == 0:
            return (self.lo,)
        width = self.hi - self.lo
        inner = tuple(self.lo + _floor_to(width * k / n, tick) for k in range(1, n))
        return (self.lo, *inner, self.hi)


def band_from_nbbo(
    start: Decimal, far: Decimal, *, max_steps: int, reach: Decimal, tick: Decimal
) -> PriceBand:
    """Band from the start (mid) limit toward the far touch ``far`` of the combo NBBO.

    ``hi = start + reach·(far − start)`` floored to ``tick`` (never below
    ``start``). With ``reach = 1`` the last step is the far touch. When the far
    touch is not worse than the start there is no room to improve: ``hi = lo`` and
    ``max_steps = 0`` (one attempt at the start price).
    """
    if tick <= 0 or not Decimal(0) <= reach <= Decimal(1):
        msg = f"invalid tick {tick} or reach {reach}"
        raise ValueError(msg)
    room = max(far - start, Decimal(0))
    hi = start + _floor_to(room * reach, tick)
    return PriceBand(lo=start, hi=hi, max_steps=max_steps if hi > start else 0)
