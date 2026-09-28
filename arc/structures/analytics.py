"""Structure analytics: payoff at expiry, max gain/loss, breakevens, net Greeks,
defined-risk assertion, and buying-power estimate.

All functions are deterministic and side-effect free.

Conventions
-----------
- A list of :class:`arc.models.Leg` describes **one unit** of a structure; a
  leg's ``ratio`` is its quantity per unit (the mleg ``ratio_qty``).
- Every leg must carry ``premium`` (per-share price, e.g. the quote mid).
- Prices / strikes / premiums are per share. P&L, max gain, max loss and
  buying power are **dollars per unit** (x :data:`CONTRACT_MULTIPLIER`).
- Net Greeks are position Greeks in share-equivalents: per-share BSM Greek
  x 100 x signed ratio, summed. Theta is $/calendar day; vega is $ per 1.00
  change in σ (see :mod:`arc.pricing.bs`).
- Payoff analytics assume a **single root and a single expiration** (true for
  every Phase-1 structure in PLAN D4); mixed roots/expirations raise
  :class:`ValueError`.
- ``None`` for max gain / max loss means unbounded.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — kept at runtime for readability of signatures
from collections.abc import Sequence  # noqa: TC003
from decimal import Decimal
from typing import NamedTuple

from pydantic import BaseModel, Field

from arc.models import Greeks, Leg, LegIntent, Structure, StructureKind
from arc.pricing.bs import BSMInputs, OptionKind, greeks
from arc.structures.occ import OccSymbol, parse_occ
from arc.utils.calendar import dte_calendar, now_et

__all__ = [
    "CONTRACT_MULTIPLIER",
    "MarketInputs",
    "UndefinedRiskError",
    "analyze",
    "assert_defined_risk",
    "breakevens",
    "buying_power",
    "classify",
    "is_defined_risk",
    "max_gain_loss",
    "net_debit_credit",
    "net_greeks",
    "payoff_at",
    "payoff_grid",
    "strike_grid",
]

CONTRACT_MULTIPLIER = Decimal(100)
_BE_QUANT = Decimal("0.0001")
_MAX_GRID_POINTS = 5000


class UndefinedRiskError(ValueError):
    """Raised when a structure's risk is not defined (uncovered short option)."""


class MarketInputs(BaseModel):
    """Market state needed for net Greeks."""

    spot: float = Field(..., gt=0, description="Underlying price")
    r: float = Field(..., description="Risk-free rate (annualised, continuous)")
    q: float = Field(0.0, description="Continuous dividend yield")
    ivs: dict[str, float] = Field(
        ..., description="Implied vol per leg, keyed by the leg's occ_symbol exactly"
    )


# ---------------------------------------------------------------------------
# Leg resolution
# ---------------------------------------------------------------------------


class _RLeg(NamedTuple):
    occ: OccSymbol
    sign: int  # +1 long, -1 short
    ratio: int
    premium: Decimal


def _resolve(legs: Sequence[Leg]) -> list[_RLeg]:
    if not legs:
        msg = "structure has no legs"
        raise ValueError(msg)
    out: list[_RLeg] = []
    for leg in legs:
        if leg.premium is None:
            msg = f"leg {leg.occ_symbol} has no premium; analytics need a per-share price"
            raise ValueError(msg)
        sign = 1 if leg.side == LegIntent.LONG else -1
        out.append(_RLeg(parse_occ(leg.occ_symbol), sign, leg.ratio, leg.premium))
    roots = {r.occ.root for r in out}
    exps = {r.occ.expiration for r in out}
    if len(roots) != 1 or len(exps) != 1:
        msg = (
            f"structure must have a single root and expiration; got roots={sorted(roots)} "
            f"expirations={sorted(exps)}"
        )
        raise ValueError(msg)
    return out


def _intrinsic(occ: OccSymbol, spot: Decimal) -> Decimal:
    if occ.kind == OptionKind.CALL:
        return max(spot - occ.strike, Decimal(0))
    return max(occ.strike - spot, Decimal(0))


