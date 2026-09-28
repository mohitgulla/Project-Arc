"""Builders for the Phase-1 structure whitelist (PLAN D4).

Each builder takes strikes and per-share premiums (e.g. quote mids), validates
strike geometry and premium sign, and returns a fully analysed
:class:`arc.models.Structure` via :func:`arc.structures.analytics.analyze`.

Premiums are always given as non-negative per-share prices; the builder
assigns the long/short side.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — kept at runtime for readability of signatures
from decimal import Decimal

from arc.models import Leg, LegIntent, Structure, StructureKind
from arc.pricing.bs import OptionKind
from arc.structures.analytics import MarketInputs, analyze
from arc.structures.occ import format_occ

__all__ = [
    "credit_vertical",
    "debit_vertical",
    "iron_condor",
    "long_call",
    "long_put",
]

Num = Decimal | float | str | int


def _d(x: Num) -> Decimal:
    return Decimal(str(x))


def _leg(
    root: str,
    expiration: dt.date,
    kind: OptionKind,
    strike: Num,
    side: LegIntent,
    premium: Num,
    intent: str,
) -> Leg:
    return Leg(
        occ_symbol=format_occ(root, expiration, kind, _d(strike)),
        side=side,
        ratio=1,
        intent=intent,
        premium=_d(premium),
    )


def _single(
    kind: OptionKind,
    root: str,
    expiration: dt.date,
    strike: Num,
    premium: Num,
    as_of: dt.date | None,
    market: MarketInputs | None,
) -> Structure:
    if _d(premium) <= 0:
        msg = "long option premium must be > 0"
        raise ValueError(msg)
    leg = _leg(root, expiration, kind, strike, LegIntent.LONG, premium, f"long {kind.name.lower()}")
    return analyze([leg], as_of=as_of, market=market)


def long_call(
    root: str,
    expiration: dt.date,
    strike: Num,
    premium: Num,
    *,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Single long call (debit)."""
    return _single(OptionKind.CALL, root, expiration, strike, premium, as_of, market)


def long_put(
    root: str,
    expiration: dt.date,
    strike: Num,
    premium: Num,
    *,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Single long put (debit)."""
    return _single(OptionKind.PUT, root, expiration, strike, premium, as_of, market)


def _vertical(
    want: StructureKind,
    kind: OptionKind | str,
    root: str,
    expiration: dt.date,
    long_strike: Num,
    long_premium: Num,
    short_strike: Num,
    short_premium: Num,
    as_of: dt.date | None,
    market: MarketInputs | None,
) -> Structure:
    k = OptionKind(str(kind).lower()[0])
    name = k.name.lower()
    legs = [
        _leg(root, expiration, k, long_strike, LegIntent.LONG, long_premium, f"long {name}"),
        _leg(root, expiration, k, short_strike, LegIntent.SHORT, short_premium, f"short {name}"),
    ]
    s = analyze(legs, as_of=as_of, market=market)
    if s.kind != want:
        msg = (
            f"strikes long={long_strike} short={short_strike} make a {s.kind} {name} "
            f"spread, not {want}"
        )
        raise ValueError(msg)
    if want == StructureKind.VERTICAL_DEBIT and s.net_debit_credit <= 0:
        msg = f"debit vertical priced at a non-debit {s.net_debit_credit}; check quotes"
        raise ValueError(msg)
    if want == StructureKind.VERTICAL_CREDIT and s.net_debit_credit >= 0:
        msg = f"credit vertical priced at a non-credit {s.net_debit_credit}; check quotes"
        raise ValueError(msg)
    return s


def debit_vertical(
    kind: OptionKind | str,
    root: str,
    expiration: dt.date,
    *,
    long_strike: Num,
    long_premium: Num,
    short_strike: Num,
    short_premium: Num,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Debit vertical: bull call (long K < short K) or bear put (long K > short K)."""
    return _vertical(
        StructureKind.VERTICAL_DEBIT,
        kind,
        root,
        expiration,
        long_strike,
        long_premium,
        short_strike,
        short_premium,
        as_of,
        market,
    )


def credit_vertical(
    kind: OptionKind | str,
    root: str,
    expiration: dt.date,
    *,
    short_strike: Num,
    short_premium: Num,
    long_strike: Num,
    long_premium: Num,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Credit vertical: bull put (short K > long K) or bear call (short K < long K)."""
    return _vertical(
        StructureKind.VERTICAL_CREDIT,
        kind,
        root,
        expiration,
        long_strike,
        long_premium,
        short_strike,
        short_premium,
        as_of,
        market,
    )


def iron_condor(
    root: str,
    expiration: dt.date,
    *,
    long_put_strike: Num,
    long_put_premium: Num,
    short_put_strike: Num,
    short_put_premium: Num,
    short_call_strike: Num,
    short_call_premium: Num,
    long_call_strike: Num,
    long_call_premium: Num,
    as_of: dt.date | None = None,
    market: MarketInputs | None = None,
) -> Structure:
    """Short iron condor: long put < short put < short call < long call, net credit."""
    ks = [_d(long_put_strike), _d(short_put_strike), _d(short_call_strike), _d(long_call_strike)]
    if not ks[0] < ks[1] < ks[2] < ks[3]:
        msg = f"iron condor strikes must be strictly ascending lp<sp<sc<lc, got {ks}"
        raise ValueError(msg)
    P, C = OptionKind.PUT, OptionKind.CALL
    L, S = LegIntent.LONG, LegIntent.SHORT
    legs = [
        _leg(root, expiration, P, ks[0], L, long_put_premium, "long put wing"),
        _leg(root, expiration, P, ks[1], S, short_put_premium, "short put"),
        _leg(root, expiration, C, ks[2], S, short_call_premium, "short call"),
        _leg(root, expiration, C, ks[3], L, long_call_premium, "long call wing"),
    ]
    s = analyze(legs, as_of=as_of, market=market)
    if s.net_debit_credit >= 0:
        msg = f"iron condor priced at a non-credit {s.net_debit_credit}; check quotes"
        raise ValueError(msg)
    return s
