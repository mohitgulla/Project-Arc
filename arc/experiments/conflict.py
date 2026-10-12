"""D69 (E15.3): ``cross_arm_conflict`` between arms on one shared paper account.

Alpaca paper has no sub-accounts, so two arms trading the same contract on opposite
sides would cross at the broker: Alpaca rejects an order opposite to a working order
on the same contract (403 ``40310000`` "potential wash trade detected"), and an order
opposite to another arm's *position* would net it away (the broker sees one position).
The owner's rule is minimal: refuse the later order, journal ``cross_arm_conflict``,
no netting, no simulated fills.

:func:`find_conflict` is the pre-check (pure): an order leg conflicts when

* another arm's **open order** on the account has a leg on the same contract on the
  opposite side (exactly Alpaca's wash-trade rule), or
* another arm **holds** the contract on the opposite side and the leg is bigger than
  what this arm itself holds on the leg's closing side (a sell larger than this arm's
  own long, a buy larger than its own short), i.e. the leg would trade into the other
  arm's position. Closing this arm's own position never conflicts.

:func:`is_wash_trade_rejection` maps the broker's 403 ``40310000`` to the same reason
(the race the pre-check cannot see: an order placed between the read and the submit).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.execution.resolve import WASH_TRADE_CODE, is_wash_trade_rejection

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.broker.base import MlegOrder

__all__ = [
    "OPEN_ORDER_STATUSES",
    "WASH_TRADE_CODE",
    "find_conflict",
    "is_wash_trade_rejection",
    "open_legs",
]

#: Alpaca order statuses that can still trade (the rest are terminal).
OPEN_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "partially_filled",
        "pending_replace",
        "pending_cancel",
        "held",
        "calculated",
    }
)


def _side(v: object) -> str:
    s = str(v or "").lower()
    return s.rsplit(".", 1)[-1]  # an enum repr ("OrderSide.BUY") -> "buy"


def open_legs(
    orders: Iterable[Mapping[str, Any]], *, exclude_code: str | None
) -> list[tuple[str, str, str]]:
    """``(symbol, side, client_order_id)`` of every leg of every open order on the account
    whose arm code is not *exclude_code* (untagged orders included: they are someone's).

    *orders* are raw ``/v2/orders?nested=true`` rows: an mleg parent carries ``legs``;
    a single-leg order its own ``symbol`` / ``side``.
    """
    from arc.execution.submission import parse_order_id

    out: list[tuple[str, str, str]] = []
    for o in orders:
        if str(o.get("status") or "").lower() not in OPEN_ORDER_STATUSES:
            continue
        coid = str(o.get("client_order_id") or "")
        if exclude_code is not None and parse_order_id(coid).arm_code == exclude_code:
            continue
        legs = [x for x in (o.get("legs") or []) if isinstance(x, dict)]
        if legs:
            out.extend((str(x.get("symbol")), _side(x.get("side")), coid) for x in legs)
        elif o.get("symbol"):
            out.append((str(o["symbol"]), _side(o.get("side")), coid))
    return out


def find_conflict(
    order: MlegOrder,
    *,
    other_open: Sequence[tuple[str, str, str]],
    other_held: Mapping[str, Mapping[str, Decimal]],
    own_held: Mapping[str, Decimal],
) -> str | None:
    """Pure: why *order* conflicts with another arm (see the module doc), else None.

    *other_open*: :func:`open_legs` of the other arms. *other_held*: ``arm -> symbol ->
    signed qty`` of the other arms (attribution). *own_held*: this arm's signed qty.
    """
    for leg in order.legs:
        side = _side(leg.side)
        opposite = "sell" if side == "buy" else "buy"
        for sym, s, coid in other_open:
            if sym == leg.symbol and s == opposite:
                return (
                    f"{side} {leg.symbol} is opposite to another arm's working {s} order "
                    f"({coid[-6:] or 'untagged'}) on the shared account"
                )
        qty = Decimal(leg.ratio_qty * order.qty)
        own = own_held.get(leg.symbol, Decimal(0))
        cover = max(own, Decimal(0)) if side == "sell" else max(-own, Decimal(0))
        if qty <= cover:
            continue  # closes (part of) this arm's own position
        for arm, held in sorted(other_held.items()):
            q = held.get(leg.symbol, Decimal(0))
            if (side == "sell" and q > 0) or (side == "buy" and q < 0):
                return (
                    f"{side} {qty} {leg.symbol} would trade into {arm}'s "
                    f"{'long' if q > 0 else 'short'} {abs(q)} on the shared account"
                )
    return None
