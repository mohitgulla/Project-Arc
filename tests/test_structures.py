"""Tests for arc.structures — OCC symbology, builders, analytics.

Hand-computed examples: every expected number below is derived in the
comment next to it (per-share prices; dollars = x100 per unit).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.models import Leg, LegIntent, Structure, StructureKind
from arc.pricing.bs import BSMInputs, OptionKind, greeks
from arc.structures import (
    MarketInputs,
    OccSymbol,
    UndefinedRiskError,
    analyze,
    assert_defined_risk,
    breakevens,
    buying_power,
    classify,
    credit_vertical,
    debit_vertical,
    format_occ,
    iron_condor,
    is_defined_risk,
    long_call,
    long_put,
    max_gain_loss,
    net_debit_credit,
    net_greeks,
    parse_occ,
    payoff_at,
    payoff_grid,
    strike_grid,
)

EXP = dt.date(2026, 11, 20)
AS_OF = dt.date(2026, 10, 9)  # 42 calendar days to EXP


def _leg(root: str, kind: str, strike: str, side: LegIntent, prem: str, ratio: int = 1) -> Leg:
    return Leg(
        occ_symbol=format_occ(root, EXP, kind, strike),
        side=side,
        ratio=ratio,
        premium=D(prem),
    )


L, S = LegIntent.LONG, LegIntent.SHORT

# ---------------------------------------------------------------------------
# OCC
# ---------------------------------------------------------------------------


class TestOcc:
    def test_parse_padded(self) -> None:
        o = parse_occ("AAPL  260117C00200000")
        assert o == OccSymbol(
            root="AAPL", expiration=dt.date(2026, 1, 17), kind=OptionKind.CALL, strike=D(200)
        )

    def test_parse_compact_fractional_strike(self) -> None:
        o = parse_occ("SPY261120P00412500")
        assert o.root == "SPY"
        assert o.kind == OptionKind.PUT
        assert o.strike == D("412.5")
        assert o.expiration == EXP

    def test_parse_one_char_and_six_char_roots(self) -> None:
        assert parse_occ("F     261120C00012000").root == "F"
        assert parse_occ("GOOGL1261120C00150000").root == "GOOGL1"

    def test_format_compact_and_padded(self) -> None:
        assert format_occ("AAPL", dt.date(2026, 1, 17), "c", 200) == "AAPL260117C00200000"
        assert (
            format_occ("AAPL", dt.date(2026, 1, 17), OptionKind.PUT, "187.5", padded=True)
            == "AAPL  260117P00187500"
        )
        assert format_occ("SPY", EXP, "call", D("0.5")) == "SPY261120C00000500"

    def test_str_and_format_method(self) -> None:
        o = parse_occ("AAPL  260117C00200000")
        assert str(o) == "AAPL260117C00200000"
        assert o.format(padded=True) == "AAPL  260117C00200000"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "AAPL",
            "aapl260117C00200000",  # lowercase
            "AAPL260117X00200000",  # bad C/P
            "AAPL260117C0020000",  # 7-digit strike
            "TOOLONGX260117C00200000",  # 8-char root
            "AAPL261317C00200000",  # month 13
            "AAPL260230C00200000",  # Feb 30
        ],
    )
    def test_parse_rejects(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_occ(bad)

    @pytest.mark.parametrize(
        ("root", "kind", "strike"),
        [
            ("aapl", "c", 100),  # lowercase root
            ("AAPL", "x", 100),  # bad kind
            ("AAPL", "c", "1.2345"),  # >3 dp
            ("AAPL", "c", "abc"),  # not a number
            ("AAPL", "c", 0),  # non-positive
            ("AAPL", "c", 100000),  # too large for 8 digits
        ],
    )
    def test_format_rejects(self, root: str, kind: str, strike: object) -> None:
        with pytest.raises(ValueError):
            format_occ(root, EXP, kind, strike)  # type: ignore[arg-type]

    def test_format_rejects_year_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            format_occ("AAPL", dt.date(2100, 1, 1), "c", 100)

    @given(
        root=st.from_regex(r"[A-Z][A-Z0-9]{0,5}", fullmatch=True),
        exp=st.dates(dt.date(2000, 1, 1), dt.date(2099, 12, 31)),
        kind=st.sampled_from(list(OptionKind)),
        milli=st.integers(1, 99_999_999),
        padded=st.booleans(),
    )
    def test_roundtrip(
        self, root: str, exp: dt.date, kind: OptionKind, milli: int, padded: bool
    ) -> None:
        strike = D(milli) / 1000
        sym = format_occ(root, exp, kind, strike, padded=padded)
        if padded:
            assert len(sym) == 21
        o = parse_occ(sym)
        assert (o.root, o.expiration, o.kind, o.strike) == (root, exp, kind, strike)


# ---------------------------------------------------------------------------
# Builders — hand-computed examples
# ---------------------------------------------------------------------------


class TestLongCall:
    def test_numbers(self) -> None:
        # AAPL 200C @ 5.00: debit 5.00, max loss 500, unbounded gain, BE 205.
        s = long_call("AAPL", EXP, 200, "5.00", as_of=AS_OF)
        assert s.kind == StructureKind.LONG_CALL
        assert s.net_debit_credit == D("5.00")
        assert s.max_gain is None
        assert s.max_loss == D(500)
        assert s.breakevens == [D(205)]
        assert s.buying_power == D(500)
        assert s.dte == 42
        assert s.legs[0].occ_symbol == "AAPL261120C00200000"
        assert s.legs[0].side == LegIntent.LONG
        # At 220: (20 - 5) * 100 = 1500. At 190: -500.
        assert payoff_at(s.legs, 220) == D(1500)
        assert payoff_at(s.legs, 190) == D(-500)
        assert is_defined_risk(s.legs)

    def test_rejects_zero_premium(self) -> None:
        with pytest.raises(ValueError, match="premium"):
            long_call("AAPL", EXP, 200, 0, as_of=AS_OF)


class TestLongPut:
    def test_numbers(self) -> None:
        # AAPL 190P @ 4.00: max gain (190-4)*100 = 18600 at S=0, max loss 400, BE 186.
        s = long_put("AAPL", EXP, 190, "4.00", as_of=AS_OF)
        assert s.kind == StructureKind.LONG_PUT
        assert s.net_debit_credit == D("4.00")
        assert s.max_gain == D(18600)
        assert s.max_loss == D(400)
        assert s.breakevens == [D(186)]
        assert s.buying_power == D(400)
        assert payoff_at(s.legs, 0) == D(18600)
        assert payoff_at(s.legs, 180) == D(600)


class TestDebitVertical:
    def test_bull_call(self) -> None:
        # Long 100C @3.00, short 105C @1.20: debit 1.80.
        # Max gain (5 - 1.80)*100 = 320; max loss 180; BE 101.80.
        s = debit_vertical(
            "c",
            "XYZ",
            EXP,
            long_strike=100,
            long_premium="3.00",
            short_strike=105,
            short_premium="1.20",
            as_of=AS_OF,
        )
        assert s.kind == StructureKind.VERTICAL_DEBIT
        assert s.net_debit_credit == D("1.80")
        assert s.max_gain == D(320)
        assert s.max_loss == D(180)
        assert s.breakevens == [D("101.80")]
        assert s.buying_power == D(180)
        assert payoff_at(s.legs, D("102.5")) == D(70)  # (2.5 - 1.8)*100

    def test_bear_put(self) -> None:
        # Long 105P @3.50, short 100P @1.50: debit 2.00; gain 300, loss 200, BE 103.
        s = debit_vertical(
            OptionKind.PUT,
            "XYZ",
            EXP,
            long_strike=105,
            long_premium="3.50",
            short_strike=100,
            short_premium="1.50",
            as_of=AS_OF,
        )
        assert s.kind == StructureKind.VERTICAL_DEBIT
        assert s.net_debit_credit == D("2.00")
        assert (s.max_gain, s.max_loss) == (D(300), D(200))
        assert s.breakevens == [D(103)]

    def test_wrong_geometry_rejected(self) -> None:
        # long 105C / short 100C is a credit (bear call) spread.
        with pytest.raises(ValueError, match="vertical_credit"):
            debit_vertical(
                "c",
                "XYZ",
                EXP,
                long_strike=105,
                long_premium="1.2",
                short_strike=100,
                short_premium="3",
                as_of=AS_OF,
            )

    def test_bad_prices_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-debit"):
            debit_vertical(
                "c",
                "XYZ",
                EXP,
                long_strike=100,
                long_premium="1",
                short_strike=105,
                short_premium="1.5",
                as_of=AS_OF,
            )

    def test_same_strike_rejected(self) -> None:
        with pytest.raises(ValueError, match="other"):
            debit_vertical(
                "c",
                "XYZ",
                EXP,
                long_strike=100,
                long_premium="1",
                short_strike=100,
                short_premium="1",
                as_of=AS_OF,
            )


class TestCreditVertical:
    def test_bull_put(self) -> None:
        # Short 95P @2.10, long 90P @0.85: credit 1.25.
        # Max gain 125; max loss (5 - 1.25)*100 = 375; BE 95 - 1.25 = 93.75.
        s = credit_vertical(
            "p",
            "XYZ",
            EXP,
            short_strike=95,
            short_premium="2.10",
            long_strike=90,
            long_premium="0.85",
            as_of=AS_OF,
        )
        assert s.kind == StructureKind.VERTICAL_CREDIT
        assert s.net_debit_credit == D("-1.25")
        assert (s.max_gain, s.max_loss) == (D(125), D(375))
        assert s.breakevens == [D("93.75")]
        assert s.buying_power == D(375)  # width*100 - credit*100

    def test_bear_call(self) -> None:
        # Short 110C @1.60, long 115C @0.50: credit 1.10; gain 110, loss 390, BE 111.10.
        s = credit_vertical(
            "call",
            "XYZ",
            EXP,
            short_strike=110,
            short_premium="1.60",
            long_strike=115,
            long_premium="0.50",
            as_of=AS_OF,
        )
        assert s.net_debit_credit == D("-1.10")
        assert (s.max_gain, s.max_loss) == (D(110), D(390))
        assert s.breakevens == [D("111.10")]

    def test_bad_prices_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-credit"):
            credit_vertical(
                "p",
                "XYZ",
                EXP,
                short_strike=95,
                short_premium="0.5",
                long_strike=90,
                long_premium="0.8",
                as_of=AS_OF,
            )


class TestIronCondor:
    def _ic(self, lc: int = 470) -> Structure:
        return iron_condor(
            "SPY",
            EXP,
            long_put_strike=380,
            long_put_premium="1.00",
            short_put_strike=390,
            short_put_premium="2.50",
            short_call_strike=460,
            short_call_premium="2.00",
            long_call_strike=lc,
            long_call_premium="0.70",
            as_of=AS_OF,
        )

    def test_symmetric(self) -> None:
        # Credit 2.50 + 2.00 - 1.00 - 0.70 = 2.80.
        # Max gain 280 (between 390 and 460); max loss (10 - 2.80)*100 = 720.
        # BEs 390 - 2.80 = 387.20 and 460 + 2.80 = 462.80.
        s = self._ic()
        assert s.kind == StructureKind.IRON_CONDOR
        assert s.net_debit_credit == D("-2.80")
        assert (s.max_gain, s.max_loss) == (D(280), D(720))
        assert s.breakevens == [D("387.20"), D("462.80")]
        assert s.buying_power == D(720)
        assert payoff_at(s.legs, 425) == D(280)
        assert payoff_at(s.legs, 300) == D(-720)
        assert payoff_at(s.legs, 600) == D(-720)
        assert [lg.intent for lg in s.legs] == [
            "long put wing",
            "short put",
            "short call",
            "long call wing",
        ]

    def test_unequal_wings(self) -> None:
        # Call wing 15 wide: max loss = 15*100 - 280 = 1220 (widest side).
        s = self._ic(lc=475)
        assert s.max_loss == D(1220)
        assert s.buying_power == D(1220)
        assert s.max_gain == D(280)

    def test_strike_order_rejected(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            self._ic(lc=455)

    def test_debit_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-credit"):
            iron_condor(
                "SPY",
                EXP,
                long_put_strike=380,
                long_put_premium="3",
                short_put_strike=390,
                short_put_premium="1",
                short_call_strike=460,
                short_call_premium="1",
                long_call_strike=470,
                long_call_premium="3",
                as_of=AS_OF,
            )


# ---------------------------------------------------------------------------
# Analytics — defined risk, classification, grid, errors
# ---------------------------------------------------------------------------


class TestDefinedRisk:
    def test_naked_short_call_unbounded(self) -> None:
        legs = [_leg("XYZ", "c", "100", S, "2")]
        assert max_gain_loss(legs) == (D(200), None)
        assert not is_defined_risk(legs)
        assert buying_power(legs) is None
        with pytest.raises(UndefinedRiskError, match="uncovered short call.*unbounded"):
            assert_defined_risk(legs)

    def test_naked_short_put_bounded_but_uncovered(self) -> None:
        # Max loss bounded at (100 - 2)*100 = 9800 but still uncovered → not defined-risk.
        legs = [_leg("XYZ", "p", "100", S, "2")]
        assert max_gain_loss(legs) == (D(200), D(9800))
        assert not is_defined_risk(legs)
        with pytest.raises(UndefinedRiskError, match="uncovered short put"):
            assert_defined_risk(legs)

    def test_ratio_spread_uncovered(self) -> None:
        # 1x2 call ratio: long 1x 100C, short 2x 105C → one uncovered call.
        legs = [_leg("XYZ", "c", "100", L, "3"), _leg("XYZ", "c", "105", S, "1.2", ratio=2)]
        assert max_gain_loss(legs)[1] is None
        with pytest.raises(UndefinedRiskError, match="1 uncovered short call"):
            assert_defined_risk(legs)
        assert classify(legs) == StructureKind.OTHER

    def test_assert_accepts_structure(self) -> None:
        s = long_call("AAPL", EXP, 200, 5, as_of=AS_OF)
        assert_defined_risk(s)  # no raise
        assert_defined_risk(s.legs)

    def test_long_straddle_defined(self) -> None:
        legs = [_leg("XYZ", "c", "100", L, "3"), _leg("XYZ", "p", "100", L, "2.5")]
        assert is_defined_risk(legs)
        assert classify(legs) == StructureKind.OTHER
        # BEs 100 ± 5.50
        assert breakevens(legs) == [D("94.5"), D("105.5")]


class TestClassify:
    def test_single_short_is_other(self) -> None:
        assert classify([_leg("XYZ", "c", "100", S, "1")]) == StructureKind.OTHER

    def test_mixed_type_pair_is_other(self) -> None:
        legs = [_leg("XYZ", "c", "100", L, "1"), _leg("XYZ", "p", "95", S, "1")]
        assert classify(legs) == StructureKind.OTHER

    def test_same_side_pair_is_other(self) -> None:
        legs = [_leg("XYZ", "c", "100", L, "1"), _leg("XYZ", "c", "105", L, "1")]
        assert classify(legs) == StructureKind.OTHER

    def test_iron_butterfly_is_other(self) -> None:
        # Short strikes equal → not an iron condor.
        legs = [
            _leg("XYZ", "p", "90", L, "0.5"),
            _leg("XYZ", "p", "100", S, "3"),
            _leg("XYZ", "c", "100", S, "3"),
            _leg("XYZ", "c", "110", L, "0.5"),
        ]
        assert classify(legs) == StructureKind.OTHER

    def test_four_calls_is_other(self) -> None:
        legs = [
            _leg("XYZ", "c", "90", L, "11"),
            _leg("XYZ", "c", "95", S, "7"),
            _leg("XYZ", "c", "105", S, "2"),
            _leg("XYZ", "c", "110", L, "1"),
        ]
        assert classify(legs) == StructureKind.OTHER

    def test_reverse_iron_condor_is_other(self) -> None:
        legs = [
            _leg("XYZ", "p", "90", S, "0.5"),
            _leg("XYZ", "p", "95", L, "1"),
            _leg("XYZ", "c", "105", L, "1"),
            _leg("XYZ", "c", "110", S, "0.5"),
        ]
        assert classify(legs) == StructureKind.OTHER

    def test_three_legs_is_other(self) -> None:
        legs = [
            _leg("XYZ", "c", "95", L, "6"),
            _leg("XYZ", "c", "100", S, "3", ratio=1),
            _leg("XYZ", "c", "105", L, "1"),
        ]
        assert classify(legs) == StructureKind.OTHER


class TestGridAndErrors:
    def _bull_call(self) -> list[Leg]:
        return [_leg("XYZ", "c", "100", L, "3"), _leg("XYZ", "c", "105", S, "1.2")]

    def test_strike_grid_default(self) -> None:
        g = strike_grid(self._bull_call())
        # pad 20%: lo 80, hi 126, step = 5 (min strike gap); strikes included.
        assert g[0] == D(80) and g[-1] == D(126)
        assert D(100) in g and D(105) in g
        assert g == sorted(g)

    def test_strike_grid_single_strike_step(self) -> None:
        g = strike_grid([_leg("XYZ", "c", "100", L, "3")], pad=0)
        assert g == [D(100)]
        g = strike_grid([_leg("XYZ", "c", "100", L, "3")], pad="0.05")
        assert g[1] - g[0] == D(1)  # 1% of 100

    def test_strike_grid_errors(self) -> None:
        with pytest.raises(ValueError, match="pad"):
            strike_grid(self._bull_call(), pad=-1)
        with pytest.raises(ValueError, match="step"):
            strike_grid(self._bull_call(), step=0)
        with pytest.raises(ValueError, match="points"):
            strike_grid(self._bull_call(), step="0.001")

    def test_payoff_grid(self) -> None:
        pg = payoff_grid(self._bull_call(), [95, 100, "102.5", 110])
        assert pg == [
            (D(95), D(-180)),
            (D(100), D(-180)),
            (D("102.5"), D(70)),
            (D(110), D(320)),
        ]
        assert len(payoff_grid(self._bull_call())) == len(strike_grid(self._bull_call()))

    def test_payoff_negative_spot(self) -> None:
        with pytest.raises(ValueError, match="spot"):
            payoff_at(self._bull_call(), -1)

    def test_no_legs(self) -> None:
        with pytest.raises(ValueError, match="no legs"):
            net_debit_credit([])

    def test_missing_premium(self) -> None:
        leg = Leg(occ_symbol="XYZ261120C00100000", side=L)
        with pytest.raises(ValueError, match="premium"):
            max_gain_loss([leg])

    def test_mixed_expiry_rejected(self) -> None:
        other = Leg(occ_symbol="XYZ261218C00105000", side=S, premium=D(1))
        with pytest.raises(ValueError, match="single root and expiration"):
            analyze([_leg("XYZ", "c", "100", L, "3"), other], as_of=AS_OF)

    def test_mixed_root_rejected(self) -> None:
        with pytest.raises(ValueError, match="single root"):
            analyze([_leg("XYZ", "c", "100", L, "3"), _leg("ABC", "c", "105", S, "1")])

    def test_expired_rejected(self) -> None:
        with pytest.raises(ValueError, match="before as_of"):
            analyze(self._bull_call(), as_of=EXP + dt.timedelta(days=1))

    def test_expiry_day_dte_zero(self) -> None:
        assert analyze(self._bull_call(), as_of=EXP).dte == 0

    def test_default_as_of_uses_today(self) -> None:
        far = dt.date(2099, 12, 18)
        leg = Leg(occ_symbol=format_occ("XYZ", far, "c", 100), side=L, premium=D(1))
        assert analyze([leg]).dte > 0

    def test_breakeven_at_kink_and_zero_segment(self) -> None:
        # Zero-cost long call: P&L is 0 on [0, 100] → BE reported at every kink with 0.
        legs = [_leg("XYZ", "c", "100", L, "0")]
        assert breakevens(legs) == [D(0), D(100)]
        # Zero-cost bull call spread: P&L 0 below 100, flat 500 above 105.
        legs = [_leg("XYZ", "c", "100", L, "1"), _leg("XYZ", "c", "105", S, "1")]
        assert breakevens(legs) == [D(0), D(100)]
        # Short call worth 0 with slope < 0 → last kink is itself a BE.
        legs = [_leg("XYZ", "c", "100", S, "0")]
        assert breakevens(legs) == [D(0), D(100)]


# ---------------------------------------------------------------------------
# Net Greeks
# ---------------------------------------------------------------------------


class TestNetGreeks:
    MKT_IV = {"XYZ261120C00100000": 0.30, "XYZ261120C00105000": 0.28}

    def _mkt(self) -> MarketInputs:
        return MarketInputs(spot=102.0, r=0.04, q=0.01, ivs=self.MKT_IV)

    def test_bull_call_matches_leg_sum(self) -> None:
        s = debit_vertical(
            "c",
            "XYZ",
            EXP,
            long_strike=100,
            long_premium="3.00",
            short_strike=105,
            short_premium="1.20",
            as_of=AS_OF,
            market=self._mkt(),
        )
        t = 42 / 365.0
        g_long = greeks(BSMInputs(S=102, K=100, t=t, r=0.04, q=0.01, sigma=0.30, flag="c"))
        g_short = greeks(BSMInputs(S=102, K=105, t=t, r=0.04, q=0.01, sigma=0.28, flag="c"))
        for name in ("delta", "gamma", "vega", "theta", "rho", "vanna", "volga"):
            want = 100 * (getattr(g_long, name) - getattr(g_short, name))
            assert getattr(s.greeks, name) == pytest.approx(want, rel=1e-12, abs=1e-12)
        # Bull call: long delta, positive but < 100 share-equivalents.
        assert 0 < s.greeks.delta < 100

    def test_ratio_scales(self) -> None:
        one = [_leg("XYZ", "c", "100", L, "3")]
        two = [_leg("XYZ", "c", "100", L, "3", ratio=2)]
        g1 = net_greeks(one, self._mkt(), 42)
        g2 = net_greeks(two, self._mkt(), 42)
        assert g2.delta == pytest.approx(2 * g1.delta)

    def test_symmetric_condor_near_delta_neutral(self) -> None:
        # Flat vol, spot centred, symmetric strikes → |Δ| small vs. a single wing.
        ivs = {
            format_occ("XYZ", EXP, k, s): 0.25
            for k, s in (("p", 90), ("p", 95), ("c", 105), ("c", 110))
        }
        mkt = MarketInputs(spot=100.0, r=0.0, q=0.0, ivs=ivs)
        s = iron_condor(
            "XYZ",
            EXP,
            long_put_strike=90,
            long_put_premium="0.4",
            short_put_strike=95,
            short_put_premium="1.2",
            short_call_strike=105,
            short_call_premium="1.1",
            long_call_strike=110,
            long_call_premium="0.35",
            as_of=AS_OF,
            market=mkt,
        )
        assert abs(s.greeks.delta) < 5
        assert s.greeks.gamma < 0  # short premium
        assert s.greeks.vega < 0
        assert s.greeks.theta > 0

    def test_missing_iv(self) -> None:
        with pytest.raises(ValueError, match="implied vol"):
            net_greeks([_leg("XYZ", "c", "100", L, "3")], MarketInputs(spot=100, r=0, ivs={}), 42)

    def test_dte_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="dte"):
            net_greeks([_leg("XYZ", "c", "100", L, "3")], self._mkt(), 0)

    def test_no_market_leaves_zero_greeks(self) -> None:
        s = long_call("XYZ", EXP, 100, 3, as_of=AS_OF)
        assert s.greeks.delta == 0.0


# ---------------------------------------------------------------------------
# Property tests: analytic extremes / breakevens agree with a dense grid
# ---------------------------------------------------------------------------

_price = st.decimals(min_value="0.01", max_value="20", places=2)


@st.composite
def _random_legs(draw: st.DrawFn) -> list[Leg]:
    n = draw(st.integers(1, 4))
    legs = []
    for _ in range(n):
        legs.append(
            _leg(
                "XYZ",
                draw(st.sampled_from(["c", "p"])),
                str(draw(st.integers(80, 120))),
                draw(st.sampled_from([L, S])),
                str(draw(_price)),
                ratio=draw(st.integers(1, 3)),
            )
        )
    return legs


@settings(max_examples=200, deadline=None)
@given(_random_legs())
def test_extremes_match_dense_grid(legs: list[Leg]) -> None:
    grid = [D(i) / 2 for i in range(0, 2 * 400 + 1)]  # 0..400 step 0.5 covers all kinks
    vals = [payoff_at(legs, s) for s in grid]
    mg, ml = max_gain_loss(legs)
    if mg is not None:
        assert max(vals) == mg
    else:
        assert payoff_at(legs, 10_000) > max(vals)
    if ml is not None:
        assert -min(vals) == ml
    else:
        assert payoff_at(legs, 10_000) < min(vals)


@settings(max_examples=200, deadline=None)
@given(_random_legs())
def test_breakevens_are_zeros(legs: list[Leg]) -> None:
    for be in breakevens(legs):
        # BEs are quantised to 4 dp; payoff slope ≤ 100 * 12 legs-ratio per $.
        assert abs(payoff_at(legs, be)) <= D("0.0001") * 100 * 12 + D("0.01")


@settings(max_examples=200, deadline=None)
@given(
    lo=st.integers(50, 150),
    width=st.integers(1, 20),
    p_long=_price,
    p_short=_price,
)
def test_defined_risk_verticals_bp_equals_max_loss(
    lo: int, width: int, p_long: D, p_short: D
) -> None:
    legs = [
        _leg("XYZ", "p", str(lo), L, str(p_long)),
        _leg("XYZ", "p", str(lo + width), S, str(p_short)),
    ]
    mg, ml = max_gain_loss(legs)
    assert is_defined_risk(legs)
    assert mg is not None and ml is not None
    assert mg + ml == D(width) * 100  # vertical: gain + loss = width
    assert buying_power(legs) == max(ml, D(0))