def _payoff(rlegs: Sequence[_RLeg], spot: Decimal) -> Decimal:
    total = Decimal(0)
    for r in rlegs:
        total += r.sign * r.ratio * (_intrinsic(r.occ, spot) - r.premium)
    return total * CONTRACT_MULTIPLIER


def _upside_slope(rlegs: Sequence[_RLeg]) -> Decimal:
    """d(P&L)/dS for S above every strike ($ per $1 of underlying)."""
    net_calls = sum(r.sign * r.ratio for r in rlegs if r.occ.kind == OptionKind.CALL)
    return Decimal(net_calls) * CONTRACT_MULTIPLIER


def _kinks(rlegs: Sequence[_RLeg]) -> list[Decimal]:
    """S=0 plus every distinct strike, ascending (payoff is linear between them)."""
    return [Decimal(0), *sorted({r.occ.strike for r in rlegs})]


# ---------------------------------------------------------------------------
# Payoff
# ---------------------------------------------------------------------------


def net_debit_credit(legs: Sequence[Leg]) -> Decimal:
    """Per-share net price of one unit: positive = debit paid, negative = credit."""
    return sum((r.sign * r.ratio * r.premium for r in _resolve(legs)), Decimal(0))


def payoff_at(legs: Sequence[Leg], spot: Decimal | float | str) -> Decimal:
    """P&L at expiry in dollars per unit for an underlying price *spot* (≥ 0)."""
    s = Decimal(str(spot))
    if s < 0:
        msg = "spot must be >= 0"
        raise ValueError(msg)
    return _payoff(_resolve(legs), s)


def strike_grid(
    legs: Sequence[Leg],
    *,
    pad: Decimal | float | str = "0.2",
    step: Decimal | float | str | None = None,
) -> list[Decimal]:
    """Underlying-price grid spanning the strikes ± *pad* (fraction), ascending.

    Default *step* is the smallest gap between distinct strikes (or 1% of the
    strike, min $0.01, for a single-strike structure). Every strike is always
    included so the grid hits every payoff kink.
    """
    rlegs = _resolve(legs)
    strikes = sorted({r.occ.strike for r in rlegs})
    pad_d = Decimal(str(pad))
    if pad_d < 0:
        msg = "pad must be >= 0"
        raise ValueError(msg)
    if step is None:
        gaps = [b - a for a, b in zip(strikes, strikes[1:], strict=False)]
        step_d = min(gaps) if gaps else max(strikes[0] / 100, Decimal("0.01"))
    else:
        step_d = Decimal(str(step))
    if step_d <= 0:
        msg = "step must be > 0"
        raise ValueError(msg)
    lo = max(Decimal(0), strikes[0] * (1 - pad_d))
    hi = strikes[-1] * (1 + pad_d)
    n = int((hi - lo) / step_d) + 1
    if n > _MAX_GRID_POINTS:
        msg = f"grid would have {n} points (> {_MAX_GRID_POINTS}); increase step"
        raise ValueError(msg)
    pts = {lo + i * step_d for i in range(n)} | set(strikes) | {hi}
    return sorted(pts)


def payoff_grid(
    legs: Sequence[Leg],
    grid: Sequence[Decimal] | None = None,
) -> list[tuple[Decimal, Decimal]]:
    """``[(spot, pnl_dollars_per_unit), ...]`` at expiry over *grid*.

    *grid* defaults to :func:`strike_grid`.
    """
    rlegs = _resolve(legs)
    pts = strike_grid(legs) if grid is None else [Decimal(str(g)) for g in grid]
    return [(s, _payoff(rlegs, s)) for s in pts]


# ---------------------------------------------------------------------------
# Max gain / loss, breakevens
# ---------------------------------------------------------------------------


