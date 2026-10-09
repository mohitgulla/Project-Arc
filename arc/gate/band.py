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

D66 (E6.2h): every price is snapped onto the order's exchange grid
(:class:`arc.gate.ticks.TickGrid`); a bare ``Decimal`` tick is a flat grid.
Ladder steps that collapse onto the same grid price are sent once.

Pure: no clock, no I/O.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.gate.ticks import TickGrid

__all__ = ["MAX_BAND_STEPS", "PriceBand", "as_grid", "band_from_nbbo"]

MAX_BAND_STEPS = 9
_CENT = Decimal("0.01")


def as_grid(grid: TickGrid | Decimal) -> TickGrid:
    """*grid* as a :class:`TickGrid` (a ``Decimal`` is one flat increment)."""
    if isinstance(grid, TickGrid):
        return grid
    if grid <= 0:
        msg = f"tick must be positive, got {grid}"
        raise ValueError(msg)
    return TickGrid.flat(grid)


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
        """Orders the ladder may send at most: the mid attempt plus every step."""
        return self.max_steps + 1

    def contains(self, price: Decimal) -> bool:
        return self.lo <= price <= self.hi

    def reanchor(self, mid: Decimal, grid: TickGrid | Decimal) -> PriceBand | None:
        """The band re-started at a fresh *mid* (D34 stale-quote re-price), or ``None``.

        Pure. ``mid`` is snapped onto *grid* toward the marketable side (up: pay
        at most one increment more, as :func:`arc.pipeline.market.limit_price`
        does). The result keeps ``hi`` and ``max_steps``, so every attempt is still
        inside the band the gate signed into the token; a fresh mid that is already
        *better* than ``lo`` also starts at ``lo`` (never outside the band). A mid
        past ``hi`` returns ``None``: the band is stale and nothing may be sent
        without a new gate decision. The band is never widened.
        """
        start = as_grid(grid).snap(mid, "up")
        if start > self.hi:
            return None
        start = max(start, self.lo)
        return PriceBand(lo=start, hi=self.hi, max_steps=self.max_steps if self.hi > start else 0)

    def ladder(self, grid: TickGrid | Decimal) -> tuple[Decimal, ...]:
        """Distinct grid prices to send, attempt 0 (the start) first.

        Step ``k`` targets ``lo + (hi − lo)·k/N`` snapped down onto *grid* (toward
        ``lo``, so no attempt is worse than its share of the band); the start is
        ``lo`` snapped up and the last step ``hi`` snapped down, so every price is
        inside the band. Steps that land on the same grid price are sent once (a
        narrow band on a coarse grid gets fewer attempts, never repeats). Empty
        when the band holds no grid price at all (nothing valid can be sent).
        """
        g = as_grid(grid)
        first, last = g.snap(self.lo, "up"), g.snap(self.hi, "down")
        if last < first:
            return ()
        n = self.max_steps
        if n == 0:
            return (first,)
        width = self.hi - self.lo
        inner = [max(g.snap(self.lo + width * k / n, "down"), first) for k in range(1, n)]
        out: list[Decimal] = [first]
        for p in (*inner, last):
            if p != out[-1]:
                out.append(p)
        return tuple(out)


def band_from_nbbo(
    start: Decimal,
    far: Decimal,
    *,
    max_steps: int,
    reach: Decimal,
    grid: TickGrid | Decimal,
    cap: Decimal | None = None,
) -> PriceBand:
    """Band from the start (mid) limit toward the far touch ``far`` of the combo NBBO.

    ``hi = start + reach·(min(far, cap) − start)`` snapped down onto *grid* (never
    below ``start``). With ``reach = 1`` the last step is the far touch. ``cap``
    is the worst price the structure can still profit at (see
    :func:`arc.gate.rules.max_gain_cap`); ``None`` = no cap. When neither the
    far touch nor the cap is worse than the start there is no room to improve:
    ``hi = lo`` and ``max_steps = 0`` (one attempt at the start price).
    """
    if not Decimal(0) <= reach <= Decimal(1):
        msg = f"invalid reach {reach}"
        raise ValueError(msg)
    g = as_grid(grid)
    if cap is not None:
        far = min(far, cap)
    room = max(far - start, Decimal(0))
    hi = max(g.snap(start + room * reach, "down"), start)
    return PriceBand(lo=start, hi=hi, max_steps=max_steps if hi > start else 0)
