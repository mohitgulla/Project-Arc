"""Delta-cap exposure per structure (D116, card E3.8 "option C"). Pure: no clock, no I/O.

Both portfolio delta caps (``portfolio_dollar_delta_cap_pct``, D57, and
``portfolio_beta_delta_cap_pct``, D62) sum one dollar term per structure:

    vertical_debit / long_call / long_put   sign(net Δ) × max loss        (D116)
    every other kind                        net Δ (share-eq) × spot      (D57)

The beta cap multiplies either term by β used = max(β vs SPY, 1.0) (D62).

A defined-risk debit structure can never lose more than the debit paid, which the
tiered per-underlying cap (E3.7) already bounds, so its delta-cap weight is its max
loss, signed by its delta (Δ == 0 contributes 0). Credit verticals, condors and
anything else keep Δ × spot. The same helper drives the gate rule ``greek_caps``,
``arc.pipeline.market.build_portfolio`` (the book side), the in-run book carried by
the pipeline and the Research portfolio context, so book and proposal use one unit.

The raw Δ × spot aggregates are still carried for display (``dollar_delta_raw``);
the gate never reads them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from arc.models import StructureKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.models import Leg

__all__ = ["MAX_LOSS_DELTA_KINDS", "delta_exposure", "kind_of"]

#: D116: structures whose delta-cap weight is sign(Δ) × max loss instead of Δ × spot.
MAX_LOSS_DELTA_KINDS: frozenset[StructureKind] = frozenset(
    {StructureKind.VERTICAL_DEBIT, StructureKind.LONG_CALL, StructureKind.LONG_PUT}
)

_ZERO = Decimal(0)


def _d(x: float | int | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def kind_of(legs: Sequence[Leg]) -> StructureKind:
    """The gate's own classification of *legs*; ``OTHER`` when they cannot be classified.

    A malformed structure is reported by the gate's ``structure_invalid`` rule; for the
    delta measure it simply keeps the Δ × spot unit.
    """
    from arc.structures import classify

    try:
        return classify(legs)
    except ValueError:
        return StructureKind.OTHER


def delta_exposure(
    kind: StructureKind,
    delta: float | Decimal,
    spot: Decimal | None,
    max_loss: Decimal | None,
) -> Decimal | None:
    """One structure's signed dollar term for the delta caps (before any β weight).

    *delta* is the structure's total net Δ (share-equivalents × contracts), *spot* the
    underlying's spot, *max_loss* its total max loss in dollars (``None`` = unbounded).
    For :data:`MAX_LOSS_DELTA_KINDS` with a bounded max loss: ``sign(Δ) × max_loss``
    (spot is not needed). Otherwise ``Δ × spot``; ``None`` when that needs a spot
    that is missing (callers fail closed or report it unknown).
    """
    dl = _d(delta)
    if kind in MAX_LOSS_DELTA_KINDS and max_loss is not None:
        if dl == 0:
            return _ZERO
        return abs(max_loss) if dl > 0 else -abs(max_loss)
    if spot is None:
        return None
    return dl * spot