def max_gain_loss(legs: Sequence[Leg]) -> tuple[Decimal | None, Decimal | None]:
    """``(max_gain, max_loss)`` in dollars per unit; ``None`` = unbounded.

    ``max_loss`` is reported as a positive number for a loss. Exact: the
    expiry payoff is piecewise-linear with kinks only at strikes, so the
    extremes on [0, ∞) lie at S=0, a strike, or S→∞.
    """
    rlegs = _resolve(legs)
    values = [_payoff(rlegs, k) for k in _kinks(rlegs)]
    slope = _upside_slope(rlegs)
    max_gain = None if slope > 0 else max(values)
    max_loss = None if slope < 0 else -min(values)
    return max_gain, max_loss


def breakevens(legs: Sequence[Leg]) -> list[Decimal]:
    """Underlying prices at expiry where P&L = 0, ascending (4 dp)."""
    rlegs = _resolve(legs)
    kinks = _kinks(rlegs)
    values = [_payoff(rlegs, k) for k in kinks]
    found: set[Decimal] = set()
    for (a, va), (b, vb) in zip(
        zip(kinks, values, strict=True), zip(kinks[1:], values[1:], strict=True), strict=False
    ):
        if va == 0:
            found.add(a)
        if va * vb < 0:
            found.add(a + (-va) * (b - a) / (vb - va))
    last_k, last_v = kinks[-1], values[-1]
    if last_v == 0:
        found.add(last_k)
    slope = _upside_slope(rlegs)
    if slope != 0 and last_v * slope < 0:
        found.add(last_k - last_v / slope)
    return sorted({x.quantize(_BE_QUANT) for x in found})


# ---------------------------------------------------------------------------
# Defined risk & buying power
# ---------------------------------------------------------------------------


def _uncovered(rlegs: Sequence[_RLeg]) -> list[str]:
    problems: list[str] = []
    for kind in (OptionKind.CALL, OptionKind.PUT):
        longs = sum(r.ratio for r in rlegs if r.occ.kind == kind and r.sign > 0)
        shorts = sum(r.ratio for r in rlegs if r.occ.kind == kind and r.sign < 0)
        if shorts > longs:
            problems.append(f"{shorts - longs} uncovered short {kind.name.lower()}(s)")
    return problems


def is_defined_risk(legs: Sequence[Leg]) -> bool:
    """True iff every short option is covered by a long of the same type and
    expiry, and max loss is bounded."""
    rlegs = _resolve(legs)
    return not _uncovered(rlegs) and max_gain_loss(legs)[1] is not None


def assert_defined_risk(legs_or_structure: Sequence[Leg] | Structure) -> None:
    """Raise :class:`UndefinedRiskError` unless the structure is defined-risk."""
    legs = legs_or_structure.legs if isinstance(legs_or_structure, Structure) else legs_or_structure
    rlegs = _resolve(legs)
    problems = _uncovered(rlegs)
    if max_gain_loss(legs)[1] is None:
        problems.append("unbounded max loss")
    if problems:
        msg = "structure is not defined-risk: " + "; ".join(problems)
        raise UndefinedRiskError(msg)


def buying_power(legs: Sequence[Leg]) -> Decimal | None:
    """Estimated buying-power reduction per unit (Reg-T style), dollars.

    For defined-risk structures this is the max loss: debit paid for long
    options / debit spreads; spread width x 100 - credit for credit spreads;
    widest side x 100 - credit for iron condors. Returns ``None`` for
    undefined-risk structures (naked margin is out of Phase-1 scope, PLAN D4).
    """
    if not is_defined_risk(legs):
        return None
    max_loss = max_gain_loss(legs)[1]
    assert max_loss is not None  # guaranteed by is_defined_risk
    return max(max_loss, Decimal(0))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _vertical_kind(a: _RLeg, b: _RLeg) -> StructureKind | None:
    if a.occ.kind != b.occ.kind or a.sign == b.sign or a.ratio != b.ratio:
        return None
    if a.occ.strike == b.occ.strike:
        return None
    long_, short = (a, b) if a.sign > 0 else (b, a)
    if long_.occ.kind == OptionKind.CALL:
        debit = long_.occ.strike < short.occ.strike
    else:
        debit = long_.occ.strike > short.occ.strike
    return StructureKind.VERTICAL_DEBIT if debit else StructureKind.VERTICAL_CREDIT


