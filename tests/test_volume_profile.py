"""E23.0 (D98): composite volume profile (POC / value area / HVN / LVN), pure."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.features.volume_profile import (
    PRICE_TICK,
    bin_width_for,
    build_profile,
    nearest_above,
    nearest_below,
    rth_bars,
    session_ohlc,
)
from arc.utils.calendar import ET


@dataclass(frozen=True)
class Bar:
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


T0 = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)


def bar(lo: float, hi: float, vol: float, *, at: dt.datetime = T0) -> Bar:
    return Bar(at, lo, hi, lo, (lo + hi) / 2, vol)


def point(price: float, vol: float) -> Bar:
    """A bar whose whole range sits inside one $1 bin (``[p, p + 0.5]``)."""
    return bar(price, price + 0.5, vol)


# ---------------------------------------------------------------------------
# POC / value area on a known distribution
# ---------------------------------------------------------------------------


def test_known_distribution_poc_and_value_area() -> None:
    # $1 bins 100..104 with volumes 10, 20, 40, 20, 10 (total 100)
    bars = [point(100 + i, v) for i, v in enumerate([10, 20, 40, 20, 10])]
    p = build_profile(bars, bin_width=1.0, value_area=0.70, hvn_min_share=0.03)
    assert p is not None
    assert p.bins == 5 and p.total_volume == pytest.approx(100)
    assert p.poc == pytest.approx(102.5)
    # POC 40, then the tie 20/20 adds both sides: 80 % >= 70 %
    assert (p.val, p.vah) == (pytest.approx(101.0), pytest.approx(104.0))
    assert p.va_share == pytest.approx(0.80)
    assert p.hvns == [pytest.approx(102.5)] and p.lvns == []


def test_value_area_grows_toward_the_heavier_side() -> None:
    bars = [point(100 + i, v) for i, v in enumerate([5, 30, 50, 10, 5])]
    p = build_profile(bars, bin_width=1.0, value_area=0.70)
    assert p is not None
    assert p.poc == pytest.approx(102.5)
    # 50 -> +30 below = 80 % (stops; the lighter side above is not added)
    assert (p.val, p.vah) == (pytest.approx(101.0), pytest.approx(103.0))


def test_volume_spread_evenly_over_the_bins_a_bar_touches() -> None:
    p = build_profile([bar(100.0, 103.5, 400)], bin_width=1.0, value_area=0.5)
    assert p is not None and p.bins == 4
    # 100 per bin; four-way tie -> the bin nearest the VW mean price (101.75)
    assert p.poc == pytest.approx(101.5)
    # 100 < 200 target; the 100/100 tie on either side adds both -> 300 (75 %)
    assert (p.val, p.vah) == (pytest.approx(100.0), pytest.approx(103.0))
    assert p.va_share == pytest.approx(0.75)


def test_flat_bar_puts_all_volume_in_one_bin() -> None:
    p = build_profile([bar(50.0, 50.0, 7)], bin_width=0.25)
    assert p is not None and p.bins == 1
    assert p.poc == pytest.approx(50.125)
    assert (p.val, p.vah, p.va_share) == (pytest.approx(50.0), pytest.approx(50.25), 1.0)


# ---------------------------------------------------------------------------
# Bimodal profile: two HVNs with an LVN between
# ---------------------------------------------------------------------------


def test_bimodal_profile_has_two_hvns_and_an_lvn_between() -> None:
    vols = [2, 10, 30, 10, 2, 1, 1, 2, 10, 25, 10, 2]  # nodes at 102 and 109
    bars = [point(100 + i, v) for i, v in enumerate(vols)]
    p = build_profile(bars, bin_width=1.0, value_area=0.70, hvn_min_share=0.03)
    assert p is not None
    assert p.hvns == [pytest.approx(102.5), pytest.approx(109.5)]
    assert len(p.lvns) == 1 and 104.0 < p.lvns[0] < 108.0
    assert p.poc == pytest.approx(102.5)  # the heavier node
    assert nearest_above(p.hvns, 105.0) == pytest.approx(109.5)
    assert nearest_below(p.hvns, 105.0) == pytest.approx(102.5)
    assert nearest_above(p.hvns, 120.0) is None and nearest_below(p.hvns, 90.0) is None


def test_hvn_min_share_drops_small_bumps() -> None:
    vols = [50, 1, 1, 5, 5, 5, 1, 1, 50]  # a small shelf at 103-105 (smoothed share ~4 %)
    bars = [point(100 + i, v) for i, v in enumerate(vols)]
    high = build_profile(bars, bin_width=1.0, hvn_min_share=0.10)
    low = build_profile(bars, bin_width=1.0, hvn_min_share=0.005)
    assert high is not None and low is not None
    assert pytest.approx(104.5) not in high.hvns
    assert pytest.approx(104.5) in low.hvns
    assert high.lvns and not low.lvns  # an LVN is a dip below the same share


def test_poc_tie_goes_to_the_bin_nearest_the_volume_weighted_mean() -> None:
    bars = [point(100, 30), point(101, 5), point(104, 30), point(105, 40)]
    p = build_profile(bars, bin_width=1.0)
    assert p is not None and p.poc == pytest.approx(105.5)  # unique max
    # VW mean (100.25*30 + 104.25*30 + 105.25*10) / 70 = 102.68: 104.5 is nearer than 100.5
    tie = build_profile([point(100, 30), point(104, 30), point(105, 10)], bin_width=1.0)
    assert tie is not None and tie.poc == pytest.approx(104.5)
    # VW mean 102.3 with a 1-lot at 105: 100.5 is nearer
    other = build_profile([point(100, 30), point(104, 30), point(105, 1)], bin_width=1.0)
    assert other is not None and other.poc == pytest.approx(100.5)


# ---------------------------------------------------------------------------
# Bin width floor + empty input
# ---------------------------------------------------------------------------


def test_bin_width_floor() -> None:
    assert bin_width_for(3.0, 0.1) == pytest.approx(0.3)
    assert bin_width_for(0.05, 0.1) == PRICE_TICK  # 0.005 -> the one-cent floor
    assert bin_width_for(None, 0.1) == PRICE_TICK
    assert bin_width_for(0.0, 0.1) == PRICE_TICK
    assert bin_width_for(float("nan"), 0.1) == PRICE_TICK


@pytest.mark.parametrize(
    "bars",
    [
        [],
        [bar(100, 101, 0)],
        [bar(100, 101, 0), bar(101, 102, 0)],
        [Bar(T0, 0, 0, 0, 0, 10)],  # no positive price
    ],
)
def test_empty_or_zero_volume_is_none(bars: list[Bar]) -> None:
    assert build_profile(bars, bin_width=0.5) is None


@pytest.mark.parametrize("width", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_bin_width_is_none(width: float) -> None:
    assert build_profile([bar(100, 101, 10)], bin_width=width) is None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def test_rth_bars_keep_regular_hours_of_the_given_sessions() -> None:
    d1, d2 = dt.date(2026, 10, 8), dt.date(2026, 10, 9)

    def at(d: dt.date, h: int, m: int) -> dt.datetime:
        return dt.datetime.combine(d, dt.time(h, m), ET)

    bars = [
        bar(1, 2, 1, at=at(d2, 9, 15)),  # pre-market
        bar(1, 2, 1, at=at(d2, 15, 45)),
        bar(1, 2, 1, at=at(d1, 9, 30)),
        bar(1, 2, 1, at=at(d2, 16, 0)),  # after hours
        bar(1, 2, 1, at=at(dt.date(2026, 10, 7), 10, 0)),  # outside the window
    ]
    kept = rth_bars(bars, [d1, d2])
    assert [b.timestamp for b in kept] == [at(d1, 9, 30), at(d2, 15, 45)]


def test_session_ohlc_aggregates_per_day() -> None:
    d = dt.date(2026, 10, 9)
    bars = [
        Bar(dt.datetime.combine(d, dt.time(9, 45), ET), 10, 12, 9, 11, 5),
        Bar(dt.datetime.combine(d, dt.time(9, 30), ET), 9.5, 10.5, 9.2, 10, 5),
        Bar(dt.datetime.combine(d, dt.time(10, 0), ET), 11, 13, 10.5, 12.5, 5),
    ]
    (s,) = session_ohlc(bars)
    assert (s.day, s.open, s.high, s.low, s.close) == (d, 9.5, 13, 9.0, 12.5)


# ---------------------------------------------------------------------------
# Property: VAL <= POC <= VAH and the value area holds >= value_area of the volume
# ---------------------------------------------------------------------------

_bar = st.tuples(
    st.floats(1.0, 500.0, allow_nan=False),
    st.floats(0.0, 20.0, allow_nan=False),
    st.integers(0, 10_000_000).map(float),  # share counts (no subnormal volumes)
)


@settings(max_examples=300, deadline=None)
@given(
    rows=st.lists(_bar, min_size=1, max_size=40),
    width=st.floats(0.01, 5.0, allow_nan=False),
    va=st.floats(0.5, 0.95),
)
def test_property_value_area_brackets_poc_and_holds_its_share(
    rows: list[tuple[float, float, float]], width: float, va: float
) -> None:
    bars = [bar(lo, lo + span, vol) for lo, span, vol in rows]
    p = build_profile(bars, bin_width=width, value_area=va)
    if sum(v for _, _, v in rows) <= 0:
        assert p is None
        return
    assert p is not None
    assert p.val <= p.poc <= p.vah
    assert p.va_share >= va - 1e-9
    assert all(p.val - 1e-6 <= x for x in [p.poc])
    assert all(a < b for a, b in zip(p.hvns, p.hvns[1:], strict=False))
