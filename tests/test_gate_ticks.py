"""D66 (E6.2h): exchange-valid option price increments.

Part of ``make test-gate`` (100% branch coverage on ``arc.gate``).

Grids (Alpaca, options-pricing-increments article):
- single-leg Penny Program (``ppind``): $0.01 below $3, $0.05 at $3+;
- single-leg standard, including unknown ``ppind``: $0.05 below $3, $0.10 at $3+;
- SPY / QQQ / IWM single-leg: $0.01 at any price;
- multi-leg (mleg) net: $0.01 (configurable ``ticks.mleg``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import TickRules
from arc.gate import RuleCode, derive
from arc.gate import rules as R
from arc.gate.band import PriceBand, as_grid, band_from_nbbo
from arc.gate.ticks import (
    TickGrid,
    is_single_leg,
    leg_penny,
    legs_grid,
    max_decimal_places_ok,
    order_grid,
    order_tick,
)
from arc.models import Leg, LegIntent
from arc.structures import debit_vertical, format_occ, long_call
from tests import test_gate as G

RULES = TickRules()
EXP = dt.date(2026, 11, 20)
COIN = format_occ("COIN", EXP, "call", 300)
APP = format_occ("APP", EXP, "call", 500)
SPY = format_occ("SPY", EXP, "call", 700)


def leg(sym: str, ratio: int = 1, penny: bool | None = None) -> Leg:
    return Leg(
        occ_symbol=sym, side=LegIntent.LONG, ratio=ratio, premium=D("1"), penny_program=penny
    )


PENNY = order_grid([leg(COIN)], RULES, {COIN: True})
STANDARD = order_grid([leg(APP)], RULES, {APP: False})
SPY_GRID = order_grid([leg(SPY)], RULES, {})
MLEG = order_grid([leg(COIN), leg(APP)], RULES, {})
GRIDS = [PENNY, STANDARD, SPY_GRID, MLEG, TickGrid.flat(D("0.05"))]


# ---------------------------------------------------------------------------
# Grid selection
# ---------------------------------------------------------------------------


class TestOrderGrid:
    def test_labels_and_increments(self) -> None:
        assert (PENNY.below, PENNY.above, PENNY.boundary) == (D("0.01"), D("0.05"), D(3))
        assert (STANDARD.below, STANDARD.above) == (D("0.05"), D("0.10"))
        assert SPY_GRID.boundary is None and SPY_GRID.below == D("0.01")
        assert MLEG.boundary is None and MLEG.below == D("0.01")
        assert PENNY.label == "single-leg penny" and STANDARD.label == "single-leg standard"
        assert SPY_GRID.label == "single-leg SPY" and MLEG.label == "mleg net"

    @pytest.mark.parametrize("penny", [None, False])
    def test_unknown_or_false_ppind_is_standard(self, penny: bool | None) -> None:
        assert order_grid([leg(COIN)], RULES, {COIN: penny}) == STANDARD.model_copy()
        assert order_grid([leg(COIN)], RULES, {}) == STANDARD

    @pytest.mark.parametrize("root", ["SPY", "QQQ", "IWM"])
    def test_penny_all_underlyings_ignore_ppind(self, root: str) -> None:
        sym = format_occ(root, EXP, "put", 400)
        g = order_grid([leg(sym)], RULES, {sym: False})
        assert all(g.on_grid(D(p)) for p in ("0.01", "2.99", "3.01", "7.07", "33.33"))

    def test_ratio_two_single_contract_is_mleg(self) -> None:
        legs = [leg(COIN, ratio=2)]
        assert not is_single_leg(legs)
        assert order_grid(legs, RULES, {COIN: False}) == MLEG

    def test_legs_grid_reads_leg_flags(self) -> None:
        assert leg_penny([leg(COIN, penny=True)]) == {COIN: True}
        assert legs_grid([leg(COIN, penny=True)], RULES) == PENNY
        assert legs_grid([leg(COIN)], RULES) == STANDARD

    def test_mleg_tick_is_configurable(self) -> None:
        rules = TickRules(mleg=D("0.05"))
        assert order_tick([leg(COIN), leg(APP)], D("1.23"), rules, {}) == D("0.05")
        assert order_tick([leg(COIN), leg(APP)], D("10.97"), RULES, {}) == D("0.01")

    def test_order_tick_by_level(self) -> None:
        assert order_tick([leg(COIN)], D("2.99"), RULES, {COIN: True}) == D("0.01")
        assert order_tick([leg(COIN)], D("3.00"), RULES, {COIN: True}) == D("0.05")
        assert order_tick([leg(COIN)], D("-3.00"), RULES, {COIN: True}) == D("0.05")
        assert order_tick([leg(APP)], D("2.12"), RULES, {}) == D("0.05")
        assert order_tick([leg(APP)], D("4.47"), RULES, {}) == D("0.10")

    def test_describe(self) -> None:
        assert PENNY.describe(D("19.68")) == "single-leg penny ≥$3"
        assert STANDARD.describe(D("-2.12")) == "single-leg standard <$3"
        assert MLEG.describe(D("1")) == "mleg net"


# ---------------------------------------------------------------------------
# Snap: table (card acceptance 2)
# ---------------------------------------------------------------------------


class TestSnapTable:
    @pytest.mark.parametrize(
        ("grid", "price", "up", "down"),
        [
            # card examples
            (PENNY, "19.68", "19.70", "19.65"),  # COIN single-leg
            (STANDARD, "2.12", "2.15", "2.10"),  # APP (ppind=false)
            (STANDARD, "4.47", "4.50", "4.40"),
            (SPY_GRID, "7.07", "7.07", "7.07"),
            (MLEG, "-10.97", "-10.97", "-10.97"),  # AAPL vertical net
            # $3 boundary: penny
            (PENNY, "2.99", "2.99", "2.99"),
            (PENNY, "3.00", "3.00", "3.00"),
            (PENNY, "3.01", "3.05", "3.00"),
            (PENNY, "2.995", "3.00", "2.99"),
            # $3 boundary: standard
            (STANDARD, "2.99", "3.00", "2.95"),
            (STANDARD, "3.00", "3.00", "3.00"),
            (STANDARD, "3.01", "3.10", "3.00"),
            (STANDARD, "2.96", "3.00", "2.95"),
            # $3 boundary: SPY / QQQ / IWM
            (SPY_GRID, "2.99", "2.99", "2.99"),
            (SPY_GRID, "3.00", "3.00", "3.00"),
            (SPY_GRID, "3.01", "3.01", "3.01"),
            # credits (sell-to-close single leg is a negative net)
            (PENNY, "-3.03", "-3.00", "-3.05"),
            (PENNY, "-2.97", "-2.97", "-2.97"),
            (STANDARD, "-3.01", "-3.00", "-3.10"),
            (STANDARD, "-2.97", "-2.95", "-3.00"),
            (STANDARD, "-5.00", "-5.00", "-5.00"),
            (STANDARD, "0", "0", "0"),
            # mid with 4 decimals (price_structure rounds mids to 4 dp)
            (MLEG, "-1.6555", "-1.65", "-1.66"),
            (PENNY, "40.8725", "40.90", "40.85"),
        ],
    )
    def test_snap(self, grid: TickGrid, price: str, up: str, down: str) -> None:
        assert grid.snap(D(price), "up") == D(up)
        assert grid.snap(D(price), "down") == D(down)

    def test_on_grid(self) -> None:
        assert not PENNY.on_grid(D("19.68")) and PENNY.on_grid(D("19.70"))
        assert not PENNY.on_grid(D("40.87"))  # AMD, the audit's example
        assert PENNY.on_grid(D("2.99")) and not STANDARD.on_grid(D("2.99"))


# ---------------------------------------------------------------------------
# Snap: properties (card acceptance 1)
# ---------------------------------------------------------------------------

_prices = st.decimals(min_value=-60, max_value=60, places=4)
_dirs = st.sampled_from(["up", "down"])
_grids = st.sampled_from(GRIDS)


class TestSnapProperties:
    @given(_grids, _prices, _dirs)
    def test_on_grid_at_own_level_and_idempotent(self, g: TickGrid, x: D, d: str) -> None:
        s = g.snap(x, d)  # type: ignore[arg-type]
        assert g.on_grid(s)
        assert max_decimal_places_ok(s)
        assert g.snap(s, "up") == s and g.snap(s, "down") == s

    @given(_grids, _prices, _dirs)
    def test_direction_and_at_most_one_step(self, g: TickGrid, x: D, d: str) -> None:
        s = g.snap(x, d)  # type: ignore[arg-type]
        assert (s >= x) if d == "up" else (s <= x)
        assert abs(s - x) < g.tick_at(s)

    @given(_grids, _prices, _prices, _dirs)
    def test_monotone(self, g: TickGrid, x: D, y: D, d: str) -> None:
        lo, hi = min(x, y), max(x, y)
        assert g.snap(lo, d) <= g.snap(hi, d)  # type: ignore[arg-type]

    @given(_prices, _dirs)
    def test_mleg_always_penny(self, x: D, d: str) -> None:
        s = MLEG.snap(x, d)  # type: ignore[arg-type]
        assert s % D("0.01") == 0 and abs(s - x) < D("0.01")


# ---------------------------------------------------------------------------
# TickGrid / TickRules validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_grid_needs_whole_cents(self) -> None:
        with pytest.raises(ValueError, match="whole cents"):
            TickGrid(below=D("0.005"), above=D("0.05"))

    def test_grid_boundary_on_both_increments(self) -> None:
        with pytest.raises(ValueError, match="boundary"):
            TickGrid(below=D("0.05"), above=D("0.10"), boundary=D("3.05"))

    def test_max_decimal_places(self) -> None:
        assert max_decimal_places_ok(D("1.23")) and max_decimal_places_ok(D("-4"))
        assert not max_decimal_places_ok(D("1.234"))

    def test_as_grid(self) -> None:
        assert as_grid(PENNY) is PENNY
        assert as_grid(D("0.05")) == TickGrid.flat(D("0.05"))
        with pytest.raises(ValueError, match="tick must be positive"):
            as_grid(D(0))

    @pytest.mark.parametrize(
        ("kw", "match"),
        [
            ({"mleg": D("0.005")}, "whole-cent"),
            ({"penny_above": D(0)}, "whole-cent"),
            ({"boundary": D(0)}, "boundary must be positive"),
            ({"boundary": D("3.01")}, "not a multiple"),
        ],
    )
    def test_rules_validation(self, kw: dict[str, D], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            TickRules(**kw)  # type: ignore[arg-type]

    def test_rules_uppercase_underlyings(self) -> None:
        assert TickRules(penny_all_underlyings=("spy", " qqq")).penny_all_underlyings == (
            "SPY",
            "QQQ",
        )


# ---------------------------------------------------------------------------
# Gate: RuleCode.TICK (card acceptance 3)
# ---------------------------------------------------------------------------


def _single(root: str, premium: str, *, ppind: bool | None, bid: str, ask: str):
    st_ = long_call(root, EXP, strike=300, premium=premium, as_of=G.AS_OF)
    sym = st_.legs[0].occ_symbol
    m = G.mkt(
        quotes={sym: G.quote(bid, ask)},
        penny_program={} if ppind is None else {sym: ppind},
    )
    return st_, m


def _tick_violations(st_, limit: str, m) -> list[str]:
    p = G.make_proposal(structure=st_, limit_price=D(limit))
    out = R.check_spread_tick(p, derive(p), m, G.cfg())
    return [v.detail for v in out if v.code == RuleCode.TICK]


class TestGateTick:
    def test_off_grid_single_leg_penny_is_tick(self) -> None:
        st_, m = _single("COIN", "19.68", ppind=True, bid="19.50", ask="19.90")
        assert _tick_violations(st_, "19.68", m) == [
            "limit 19.68 is not a multiple of tick 0.05 (single-leg penny ≥$3)"
        ]
        assert _tick_violations(st_, "19.70", m) == []

    def test_missing_ppind_falls_back_to_standard(self) -> None:
        st_, m = _single("COIN", "2.12", ppind=None, bid="2.00", ask="2.30")
        assert _tick_violations(st_, "2.12", m) == [
            "limit 2.12 is not a multiple of tick 0.05 (single-leg standard <$3)"
        ]
        assert _tick_violations(st_, "2.15", m) == []
        _, penny = _single("COIN", "2.12", ppind=True, bid="2.00", ask="2.30")
        assert _tick_violations(st_, "2.12", penny) == []

    def test_spy_single_leg_and_mleg_stay_penny(self) -> None:
        st_, m = _single("SPY", "7.07", ppind=False, bid="7.00", ask="7.15")
        assert _tick_violations(st_, "7.07", m) == []
        p = G.make_proposal(limit_price=D("-0.84"))
        assert [v for v in R.check_spread_tick(p, derive(p), G.mkt(), G.cfg())] == []

    def test_evaluate_reports_tick_on_band_worst_price(self) -> None:
        st_, m = _single("COIN", "4.60", ppind=True, bid="4.46", ask="4.74")
        p = G.make_proposal(
            structure=st_,
            limit_price=D("4.60"),
            sizing=G.Sizing(contracts=1, notional=D("460"), pct_equity=0.0046),
        )
        bad = PriceBand(lo=D("4.60"), hi=D("4.73"), max_steps=3)
        d = R.evaluate(p, G.acct(), G.Portfolio(), G.cfg(), market=m, now=G.NOW, band=bad)
        assert any(
            "worst price: limit 4.73 is not a multiple of tick 0.05" in v for v in d.violations
        )


# ---------------------------------------------------------------------------
# Band and ladder on the grid (card acceptance 4)
# ---------------------------------------------------------------------------


class TestBandOnGrid:
    def test_narrow_spread_single_leg_penny_ladder(self) -> None:
        """Mid 4.60, far 4.74, penny ≥$3: distinct $0.05 prices ending ≤ the far touch."""
        st_, m = _single("COIN", "4.60", ppind=True, bid="4.46", ask="4.74")
        band = R.price_band(st_.legs, D("4.60"), m, G.cfg())
        assert (band.lo, band.hi, band.max_steps) == (D("4.60"), D("4.70"), 3)
        ladder = band.ladder(R.grid_for(st_.legs, m, G.cfg()))
        assert ladder == (D("4.60"), D("4.65"), D("4.70"))
        assert all(p % D("0.05") == 0 and p <= D("4.74") for p in ladder)
        assert len(set(ladder)) == len(ladder)

    def test_off_grid_single_leg_limit_gets_no_band(self) -> None:
        st_, m = _single("COIN", "19.68", ppind=True, bid="19.50", ask="19.90")
        band = R.price_band(st_.legs, D("19.68"), m, G.cfg())
        assert (band.hi, band.max_steps) == (D("19.68"), 0)

    def test_mleg_band_unchanged(self) -> None:
        """The baseline bull put band is the pre-D66 band: [-0.85, -0.75], 4 attempts."""
        band = R.proposal_band(G.make_proposal(), G.mkt(), G.cfg())
        assert band == PriceBand(lo=D("-0.85"), hi=D("-0.75"), max_steps=3)
        grid = R.grid_for(G.bull_put().legs, G.mkt(), G.cfg())
        assert band.ladder(grid) == (D("-0.85"), D("-0.82"), D("-0.79"), D("-0.75"))

    def test_ladder_with_no_grid_price_is_empty(self) -> None:
        assert PriceBand(lo=D("4.61"), hi=D("4.64"), max_steps=3).ladder(PENNY) == ()

    def test_ladder_snaps_off_grid_band_inward(self) -> None:
        b = PriceBand(lo=D("4.61"), hi=D("4.79"), max_steps=3)
        assert b.ladder(PENNY) == (D("4.65"), D("4.70"), D("4.75"))
        assert PriceBand(lo=D("4.61"), hi=D("4.64"), max_steps=0).ladder(PENNY) == ()

    def test_band_from_nbbo_on_standard_grid(self) -> None:
        b = band_from_nbbo(D("2.95"), D("3.27"), max_steps=3, reach=D(1), grid=STANDARD)
        assert (b.lo, b.hi) == (D("2.95"), D("3.20"))
        assert b.ladder(STANDARD) == (D("2.95"), D("3.00"), D("3.10"), D("3.20"))

    def test_max_gain_cap_on_grid(self) -> None:
        legs = debit_vertical(
            "call",
            "SPY",
            EXP,
            long_strike=700,
            long_premium="5",
            short_strike=701,
            short_premium="4.4",
            as_of=G.AS_OF,
        ).legs
        assert R.max_gain_cap(legs, MLEG) == D("0.99")
        assert R.max_gain_cap(legs, D("0.05")) == D("0.95")
        assert R.max_gain_cap(legs, STANDARD) == D("0.95")

    def test_reanchor_onto_grid(self) -> None:
        band = PriceBand(lo=D("4.60"), hi=D("4.70"), max_steps=3)
        nb = band.reanchor(D("4.6125"), PENNY)
        assert nb is not None and nb.lo == D("4.65") and nb.hi == D("4.70")
        assert nb.ladder(PENNY) == (D("4.65"), D("4.70"))
        assert band.reanchor(D("4.68"), PENNY) == PriceBand(lo=D("4.70"), hi=D("4.70"), max_steps=0)
        assert band.reanchor(D("4.71"), PENNY) is None

    @given(
        lo_c=st.integers(1, 2_000),
        width_c=st.integers(0, 300),
        n=st.integers(0, 9),
        g=_grids,
    )
    def test_property_every_ladder_price_is_exchange_valid(
        self, lo_c: int, width_c: int, n: int, g: TickGrid
    ) -> None:
        lo = g.snap(D(lo_c) / 100, "up")
        b = band_from_nbbo(lo, lo + D(width_c) / 100, max_steps=n, reach=D(1), grid=g)
        ladder = b.ladder(g)
        assert ladder and ladder[0] == lo and ladder[-1] == (b.hi if n else lo)
        assert all(g.on_grid(p) and b.contains(p) for p in ladder)
        assert list(ladder) == sorted(set(ladder))
