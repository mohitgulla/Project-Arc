"""Deterministic outcome attribution (E7.4). Pure functions: no I/O, no LLM.

Price convention (same as :class:`~arc.models.Structure` and the mleg limit):
per-share net, **positive = debit, negative = credit**. ``exit_fill`` / marks
use the same sign convention for the value of the open position, so

    P&L = (exit − entry) × 100 × contracts

works for debits and credits alike: a credit IC opened at −1.66 and bought
back at −0.40 made (−0.40 − −1.66) × 100 = +$126 per contract.

Slippage is ``fill − limit`` per share: positive is always worse for us (paid
more on a debit, received less on a credit). It is also expressed in bps of
capital at risk (max loss), the unit of the Quant's ``cost_bps``.

``calibration`` buckets stated probabilities (PoP / confidence) against
realised hit rates, per persona. ``gaps`` in :mod:`arc.journal.cli` feeds it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from arc.journal.models import OutcomeRecord, OutcomeStatus
from arc.models import LegIntent
from arc.pricing.bs import OptionKind
from arc.structures import parse_occ

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Iterable, Sequence

    from arc.models import Leg, Proposal

__all__ = [
    "CalibrationBucket",
    "MULTIPLIER",
    "attribute",
    "calibration",
    "expiry_value",
    "max_adverse_excursion",
    "realised_pnl",
    "slippage",
]

MULTIPLIER = Decimal(100)
_BPS = Decimal(10_000)


def slippage(
    *,
    limit: Decimal,
    fill: Decimal,
    contracts: int,
    max_loss_per_contract: Decimal | None,
) -> tuple[Decimal, float | None]:
    """``(slippage_usd, slippage_bps)``; positive = worse than the limit.

    ``bps`` is relative to capital at risk (``max_loss_per_contract × contracts``)
    and is ``None`` when that is unbounded or zero.
    """
    usd = (fill - limit) * MULTIPLIER * contracts
    if max_loss_per_contract is None or max_loss_per_contract <= 0 or contracts <= 0:
        return usd, None
    return usd, float(usd / (max_loss_per_contract * contracts) * _BPS)


def realised_pnl(*, entry: Decimal, exit_: Decimal, contracts: int) -> Decimal:
    """Dollar P&L of a position opened at *entry* and closed at *exit_* (per-share nets)."""
    return (exit_ - entry) * MULTIPLIER * contracts


def max_adverse_excursion(*, entry: Decimal, marks: Iterable[Decimal], contracts: int) -> Decimal:
    """Worst open P&L over *marks* (≤ 0; 0 when the position never went against us)."""
    worst = Decimal(0)
    for m in marks:
        worst = min(worst, realised_pnl(entry=entry, exit_=m, contracts=contracts))
    return worst


def expiry_value(legs: Sequence[Leg], settlement: Decimal) -> Decimal:
    """Per-share net value of *legs* at expiry for an underlying settlement price.

    Long legs are worth +intrinsic, short legs −intrinsic, so the result is in
    the entry sign convention (a credit spread is worth ≤ 0 to its holder).
    """
    total = Decimal(0)
    for leg in legs:
        occ = parse_occ(leg.occ_symbol)
        if occ.kind is OptionKind.CALL:
            intrinsic = max(settlement - occ.strike, Decimal(0))
        else:
            intrinsic = max(occ.strike - settlement, Decimal(0))
        sign = 1 if leg.side is LegIntent.LONG else -1
        total += sign * leg.ratio * intrinsic
    return total


def attribute(
    proposal: Proposal,
    *,
    proposal_hash: str,
    at: _dt.datetime,
    traded: bool,
    entry_fill: Decimal | None = None,
    exit_fill: Decimal | None = None,
    marks: Sequence[Decimal] = (),
    opened_at: _dt.date | None = None,
    closed_at: _dt.date | None = None,
    exit_reason: str | None = None,
    settlement: Decimal | None = None,
    expired: bool = False,
) -> OutcomeRecord:
    """Build the :class:`OutcomeRecord` for *proposal* from fills and marks.

    - ``traded=False`` → ``not_traded`` (rejected, expired card, gate FAIL).
    - traded without an entry fill → ``never_filled``.
    - entry fill without exit → ``open`` (MAE from marks so far).
    - ``expired=True`` with no exit fill → closed at expiry value (needs
      *settlement*); worthless when that value is 0.
    - ``settlement`` also gives the D19 hold-to-expiry shadow P&L for any
      position closed early.
    """
    n = proposal.sizing.contracts
    limit = (
        proposal.limit_price
        if proposal.limit_price is not None
        else proposal.structure.net_debit_credit
    )
    base = {
        "proposal_hash": proposal_hash,
        "contracts": n,
        "limit_price": limit,
        "cost_bps": proposal.quant.cost_bps,
        "at": at,
    }
    if not traded:
        return OutcomeRecord(status=OutcomeStatus.NOT_TRADED, **base)
    if entry_fill is None:
        return OutcomeRecord(status=OutcomeStatus.NEVER_FILLED, **base)

    slip_usd, slip_bps = slippage(
        limit=limit,
        fill=entry_fill,
        contracts=n,
        max_loss_per_contract=proposal.structure.max_loss,
    )
    shadow_value = (
        expiry_value(proposal.structure.legs, settlement) if settlement is not None else None
    )
    shadow = (
        realised_pnl(entry=entry_fill, exit_=shadow_value, contracts=n)
        if shadow_value is not None
        else None
    )
    if exit_fill is None and expired and shadow_value is not None:
        exit_fill = shadow_value
        exit_reason = exit_reason or "expiry"
    mae = max_adverse_excursion(
        entry=entry_fill,
        marks=[*marks, *([exit_fill] if exit_fill is not None else [])],
        contracts=n,
    )
    days = (closed_at - opened_at).days if opened_at and closed_at else None
    common = {
        **base,
        "entry_fill": entry_fill,
        "slippage_usd": slip_usd,
        "slippage_bps": slip_bps,
        "max_adverse_excursion": mae,
        "hold_to_expiry_shadow_pnl": shadow,
    }
    if exit_fill is None:
        return OutcomeRecord(status=OutcomeStatus.OPEN, **common)
    pnl = realised_pnl(entry=entry_fill, exit_=exit_fill, contracts=n)
    ev_total = proposal.quant.ev * n
    status = OutcomeStatus.EXPIRED_WORTHLESS if expired and exit_fill == 0 else OutcomeStatus.CLOSED
    return OutcomeRecord(
        status=status,
        exit_fill=exit_fill,
        realised_pnl=pnl,
        days_held=days if days is None else max(days, 0),
        exit_reason=exit_reason,
        ev_total=ev_total,
        pnl_vs_ev=pnl - ev_total,
        **common,
    )


@dataclass(frozen=True)
class CalibrationBucket:
    persona: str
    lo: float
    hi: float
    n: int
    stated_mean: float
    hit_rate: float

    @property
    def gap(self) -> float:
        """Realised minus stated: negative = over-confident."""
        return self.hit_rate - self.stated_mean


def calibration(
    points: Iterable[tuple[str, float, bool]], *, buckets: int = 5
) -> list[CalibrationBucket]:
    """Bucket ``(persona, stated probability, hit)`` points into equal-width bins per persona."""
    if buckets < 1:
        msg = "buckets must be >= 1"
        raise ValueError(msg)
    grouped: dict[tuple[str, int], list[tuple[float, bool]]] = defaultdict(list)
    for persona, p, hit in points:
        if not 0.0 <= p <= 1.0:
            msg = f"stated probability {p} is outside [0, 1]"
            raise ValueError(msg)
        idx = min(int(p * buckets), buckets - 1)
        grouped[(persona, idx)].append((p, hit))
    out = []
    for (persona, idx), items in sorted(grouped.items()):
        n = len(items)
        out.append(
            CalibrationBucket(
                persona=persona,
                lo=idx / buckets,
                hi=(idx + 1) / buckets,
                n=n,
                stated_mean=sum(p for p, _ in items) / n,
                hit_rate=sum(1 for _, h in items if h) / n,
            )
        )
    return out