def classify(legs: Sequence[Leg]) -> StructureKind:
    """Classify legs by strike geometry (not by quoted premiums)."""
    rlegs = _resolve(legs)
    if len(rlegs) == 1:
        (r,) = rlegs
        if r.sign > 0:
            is_call = r.occ.kind == OptionKind.CALL
            return StructureKind.LONG_CALL if is_call else StructureKind.LONG_PUT
        return StructureKind.OTHER
    if len(rlegs) == 2:
        return _vertical_kind(rlegs[0], rlegs[1]) or StructureKind.OTHER
    if len(rlegs) == 4:
        puts = [r for r in rlegs if r.occ.kind == OptionKind.PUT]
        calls = [r for r in rlegs if r.occ.kind == OptionKind.CALL]
        if len(puts) == 2 and len({r.ratio for r in rlegs}) == 1:
            pk = _vertical_kind(*puts)
            ck = _vertical_kind(*calls)
            if pk == ck == StructureKind.VERTICAL_CREDIT:
                short_put = next(r for r in puts if r.sign < 0)
                short_call = next(r for r in calls if r.sign < 0)
                if short_put.occ.strike < short_call.occ.strike:
                    return StructureKind.IRON_CONDOR
    return StructureKind.OTHER


# ---------------------------------------------------------------------------
# Net Greeks
# ---------------------------------------------------------------------------


def net_greeks(legs: Sequence[Leg], market: MarketInputs, dte: int) -> Greeks:
    """Position Greeks per unit in share-equivalents (BSM, European).

    *dte* is calendar days to expiry; must be ≥ 1 (t = dte / 365).
    """
    if dte < 1:
        msg = "net Greeks require dte >= 1"
        raise ValueError(msg)
    t = dte / 365.0
    totals = dict.fromkeys(Greeks.model_fields, 0.0)
    mult = float(CONTRACT_MULTIPLIER)
    for leg, r in zip(legs, _resolve(legs), strict=True):
        iv = market.ivs.get(leg.occ_symbol)
        if iv is None:
            msg = f"no implied vol for leg {leg.occ_symbol}"
            raise ValueError(msg)
        g = greeks(
            BSMInputs(
                S=market.spot,
                K=float(r.occ.strike),
                t=t,
                r=market.r,
                q=market.q,
                sigma=iv,
                flag=r.occ.kind,
            )
        )
        w = r.sign * r.ratio * mult
        for name in totals:
            totals[name] += w * getattr(g, name)
    return Greeks(**totals)


# ---------------------------------------------------------------------------
# All-in-one
# ---------------------------------------------------------------------------


def analyze(
    legs: Sequence[Leg],
    *,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Build a fully-populated :class:`arc.models.Structure` from priced legs.

    *as_of* defaults to today (ET). Greeks are computed only when *market*
    is supplied; otherwise they are left at zero.
    """
    rlegs = _resolve(legs)
    today = as_of if as_of is not None else now_et().date()
    dte = dte_calendar(today, rlegs[0].occ.expiration)
    if dte < 0:
        msg = f"expiration {rlegs[0].occ.expiration} is before as_of {today}"
        raise ValueError(msg)
    max_gain, max_loss = max_gain_loss(legs)
    return Structure(
        legs=list(legs),
        kind=classify(legs),
        net_debit_credit=net_debit_credit(legs),
        max_gain=max_gain,
        max_loss=max_loss,
        breakevens=breakevens(legs),
        greeks=net_greeks(legs, market, dte) if market is not None else Greeks(),
        dte=dte,
        buying_power=buying_power(legs),
    )
