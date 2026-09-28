"""Tests for arc.scanner — filters, IV rank/percentile, strike selection, scan, CLI.

Offline only: the recorded SPY chain (arc/data/fixtures/spy_chain.json, Alpaca
paper indicative feed, 2026-09-25 close) and synthetic BSM chains.
Hand-computed expectations are derived in the comment beside each assertion.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal as D
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.backtest.costs import CostModel, load_cost_model
from arc.config import get_settings
from arc.data.base import DataQualityFlag, MarketDataProvider, OptionContract, OptionGreeks
from arc.data.base import UnderlyingQuote as Quote
from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData, load_recording
from arc.pricing.bs import BSMInputs, OptionKind, delta, price
from arc.scanner import (
    CREDIT_STRATEGIES,
    LiquidityRules,
    RankBy,
    Reject,
    ScanParams,
    ScanStrategy,
    apply_filters,
    atm_iv,
    check_contract,
    iv_percentile,
    iv_rank,
    iv_stats,
    load_iv_history,
    record_iv,
    scan,
    select_shorts,
    select_wing,
    spread_ok,
)
from arc.structures import assert_defined_risk, format_occ, parse_occ
from arc.utils.calendar import ET

RULES = LiquidityRules()  # PLAN §5 defaults: 10% / $0.10 spread, OI 100, vol 10


def _c(
    strike: float = 100.0,
    kind: str = "put",
    *,
    bid: float | None = 1.00,
    ask: float | None = 1.05,
    oi: int | None = 500,
    vol: int | None = 50,
    d: float | None = -0.20,
    iv: float | None = 0.20,
    exp: dt.date = dt.date(2026, 11, 6),
    flags: list[str] | None = None,
) -> OptionContract:
    sym = format_occ("XYZ", exp, kind, strike)
    return OptionContract(
        symbol=sym,
        underlying="XYZ",
        expiration=exp,
        strike=strike,
        option_type=kind,
        bid=bid,
        ask=ask,
        mid=None if bid is None or ask is None else (bid + ask) / 2,
        open_interest=oi,
        volume=vol,
        implied_volatility=iv,
        greeks=None if d is None else OptionGreeks(delta=d),
        quality_flags=[DataQualityFlag(symbol=sym, issue=f) for f in flags or []],
    )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_clean_contract_passes(self) -> None:
        assert check_contract(_c(), RULES) is None

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"bid": None}, Reject.NO_QUOTE),
            ({"ask": None}, Reject.NO_QUOTE),
            ({"bid": 1.2, "ask": 1.1}, Reject.NO_QUOTE),  # crossed
            ({"bid": 0.0, "ask": 0.0}, Reject.NO_QUOTE),
            ({"bid": 0.0, "ask": 0.05}, Reject.ZERO_BID),
            ({"d": None}, Reject.MISSING_GREEKS),
            ({"iv": None}, Reject.MISSING_GREEKS),
            ({"iv": 0.0}, Reject.MISSING_GREEKS),
            ({"flags": ["stale_timestamp"]}, Reject.STALE_QUOTE),
            # mid 1.15, 10% = 0.115 > $0.10 → limit 0.115; spread 0.30 fails
            ({"bid": 1.00, "ask": 1.30}, Reject.WIDE_SPREAD),
            ({"oi": None}, Reject.LOW_OPEN_INTEREST),
            ({"oi": 99}, Reject.LOW_OPEN_INTEREST),
            ({"vol": None}, Reject.LOW_VOLUME),
            ({"vol": 9}, Reject.LOW_VOLUME),
        ],
    )
    def test_each_rule(self, kwargs: dict, reason: Reject) -> None:
        assert check_contract(_c(**kwargs), RULES) is reason

    def test_minimums_are_inclusive(self) -> None:
        assert check_contract(_c(oi=100, vol=10), RULES) is None

    def test_other_quality_flags_do_not_reject(self) -> None:
        # zero_bid / missing_greeks flags are re-derived from the data itself.
        assert check_contract(_c(flags=["missing_greeks"]), RULES) is None

    def test_spread_rule_is_pct_or_abs(self) -> None:
        # Cheap option: mid 0.50, 10% = 0.05; the $0.10 floor applies → 0.10 passes.
        assert spread_ok(0.45, 0.55, RULES)
        assert not spread_ok(0.44, 0.56, RULES)
        # Expensive option: mid 20, 10% = $2.00 → a $1.50 spread passes.
        assert spread_ok(19.25, 20.75, RULES)
        assert not spread_ok(18.9, 21.1, RULES)

    def test_rules_from_settings(self) -> None:
        s = get_settings(spread_max_pct=0.05, scanner_min_open_interest=7, scanner_min_volume=3)
        r = LiquidityRules.from_settings(s)
        assert (r.spread_max_pct, r.spread_max_abs, r.min_open_interest, r.min_volume) == (
            0.05,
            0.10,
            7,
            3,
        )

    def test_apply_filters_report(self) -> None:
        kept, rep = apply_filters([_c(), _c(oi=1), _c(oi=2), _c(bid=None)], RULES)
        assert len(kept) == 1
        assert rep.total == 4
        assert rep.kept == 1
        assert rep.rejected == {"low_open_interest": 2, "no_quote": 1}

    @given(
        bid=st.floats(0.01, 500, allow_nan=False),
        spread=st.floats(0, 50, allow_nan=False),
    )
    def test_spread_ok_matches_definition(self, bid: float, spread: float) -> None:
        ask = bid + spread
        limit = max(0.10 * (bid + ask) / 2, 0.10)
        if spread <= limit - 1e-6:
            assert spread_ok(bid, ask, RULES)
        elif spread >= limit + 1e-6:
            assert not spread_ok(bid, ask, RULES)


# ---------------------------------------------------------------------------
# IV
# ---------------------------------------------------------------------------

AS_OF = dt.date(2026, 9, 25)


def _hist(values: list[float], end: dt.date = AS_OF) -> dict[dt.date, float]:
    """Daily history ending the day before *end*."""
    n = len(values)
    return {end - dt.timedelta(days=n - i): v for i, v in enumerate(values)}


class TestIv:
    def test_atm_iv_interpolates_and_averages_call_put(self) -> None:
        cs = [
            _c(100, "put", iv=0.20),
            _c(100, "call", iv=0.22),  # strike 100 → mean 0.21
            _c(110, "put", iv=0.18),  # strike 110 → 0.18
        ]
        # spot 102.5 is 25% of the way from 100 to 110: 0.21 + 0.25 * (0.18 - 0.21) = 0.2025
        assert atm_iv(cs, 102.5) == pytest.approx(0.2025)
        assert atm_iv(cs, 90) == pytest.approx(0.21)  # clamps low
        assert atm_iv(cs, 120) == pytest.approx(0.18)  # clamps high
        assert atm_iv(cs, 110) == pytest.approx(0.18)  # exactly on a strike
        assert atm_iv([_c(iv=None)], 100) is None
        assert atm_iv([], 100) is None

    def test_rank_and_percentile_hand_computed(self) -> None:
        h = _hist([0.10, 0.20, 0.30, 0.40])
        # window [0.10, 0.20, 0.30, 0.40, 0.25]: rank (0.25-0.10)/(0.40-0.10) = 0.5
        assert iv_rank(h, AS_OF, 0.25) == pytest.approx(0.5)
        # 2 of 4 prior values are below 0.25
        assert iv_percentile(h, AS_OF, 0.25) == pytest.approx(0.5)
        # new high → rank 1, percentile 1; new low → 0, 0
        assert iv_rank(h, AS_OF, 0.50) == 1.0
        assert iv_percentile(h, AS_OF, 0.50) == 1.0
        assert iv_rank(h, AS_OF, 0.05) == 0.0
        assert iv_percentile(h, AS_OF, 0.05) == 0.0

    def test_lookback_trims_old_observations_and_ignores_future(self) -> None:
        h = _hist([0.90, 0.10, 0.20, 0.30])
        h[AS_OF] = 5.0  # same-day stored value is replaced by today's
        h[AS_OF + dt.timedelta(days=1)] = 9.0  # future must never be seen
        # lookback 4 → prior [0.10, 0.20, 0.30] + today 0.20; 0.90 dropped
        assert iv_rank(h, AS_OF, 0.20, lookback=4) == pytest.approx(0.5)
        assert iv_percentile(h, AS_OF, 0.20, lookback=4) == pytest.approx(1 / 3)

    def test_flat_and_empty_windows(self) -> None:
        assert iv_rank(_hist([0.2, 0.2]), AS_OF, 0.2) == 0.5
        assert iv_rank({}, AS_OF, 0.2) == 0.5
        assert iv_percentile({}, AS_OF, 0.2) == 0.5
        with pytest.raises(ValueError, match="lookback"):
            iv_rank({}, AS_OF, 0.2, lookback=1)

    def test_iv_stats_min_obs(self) -> None:
        h = _hist([0.1 + 0.01 * i for i in range(18)])
        s = iv_stats(h, AS_OF, 0.15, min_obs=20)  # 18 prior + today = 19 < 20
        assert s.atm_iv == 0.15
        assert s.observations == 19
        assert s.iv_rank is None
        assert s.iv_percentile is None
        s = iv_stats(h, AS_OF, 0.15, min_obs=19, expiration=dt.date(2026, 10, 30))
        assert s.iv_rank is not None
        assert s.iv_percentile is not None
        assert s.atm_iv_expiration == dt.date(2026, 10, 30)
        assert iv_stats(h, AS_OF, None).atm_iv is None

    def test_history_roundtrip(self, tmp_path) -> None:  # noqa: ANN001
        assert load_iv_history(tmp_path, "spy") == {}
        record_iv(tmp_path, "spy", dt.date(2026, 9, 25), 0.15)
        record_iv(tmp_path, "SPY", dt.date(2026, 9, 24), 0.14)
        p = record_iv(tmp_path, "SPY", dt.date(2026, 9, 25), 0.16)  # upsert
        assert p.name == "SPY.csv"
        assert p.read_text().splitlines() == [
            "date,atm_iv",
            "2026-09-24,0.140000",
            "2026-09-25,0.160000",
        ]
        assert load_iv_history(tmp_path, "SPY") == {
            dt.date(2026, 9, 24): 0.14,
            dt.date(2026, 9, 25): 0.16,
        }
        with pytest.raises(ValueError, match="positive"):
            record_iv(tmp_path, "SPY", AS_OF, 0.0)
        (tmp_path / "BAD.csv").write_text("date,atm_iv\n2026-09-25,-0.1\n")
        with pytest.raises(ValueError, match="non-positive"):
            load_iv_history(tmp_path, "BAD")

    @given(
        hist=st.lists(st.floats(0.01, 3.0), min_size=0, max_size=60),
        today=st.floats(0.01, 3.0),
        lookback=st.integers(2, 80),
    )
    def test_rank_percentile_bounds(self, hist: list[float], today: float, lookback: int) -> None:
        h = _hist(hist)
        r = iv_rank(h, AS_OF, today, lookback=lookback)
        p = iv_percentile(h, AS_OF, today, lookback=lookback)
        assert 0.0 <= r <= 1.0
        assert 0.0 <= p <= 1.0
        # percentile ≤ share of the window that is ≤ today; rank 1 iff today is the max
        w = [*hist[-(lookback - 1) :], today]
        if today >= max(w):
            assert r == 1.0 or max(w) - min(w) <= 1e-12


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


class TestSelection:
    def test_select_shorts_band_and_order(self) -> None:
        cs = [
            _c(90, d=-0.10),  # outside band
            _c(94, d=-0.17),
            _c(95, d=-0.19),
            _c(96, d=-0.21),
            _c(97, d=-0.23),
            _c(98, d=-0.31),  # outside band
        ]
        got = select_shorts(cs, target=0.20, delta_min=0.16, delta_max=0.30, n=3)
        # |Δ-0.20|: 95→0.01, 96→0.01 (tie → lower |Δ| first = 95), 94/97→0.03 (tie → 94)
        assert [c.strike for c in got] == [95, 96, 94]
        assert select_shorts(cs, target=0.20, delta_min=0.16, delta_max=0.30, n=10)[-1].strike == 97

    def test_select_wing_put_and_call(self) -> None:
        puts = [_c(k, "put") for k in (85, 88, 89, 91, 95, 100)]
        short = _c(95, "put")
        # distances below 95: 91→4, 89→6, 88→7, 85→10; width 5 → 4 and 6 tie → narrower (91)
        assert select_wing(short, puts, 5.0).strike == 91
        calls = [_c(k, "call", d=0.2) for k in (100, 103, 107, 111)]
        # above 100: 103→3, 107→7, 111→11; width 5: |3-5|=2 = |7-5| → narrower 103
        assert select_wing(_c(100, "call", d=0.2), calls, 5.0).strike == 103
        # nothing within [2.5, 10] → None
        assert select_wing(_c(100, "call", d=0.2), [_c(101, "call"), _c(120, "call")], 5) is None
        # other expiry is ignored
        other = _c(90, "put", exp=dt.date(2026, 11, 13))
        assert select_wing(short, [other], 5.0) is None


# ---------------------------------------------------------------------------
# Scan on the recorded SPY chain
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spy() -> RecordedMarketData:
    return RecordedMarketData.from_files(SPY_CHAIN_FIXTURE)


@pytest.fixture(scope="module")
def spy_result(spy: RecordedMarketData):  # noqa: ANN201
    return scan(spy, "SPY", ScanParams(), as_of=spy.recording("SPY").as_of)


class TestRecordedProvider:
    def test_is_market_data_provider(self, spy: RecordedMarketData) -> None:
        assert isinstance(spy, MarketDataProvider)

    def test_fixture_contents(self, spy: RecordedMarketData) -> None:
        rec = spy.recording("spy")
        assert rec.underlying == "SPY"
        assert rec.as_of == dt.date(2026, 9, 27)
        assert rec.quote.mid == pytest.approx(771.335)
        assert len(rec.contracts) == 458
        assert {c.expiration for c in rec.contracts} == {
            dt.date(2026, 10, 30),
            dt.date(2026, 11, 6),
        }

    def test_chain_window_quote_bars(self, spy: RecordedMarketData) -> None:
        ch = spy.option_chain("SPY", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
        assert ch
        assert {c.expiration for c in ch} == {dt.date(2026, 11, 6)}
        ch[0].bid = -1  # copies: mutating a result must not touch the recording
        assert spy.option_chain("SPY", dt.date(2026, 11, 1), dt.date(2026, 11, 30))[0].bid != -1
        assert spy.underlying_quote("SPY").symbol == "SPY"
        bars = spy.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
        assert bars
        assert bars[-1].close == pytest.approx(771.35)
        assert bars[-1].timestamp.tzinfo is ET
        with pytest.raises(ValueError, match="daily"):
            spy.history_bars("SPY", dt.date(2026, 9, 1), dt.date(2026, 9, 30), "1Min")
        with pytest.raises(KeyError, match="QQQ"):
            spy.underlying_quote("QQQ")
        with pytest.raises(ValueError, match="at least one"):
            RecordedMarketData()

    def test_load_recording_roundtrip(self, tmp_path) -> None:  # noqa: ANN001
        rec = load_recording(SPY_CHAIN_FIXTURE)
        p = tmp_path / "r.json"
        p.write_text(rec.model_dump_json())
        assert load_recording(p) == rec


class TestScanRecordedSpy:
    def test_summary(self, spy_result) -> None:  # noqa: ANN001
        r = spy_result
        assert r.ticker == "SPY"
        assert r.as_of == dt.date(2026, 9, 27)
        assert r.expirations == [dt.date(2026, 10, 30), dt.date(2026, 11, 6)]  # 33d / 40d
        assert r.filter_report.total == 458
        assert r.filter_report.kept == 146
        assert r.filter_report.rejected == {
            "low_open_interest": 238,
            "low_volume": 27,
            "wide_spread": 47,
        }
        # ATM IV from the 2026-10-30 (33 DTE, nearest 30) expiry; no history → no rank
        assert r.iv.atm_iv_expiration == dt.date(2026, 10, 30)
        assert r.iv.atm_iv == pytest.approx(0.1329, abs=1e-4)
        assert r.iv.iv_rank is None
        assert r.iv.observations == 1
        assert len(r.candidates) == 16

    def test_top_candidate_hand_checked(self, spy_result) -> None:  # noqa: ANN001
        top = spy_result.candidates[0]
        assert top.rank == 1
        assert top.strategy is ScanStrategy.IRON_CONDOR
        assert [leg.occ_symbol for leg in top.structure.legs] == [
            "SPY261030P00740000",
            "SPY261030P00745000",
            "SPY261030C00797000",
            "SPY261030C00802000",
        ]
        # mids: 740P 3.74, 745P 4.455, 797C 3.105, 802C 2.15
        # credit = (4.455 - 3.74) + (3.105 - 2.15) = 0.715 + 0.955 = 1.67
        assert top.credit == pytest.approx(1.67)
        assert top.structure.net_debit_credit == D("-1.670")
        # natural = (4.44 - 3.75) + (3.04 - 2.16) = 0.69 + 0.88 = 1.57
        assert top.natural_credit == pytest.approx(1.57)
        # width 5 → cr/w 0.334; max loss (5 - 1.67) x 100 = 333
        assert top.width == 5.0
        assert top.credit_width == pytest.approx(0.334)
        assert top.structure.max_loss == D("333")
        # entry cost under config/costs.yaml (D23, one CostModel):
        # spreads 0.02 + 0.03 + 0.13 + 0.02 = 0.20/share; slippage x=0.25 → $5.00
        # fees: 4 x (ORF 0.015 + OCC 0.025 + CAT 0.0003) = 0.1612
        #       + 2 short legs TAF 0.00329 + SEC 0.0000206 x fill x 100 (4.4475, 3.0725) ≈ 0.0221
        assert top.cost == pytest.approx(5.18, abs=0.006)
        assert top.short_deltas == [0.2071, 0.2073]
        assert top.dte == 33
        # liquidity = weakest leg: min OI 733 (802C), min volume 57 (797C)
        assert top.structure.liquidity.open_interest == 733
        assert top.structure.liquidity.volume == 57
        # 797C spread 0.13 on mid 3.105 is the widest leg in % terms
        assert top.structure.liquidity.spread_pct == pytest.approx(0.13 / 3.105)

    def test_invariants(self, spy: RecordedMarketData, spy_result) -> None:  # noqa: ANN001
        quotes = {c.symbol: c for c in spy.recording("SPY").contracts}
        ranks = [c.rank for c in spy_result.candidates]
        assert ranks == list(range(1, len(ranks) + 1))
        keys = [c.credit_width for c in spy_result.candidates]
        assert keys == sorted(keys, reverse=True)
        strategies = {c.strategy for c in spy_result.candidates}
        assert strategies == set(CREDIT_STRATEGIES)  # ScanParams() default: the credit set
        for c in spy_result.candidates:
            assert_defined_risk(c.structure)
            assert 30 <= c.dte <= 45
            assert c.structure.net_debit_credit < 0
            assert c.natural_credit <= c.credit
            assert all(0.16 <= d <= 0.30 for d in c.short_deltas)
            assert 0 < c.pop < 1
            assert c.structure.greeks.theta > 0  # short premium collects theta
            for leg in c.structure.legs:
                assert check_contract(quotes[leg.occ_symbol], RULES) is None
                # every wing is ~$5 from its short
            assert c.width == pytest.approx(5.0)

    def test_rank_by_ev(self, spy: RecordedMarketData) -> None:
        r = scan(spy, "SPY", ScanParams(rank_by=RankBy.EV, top=5), as_of=dt.date(2026, 9, 27))
        evs = [c.ev_proxy for c in r.candidates]
        assert len(evs) == 5
        assert evs == sorted(evs, reverse=True)

    def test_strategy_filter_and_iv_history(self, spy: RecordedMarketData) -> None:
        hist = _hist([0.10 + 0.005 * i for i in range(30)], end=dt.date(2026, 9, 27))
        r = scan(
            spy,
            "SPY",
            ScanParams(strategies=[ScanStrategy.BULL_PUT]),
            as_of=dt.date(2026, 9, 27),
            iv_history=hist,
        )
        assert {c.strategy for c in r.candidates} == {ScanStrategy.BULL_PUT}
        for c in r.candidates:
            short, long_ = sorted(
                c.structure.legs, key=lambda leg: parse_occ(leg.occ_symbol).strike
            )[::-1]
            assert short.side.value == "short"
            assert long_.side.value == "long"
        # history 0.10..0.245, today ~0.133 → rank (0.1329-0.10)/(0.245-0.10) ≈ 0.227
        assert r.iv.iv_rank == pytest.approx((r.iv.atm_iv - 0.10) / 0.145)
        assert r.iv.observations == 31

    def test_padded_occ_symbols_from_provider(self, spy: RecordedMarketData) -> None:
        rec = spy.recording("SPY")
        padded = rec.model_copy(
            update={
                "contracts": [
                    c.model_copy(update={"symbol": parse_occ(c.symbol).format(padded=True)})
                    for c in rec.contracts
                ]
            }
        )
        r = scan(RecordedMarketData(padded), "SPY", ScanParams(), as_of=rec.as_of)
        assert len(r.candidates) == 16
        assert r.candidates[0].credit == pytest.approx(1.67)

    def test_tight_liquidity_yields_nothing(self, spy: RecordedMarketData) -> None:
        p = ScanParams(rules=LiquidityRules(min_open_interest=10**9))
        r = scan(spy, "SPY", p, as_of=dt.date(2026, 9, 27))
        assert r.candidates == []
        assert r.filter_report.kept == 0

    def test_window_with_no_expiry(self, spy: RecordedMarketData) -> None:
        r = scan(spy, "SPY", ScanParams(dte_min=1, dte_max=5), as_of=dt.date(2026, 9, 27))
        assert r.expirations == []
        assert r.candidates == []
        assert r.iv.atm_iv is None


# ---------------------------------------------------------------------------
# Scoring on a synthetic flat-vol BSM chain
# ---------------------------------------------------------------------------

SIG, R, SPOT = 0.20, 0.04, 100.0
SYN_ASOF = dt.date(2026, 9, 28)
SYN_EXP = SYN_ASOF + dt.timedelta(days=35)
HALF = 0.01  # half-spread on every contract


class _Flat:
    """MarketDataProvider whose chain is priced exactly by flat-vol BSM."""

    def option_chain(self, underlying: str, exp_start: dt.date, exp_end: dt.date):  # noqa: ANN201
        t = 35 / 365
        out = []
        for k in range(80, 121):
            for kind in (OptionKind.PUT, OptionKind.CALL):
                inp = BSMInputs(S=SPOT, K=k, t=t, r=R, sigma=SIG, flag=kind)
                p = price(inp)
                if p < 0.05:
                    continue
                out.append(
                    OptionContract(
                        symbol=format_occ("SYN", SYN_EXP, kind, k),
                        underlying="SYN",
                        expiration=SYN_EXP,
                        strike=float(k),
                        option_type="put" if kind is OptionKind.PUT else "call",
                        bid=p - HALF,
                        ask=p + HALF,
                        mid=p,
                        open_interest=1000,
                        volume=100,
                        implied_volatility=SIG,
                        greeks=OptionGreeks(delta=delta(inp)),
                    )
                )
        return out

    def underlying_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            bid=SPOT,
            ask=SPOT,
            mid=SPOT,
            timestamp=dt.datetime(2026, 9, 28, tzinfo=ET),
        )

    def history_bars(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return []


@pytest.fixture(scope="module")
def res():  # noqa: ANN201
    return scan(_Flat(), "SYN", ScanParams(risk_free_rate=R), as_of=SYN_ASOF)


class TestScoring:
    def test_ev_is_minus_cost_when_market_is_flat_vol(self, res) -> None:  # noqa: ANN001
        # Mids equal the flat-vol model up to the 4dp premium rounding, so EV ≈ -cost.
        # cost = entry cost under the shared CostModel (config/costs.yaml):
        # x·spread per leg + that leg's entry fees (TAF/SEC on the legs sold).
        cm = load_cost_model()
        assert res.candidates
        assert res.iv.atm_iv == pytest.approx(SIG)
        for c in res.candidates:
            n = len(c.structure.legs)
            want = 0.0
            for leg in c.structure.legs:
                side = -1 if leg.side.value == "short" else 1
                fill = cm.fill(float(leg.premium), 2 * HALF, side)
                want += cm.slippage_frac * 2 * HALF * 100 + cm.trade_fees(1, side, fill)
            assert c.cost == pytest.approx(want, abs=0.005)  # ScanCandidate rounds to cents
            assert c.ev_proxy == pytest.approx(-c.cost, abs=0.05 * n)
            assert c.natural_credit == pytest.approx(c.credit - HALF * n, abs=1e-3)

    def test_cost_matches_old_half_spread_at_x_half_and_no_fees(self) -> None:
        # x = 0.5 (fill at the touch) and zero fees reproduce the pre-D23 half-spread cost.
        touch = CostModel(slippage_frac=0.5, commission_per_contract=0.0)
        r = scan(_Flat(), "SYN", ScanParams(risk_free_rate=R, cost=touch), as_of=SYN_ASOF)
        for c in r.candidates:
            assert c.cost == pytest.approx(len(c.structure.legs) * HALF * 100)

    def test_pop_matches_closed_form(self, res) -> None:  # noqa: ANN001
        t = 35 / 365

        def p_above(x: float) -> float:
            d2 = (math.log(SPOT / x) + (R - SIG * SIG / 2) * t) / (SIG * math.sqrt(t))
            return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))

        for c in res.candidates:
            bes = sorted(float(b) for b in c.structure.breakevens)
            if c.strategy is ScanStrategy.BULL_PUT:
                want = p_above(bes[0])
            elif c.strategy is ScanStrategy.BEAR_CALL:
                want = 1 - p_above(bes[0])
            else:
                want = p_above(bes[0]) - p_above(bes[1])
            assert c.pop == pytest.approx(want, abs=1e-4)

    @settings(max_examples=15, deadline=None)
    @given(
        target=st.floats(0.16, 0.30),
        width=st.sampled_from([2.0, 3.0, 5.0]),
    )
    def test_params_respected(self, target: float, width: float) -> None:
        p = ScanParams(target_delta=target, wing_width=width, risk_free_rate=R, top=4)
        r = scan(_Flat(), "SYN", p, as_of=SYN_ASOF)
        assert len(r.candidates) <= 4
        for c in r.candidates:
            assert all(0.16 <= d <= 0.30 for d in c.short_deltas)
            assert width / 2 <= c.width <= 2 * width
            assert c.credit < c.width


class TestParams:
    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="dte_max"):
            ScanParams(dte_min=45, dte_max=30)
        with pytest.raises(ValueError, match="delta_max"):
            ScanParams(delta_min=0.3, delta_max=0.2, target_delta=0.25)
        with pytest.raises(ValueError, match="outside"):
            ScanParams(target_delta=0.40)
        with pytest.raises(ValueError, match="strategy"):
            ScanParams(strategies=[])

    def test_from_settings(self) -> None:
        s = get_settings(scanner_wing_width=10.0, dte_min=21, dte_max=50, account_profile="margin")
        p = ScanParams.from_settings(s, target_delta=0.25, dte_min=None)
        assert p.strategies == list(CREDIT_STRATEGIES)  # margin profile's stance map
        assert p.wing_width == 10.0
        assert p.target_delta == 0.25
        assert (p.dte_min, p.dte_max) == (21, 50)  # None override keeps the setting
        assert p.rules.spread_max_abs == 0.10


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_scan_fixture_table(self, capsys, tmp_path) -> None:  # noqa: ANN001
        from arc.cli import main

        rc = main(
            [
                "chains", "SPY", "--dte", "30-45", "--delta", "20", "--fixture", "spy",
                "--iv-history-dir", str(tmp_path), "--top", "3", "--profile", "margin",
            ]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0
        assert "SPY spot 771.34" in out
        assert "IVR n/a" in out
        assert "1  iron_condor     2026-10-30  33d  740/745/797/802" in out
        assert out.count("iron_condor") == 3

    def test_scan_fixture_json_and_record_iv(self, capsys, tmp_path) -> None:  # noqa: ANN001
        from arc.cli import main

        args = ["chains", "SPY", "--fixture", str(SPY_CHAIN_FIXTURE), "--json"]
        args += ["--iv-history-dir", str(tmp_path), "--record-iv", "--delta", "0.25"]
        args += ["--strategy", "bear_call", "--rank-by", "ev", "--width", "3"]
        assert main(args) == 0
        payload = json.loads(capsys.readouterr().out)
        (res,) = payload
        assert res["params"]["target_delta"] == 0.25
        assert res["params"]["wing_width"] == 3.0
        assert {c["strategy"] for c in res["candidates"]} == {"bear_call"}
        hist = load_iv_history(tmp_path, "SPY")
        assert list(hist) == [dt.date(2026, 9, 27)]
        assert hist[dt.date(2026, 9, 27)] == pytest.approx(res["iv"]["atm_iv"], abs=1e-6)

    @pytest.mark.parametrize(
        "bad", [["--dte", "45-30"], ["--dte", "x"], ["--delta", "abc"], ["--delta", "150"]]
    )
    def test_bad_args_exit_2(self, bad: list[str]) -> None:
        from arc.cli import main

        with pytest.raises(SystemExit) as ei:
            main(["chains", "SPY", "--fixture", "spy", *bad])
        assert ei.value.code == 2

    def test_delta_outside_band_and_unknown_ticker(self, capsys) -> None:  # noqa: ANN001
        from arc.cli import main

        assert main(["chains", "SPY", "--fixture", "spy", "--delta", "40"]) == 2
        assert "outside the short-strike band" in capsys.readouterr().err
        assert main(["chains", "QQQ", "--fixture", "spy"]) == 2
        assert "no recording for 'QQQ'" in capsys.readouterr().err

    def test_live_without_keys_is_a_clean_error(self, capsys, monkeypatch) -> None:  # noqa: ANN001
        from arc.cli import main

        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        assert main(["chains", "SPY"]) == 2
        assert "--fixture spy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Alpaca adapter: OI + volume enrichment (mocked, no network)
# ---------------------------------------------------------------------------


class TestAlpacaEnrichment:
    @patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    def test_open_interest_and_volume_filled(self) -> None:
        from arc.data.alpaca import AlpacaMarketData

        sym = "SPY261030P00745000"
        snap = MagicMock()
        snap.latest_quote.bid_price = 4.44
        snap.latest_quote.ask_price = 4.47
        snap.latest_quote.timestamp = dt.datetime.now(tz=ET)
        snap.latest_trade = None
        snap.greeks.delta = -0.2
        snap.implied_volatility = 0.16
        option = MagicMock()
        option.get_option_chain.return_value = {sym: snap}

        page1 = MagicMock(option_contracts=[MagicMock(symbol=sym, open_interest="21384")])
        page1.next_page_token = "p2"
        page2 = MagicMock(
            option_contracts=[MagicMock(symbol="SPY261030P00740000", open_interest=None)],
            next_page_token=None,
        )
        contracts = MagicMock()
        contracts.get_option_contracts.side_effect = [page1, page2]
        raw = MagicMock()
        raw.get_option_chain.return_value = {
            sym: {"dailyBar": {"v": 562}},
            "SPY261030P00740000": {"prevDailyBar": {"v": 7}},
            "SPY261030P00735000": {},
        }

        md = AlpacaMarketData(
            option_client=option,
            stock_client=MagicMock(),
            contracts_client=contracts,
            raw_option_client=raw,
        )
        (c,) = md.option_chain("SPY", dt.date(2026, 10, 27), dt.date(2026, 11, 11))
        assert c.open_interest == 21384
        assert c.volume == 562
        assert contracts.get_option_contracts.call_count == 2
        second = contracts.get_option_contracts.call_args_list[1].args[0]
        assert second.page_token == "p2"
        assert second.underlying_symbols == ["SPY"]

    @patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    def test_injected_option_client_skips_enrichment(self) -> None:
        from arc.data.alpaca import AlpacaMarketData

        option = MagicMock()
        option.get_option_chain.return_value = {}
        md = AlpacaMarketData(option_client=option, stock_client=MagicMock())
        assert md.option_chain("SPY", dt.date(2026, 10, 1), dt.date(2026, 10, 30)) == []
        assert option.get_option_chain.call_count == 1


# ---------------------------------------------------------------------------
# Debit strategies (D25, E3.4)
# ---------------------------------------------------------------------------


class TestDebitStrategies:
    AS_OF = dt.date(2026, 9, 27)

    def _scan(self, spy: RecordedMarketData, *strategies: ScanStrategy, **kw: object):  # noqa: ANN202
        params = ScanParams(strategies=list(strategies), top=50, **kw)  # type: ignore[arg-type]
        return scan(spy, "SPY", params, as_of=self.AS_OF)

    def test_bull_call_debit(self, spy: RecordedMarketData) -> None:
        r = self._scan(spy, ScanStrategy.BULL_CALL_DEBIT)
        assert r.candidates
        for c in r.candidates:
            assert c.strategy is ScanStrategy.BULL_CALL_DEBIT
            long_leg, short_leg = c.structure.legs
            assert long_leg.side.value == "long" and short_leg.side.value == "short"
            lk = parse_occ(long_leg.occ_symbol).strike
            sk = parse_occ(short_leg.occ_symbol).strike
            assert lk < sk  # short is further OTM
            assert c.structure.net_debit_credit > 0
            assert c.credit < 0 and c.credit_width is None
            assert 0.40 <= c.long_deltas[0] <= 0.70
            assert 0.20 <= c.short_deltas[0] <= 0.35
            assert float(c.structure.max_loss) == pytest.approx(
                float(c.structure.net_debit_credit) * 100
            )
            assert c.ev_ratio == pytest.approx(c.ev_proxy / float(c.structure.max_loss), abs=1e-3)
            assert_defined_risk(c.structure)
        ratios = [c.ev_ratio for c in r.candidates]
        assert ratios == sorted(ratios, reverse=True)

    def test_bear_put_debit(self, spy: RecordedMarketData) -> None:
        r = self._scan(spy, ScanStrategy.BEAR_PUT_DEBIT)
        assert r.candidates
        for c in r.candidates:
            long_leg, short_leg = c.structure.legs
            assert parse_occ(long_leg.occ_symbol).kind.value == "p"
            assert parse_occ(long_leg.occ_symbol).strike > parse_occ(short_leg.occ_symbol).strike
            assert c.structure.net_debit_credit > 0

    def test_long_singles(self, spy: RecordedMarketData) -> None:
        r = self._scan(spy, ScanStrategy.LONG_CALL, ScanStrategy.LONG_PUT)
        assert {c.strategy for c in r.candidates} == {ScanStrategy.LONG_CALL, ScanStrategy.LONG_PUT}
        for c in r.candidates:
            assert len(c.structure.legs) == 1 and c.width is None and c.short_deltas == []
            assert 0.40 <= c.long_deltas[0] <= 0.70
            assert float(c.structure.max_loss) == pytest.approx(
                float(c.structure.net_debit_credit) * 100
            )

    def test_debit_width_target(self, spy: RecordedMarketData) -> None:
        r = self._scan(spy, ScanStrategy.BULL_CALL_DEBIT, debit_width=10.0)
        assert r.candidates
        assert all(5.0 <= (c.width or 0) <= 20.0 for c in r.candidates)

    def test_mixed_credit_first_then_debit(self, spy: RecordedMarketData) -> None:
        r = self._scan(spy, ScanStrategy.BULL_PUT, ScanStrategy.LONG_CALL)
        kinds = [c.credit_width is None for c in r.candidates]
        assert kinds == sorted(kinds)  # credit (False) before debit (True)
        ev = self._scan(spy, ScanStrategy.BULL_PUT, ScanStrategy.LONG_CALL, rank_by=RankBy.EV)
        evs = [c.ev_proxy for c in ev.candidates]
        assert evs == sorted(evs, reverse=True)

    def test_bad_debit_band_rejected(self) -> None:
        with pytest.raises(ValueError, match="long target delta"):
            ScanParams(long_target_delta=0.9)

    def test_profile_sets_default_strategies(self) -> None:
        s = get_settings(account_profile="cash_debit")
        p = ScanParams.from_settings(s)
        assert p.strategies == [
            ScanStrategy.BULL_CALL_DEBIT,
            ScanStrategy.LONG_CALL,
            ScanStrategy.BEAR_PUT_DEBIT,
            ScanStrategy.LONG_PUT,
        ]
        assert (p.dte_min, p.dte_max) == (30, 60)

    def test_cli_debit_table(self, capsys, tmp_path) -> None:  # noqa: ANN001
        from arc.cli import main

        rc = main(
            ["chains", "SPY", "--fixture", "spy", "--iv-history-dir", str(tmp_path), "--top", "3",
             "--profile", "cash_debit"]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0
        assert "profile cash_debit" in out
        assert "db " in out and "ev/L" in out
        assert "iron_condor" not in out
        assert main(["chains", "SPY", "--fixture", "spy", "--profile", "nope"]) == 2
