"""Unit tests (no network) for the single-expiry vertical leg selector."""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from arc.data.base import OptionContract, OptionGreeks
from tests.vertical_legs import select_bull_call_vertical

_OCT30 = dt.date(2026, 10, 30)
_NOV06 = dt.date(2026, 11, 6)


def _occ(expiry: dt.date, kind: str, strike: float) -> str:
    return f"SPY{expiry:%y%m%d}{kind[0].upper()}{int(round(strike * 1000)):08d}"


def _c(
    expiry: dt.date,
    strike: float,
    *,
    kind: str = "call",
    bid: float | None = 1.0,
    delta: float | None = 0.5,
) -> OptionContract:
    return OptionContract(
        symbol=_occ(expiry, kind, strike),
        underlying="SPY",
        expiration=expiry,
        strike=strike,
        option_type=kind,
        bid=bid,
        greeks=None if delta is None else OptionGreeks(delta=delta),
    )


def test_multi_expiry_fixture_reproduces_bad_pairing_but_helper_avoids_it() -> None:
    # Same strikes across two expiries. Sorting by strike only (the old logic)
    # puts the 748 Nov06 and 748 Oct30 calls next to each other at the
    # middle: long SPY261106C00748000 / short SPY261030C00748000 → uncovered.
    chain = [
        _c(_NOV06, 747.0),
        _c(_NOV06, 748.0),
        _c(_OCT30, 748.0),
        _c(_OCT30, 749.0),
    ]
    old = sorted(chain, key=lambda c: c.strike)
    mid = (len(old) - 1) // 2
    assert old[mid].expiration != old[mid + 1].expiration  # the old bug

    legs = select_bull_call_vertical(chain)
    assert legs is not None
    long_leg, short_leg = legs
    assert long_leg.expiration == short_leg.expiration == _OCT30  # nearest expiry
    assert (long_leg.strike, short_leg.strike) == (748.0, 749.0)
    assert long_leg.symbol == "SPY261030C00748000"
    assert short_leg.symbol == "SPY261030C00749000"


def test_picks_middle_adjacent_strikes_long_lower() -> None:
    chain = [_c(_OCT30, k) for k in (740.0, 745.0, 750.0, 755.0, 760.0)]
    legs = select_bull_call_vertical(chain)
    assert legs is not None
    assert (legs[0].strike, legs[1].strike) == (750.0, 755.0)


def test_skips_expiry_with_fewer_than_two_usable_calls() -> None:
    chain = [
        _c(_OCT30, 748.0),
        _c(_OCT30, 749.0, bid=0.0),  # unusable: zero bid
        _c(_OCT30, 750.0, delta=None),  # unusable: no greeks
        _c(_OCT30, 751.0, kind="put"),  # not a call
        _c(_OCT30, 752.0, bid=None),  # unusable: no bid
        _c(_NOV06, 748.0),
        _c(_NOV06, 750.0),
    ]
    legs = select_bull_call_vertical(chain)
    assert legs is not None
    assert legs[0].expiration == legs[1].expiration == _NOV06
    assert (legs[0].strike, legs[1].strike) == (748.0, 750.0)


def test_duplicate_strike_in_one_expiry_does_not_pair_with_itself() -> None:
    chain = [_c(_OCT30, 748.0), _c(_OCT30, 748.0)]
    assert select_bull_call_vertical(chain) is None


def test_returns_none_when_no_expiry_qualifies() -> None:
    # One usable call per expiry — the old logic would pair across them.
    chain = [_c(_OCT30, 748.0), _c(_NOV06, 749.0)]
    assert select_bull_call_vertical(chain) is None
    assert select_bull_call_vertical([]) is None


_expiries = st.sampled_from([_OCT30, _NOV06, dt.date(2026, 11, 13)])
_contracts = st.builds(
    _c,
    _expiries,
    st.integers(min_value=700, max_value=720).map(float),
    kind=st.sampled_from(["call", "put"]),
    bid=st.sampled_from([None, 0.0, 0.5]),
    delta=st.sampled_from([None, 0.4]),
)


@given(st.lists(_contracts, max_size=40))
def test_property_never_pairs_across_expirations(chain: list[OptionContract]) -> None:
    legs = select_bull_call_vertical(chain)
    usable = [
        c
        for c in chain
        if c.option_type == "call" and c.bid and c.greeks and c.greeks.delta is not None
    ]
    strikes_by_exp: dict[dt.date, set[float]] = {}
    for c in usable:
        strikes_by_exp.setdefault(c.expiration, set()).add(c.strike)
    qualifying = sorted(e for e, s in strikes_by_exp.items() if len(s) >= 2)

    if not qualifying:
        assert legs is None
        return
    assert legs is not None
    long_leg, short_leg = legs
    assert long_leg.expiration == short_leg.expiration == qualifying[0]
    assert long_leg.strike < short_leg.strike
    assert long_leg.option_type == short_leg.option_type == "call"
    assert long_leg in usable and short_leg in usable
