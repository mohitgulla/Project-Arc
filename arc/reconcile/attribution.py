"""Attribute broker option legs to Arc's open structures (E6.3, Sentinel S-5).

Pure: no I/O. Both :func:`arc.pipeline.market.build_portfolio` and the
post-market reconciler use it, so the gate and the reconciler agree on which
broker legs belong to which structure.

A structure is *attributed* when the broker holds every one of its legs with the
right sign and at least ``ratio x contracts`` of it (structures are matched in
the order they were opened). Whatever the broker holds beyond the attributed
structures is *unattributed*: the reconciler reports it as a mismatch, and the
portfolio still values it, grouped by ``(root, expiration)``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arc.models import Leg, LegIntent, Structure
from arc.structures import parse_occ

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Iterable, Mapping, Sequence
    from decimal import Decimal

    from arc.broker.base import BrokerPosition

__all__ = [
    "Attribution",
    "BrokerLeg",
    "StructureHolding",
    "attribute",
    "broker_legs",
    "holdings_from_rows",
]


@dataclass(frozen=True)
class BrokerLeg:
    """One option position at the broker, signed (+ long / - short) in contracts."""

    occ_symbol: str
    qty: int
    avg_entry_price: Decimal | None = None


@dataclass(frozen=True)
class StructureHolding:
    """What one ``open_structures`` row should hold: occ -> signed contracts."""

    structure_id: str
    ticker: str
    legs: Mapping[str, int]
    premiums: Mapping[str, Decimal | None] = field(default_factory=dict)


@dataclass
class Attribution:
    """The result of matching broker legs to local structures."""

    attributed: list[StructureHolding] = field(default_factory=list)
    unmatched: list[StructureHolding] = field(default_factory=list)  # local, not fully held
    unattributed: dict[str, int] = field(default_factory=dict)  # broker legs left over
    non_option: list[str] = field(default_factory=list)  # broker positions that are not options

    def leftover_groups(
        self, entry: Mapping[str, Decimal | None] | None = None
    ) -> dict[tuple[str, _dt.date], list[Leg]]:
        """Unattributed legs grouped by ``(root, expiration)``, as :class:`Leg` rows."""
        entry = entry or {}
        groups: dict[tuple[str, _dt.date], list[Leg]] = defaultdict(list)
        for sym, qty in sorted(self.unattributed.items()):
            occ = parse_occ(sym)
            groups[(occ.root, occ.expiration)].append(
                Leg(
                    occ_symbol=sym,
                    side=LegIntent.LONG if qty > 0 else LegIntent.SHORT,
                    ratio=abs(qty),
                    premium=entry.get(sym),
                )
            )
        return dict(groups)


def broker_legs(positions: Iterable[BrokerPosition]) -> tuple[list[BrokerLeg], list[str]]:
    """Signed option legs from broker positions, plus the symbols of non-option positions.

    Raises ``ValueError`` for a fractional option quantity (never valid).
    """
    legs: list[BrokerLeg] = []
    other: list[str] = []
    for p in positions:
        if p.asset_class != "us_option":
            other.append(p.symbol)
            continue
        qty = abs(p.qty)
        if qty != qty.to_integral_value():
            msg = f"position {p.symbol} has non-integral qty {p.qty}"
            raise ValueError(msg)
        if qty == 0:
            continue
        short = p.side == "short" or p.qty < 0
        legs.append(
            BrokerLeg(
                occ_symbol=parse_occ(p.symbol).format(),
                qty=-int(qty) if short else int(qty),
                avg_entry_price=abs(p.avg_entry_price) if p.avg_entry_price is not None else None,
            )
        )
    return legs, other


def holdings_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[StructureHolding]:
    """``open_structures`` rows (status open) -> what each should hold at the broker."""
    out: list[StructureHolding] = []
    for row in rows:
        st = Structure.model_validate_json(row["structure_json"])
        n = int(row["contracts"])
        legs: dict[str, int] = defaultdict(int)
        premiums: dict[str, Decimal | None] = {}
        for leg in st.legs:
            sym = parse_occ(leg.occ_symbol).format()
            sign = 1 if leg.side == LegIntent.LONG else -1
            legs[sym] += sign * leg.ratio * n
            premiums[sym] = leg.premium
        out.append(
            StructureHolding(
                structure_id=str(row["id"]),
                ticker=str(row["ticker"]),
                legs=dict(legs),
                premiums=premiums,
            )
        )
    return out


def attribute(holdings: Sequence[StructureHolding], legs: Sequence[BrokerLeg]) -> Attribution:
    """Match broker legs to structures, oldest structure first (see module docstring)."""
    left: dict[str, int] = defaultdict(int)
    for leg in legs:
        left[leg.occ_symbol] += leg.qty
    out = Attribution()
    for h in holdings:
        fits = all(
            (need > 0 and left.get(sym, 0) >= need) or (need < 0 and left.get(sym, 0) <= need)
            for sym, need in h.legs.items()
            if need != 0
        )
        if fits:
            for sym, need in h.legs.items():
                left[sym] -= need
            out.attributed.append(h)
        else:
            out.unmatched.append(h)
    out.unattributed = {sym: q for sym, q in sorted(left.items()) if q != 0}
    return out
