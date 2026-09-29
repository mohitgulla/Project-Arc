"""D24 price band + ``arc2`` band token + band/closing gate rules + hook (E6.2).

Part of ``make test-gate`` (100% branch coverage on ``arc.gate``).

Baseline (same as tests/test_gate.py): SPY 570/565 bull put x2, quotes
565P 1.20/1.30, 570P 2.05/2.15 -> combo NBBO [-0.95, -0.75] per share. Mid
limit -0.85 credit; far touch for the buyer of the combo = -0.75. With N = 3 and
reach 1.0 the band is [-0.85, -0.75] and the ladder -0.85, -0.82, -0.79, -0.75.
Worst-price max loss = (5 - 0.75) x 100 x 2 = $850.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.gate import Portfolio, RuleCode, derive, proposal_hash
from arc.gate import rules as R
from arc.gate import token as T
from arc.gate.band import MAX_BAND_STEPS, PriceBand, band_from_nbbo
from arc.gate.hook_policy import check_tool_call
from arc.gate.token import (
    BandToken,
    TokenError,
    TokenErrorCode,
    client_order_id,
    issue_token,
    mint_band,
    order_payload,
    parse_any,
    verify_any,
    verify_band,
    verify_client_order_id,
)
from arc.models import GateDecision
from arc.structures import debit_vertical
from tests import test_gate as G

SECRET = b"s" * 32
OTHER = b"o" * 32
NOW = G.NOW
TICK = D("0.01")
BAND = PriceBand(lo=D("-0.85"), hi=D("-0.75"), max_steps=3)
TOOL = "mcp__alpaca__place_option_order"


def cfg(**kw: object):
    return G.cfg(**kw)


def code_of(exc: pytest.ExceptionInfo[TokenError]) -> TokenErrorCode:
    return exc.value.code


def passed(p) -> GateDecision:
    return GateDecision(proposal_hash=proposal_hash(p), passed=True)


def band_token(p=None, band: PriceBand = BAND, **kw: object) -> str:
    p = p or G.make_proposal()
    base: dict[str, object] = {
        "order": order_payload(p),
        "band": band,
        "secret": SECRET,
        "expires_at": p.expires_at,
        "now": NOW,
    }
    base.update(kw)
    return mint_band(proposal_hash(p), passed(p), **base)  # type: ignore[arg-type]


def at(price: str, p=None):
    p = p or G.make_proposal()
    return order_payload(p).model_copy(update={"limit_price": D(price)})


# ---------------------------------------------------------------------------
# PriceBand / band_from_nbbo
# ---------------------------------------------------------------------------


class TestPriceBand:
    def test_ladder_three_steps(self) -> None:
        assert BAND.ladder(TICK) == (D("-0.85"), D("-0.82"), D("-0.79"), D("-0.75"))
        assert BAND.attempts == 4
        assert BAND.contains(D("-0.80")) and not BAND.contains(D("-0.74"))

    def test_zero_steps_single_attempt(self) -> None:
        b = PriceBand(lo=D("1.00"), hi=D("1.00"), max_steps=0)
        assert b.ladder(TICK) == (D("1.00"),)

    def test_bad_tick(self) -> None:
        with pytest.raises(ValueError, match="tick"):
            BAND.ladder(D(0))

    def test_hi_below_lo_invalid(self) -> None:
        with pytest.raises(ValueError, match="below"):
            PriceBand(lo=D("1.00"), hi=D("0.99"), max_steps=1)

    def test_sub_cent_invalid(self) -> None:
        with pytest.raises(ValueError, match="cents"):
            PriceBand(lo=D("1.001"), hi=D("1.10"), max_steps=1)

    def test_from_nbbo(self) -> None:
        b = band_from_nbbo(D("-0.85"), D("-0.75"), max_steps=3, reach=D(1), tick=TICK)
        assert b == BAND
        half = band_from_nbbo(D("-0.85"), D("-0.75"), max_steps=3, reach=D("0.5"), tick=TICK)
        assert half.hi == D("-0.80")

    def test_from_nbbo_no_room(self) -> None:
        b = band_from_nbbo(D("1.00"), D("0.95"), max_steps=3, reach=D(1), tick=TICK)
        assert (b.lo, b.hi, b.max_steps) == (D("1.00"), D("1.00"), 0)

    @pytest.mark.parametrize(("tick", "reach"), [(D(0), D(1)), (TICK, D("1.5")), (TICK, D(-1))])
    def test_from_nbbo_bad_args(self, tick: D, reach: D) -> None:
        with pytest.raises(ValueError, match="invalid"):
            band_from_nbbo(D(1), D(2), max_steps=1, reach=reach, tick=tick)

    @given(
        st.integers(-50_000, 50_000),
        st.integers(0, 5_000),
        st.integers(0, MAX_BAND_STEPS),
        st.sampled_from([D("0.01"), D("0.05")]),
    )
    def test_property_ladder_monotone_inside_band(
        self, lo_c: int, width_c: int, n: int, tick: D
    ) -> None:
        b = PriceBand(lo=D(lo_c) / 100, hi=D(lo_c + width_c) / 100, max_steps=n)
        ladder = b.ladder(tick)
        assert len(ladder) == n + 1
        assert ladder[0] == b.lo and ladder[-1] == (b.hi if n else b.lo)
        assert all(b.contains(x) for x in ladder)
        assert list(ladder) == sorted(ladder)


# ---------------------------------------------------------------------------
# PriceBand.reanchor (D34 stale-quote re-price; never widens)
# ---------------------------------------------------------------------------

RB = PriceBand(lo=D("-0.85"), hi=D("-0.76"), max_steps=3)


class TestReanchor:
    def test_inside_band_moves_start_keeps_hi(self) -> None:
        nb = RB.reanchor(D("-0.805"), TICK)
        assert nb is not None
        assert nb.lo == D("-0.80") and nb.hi == RB.hi and nb.max_steps == 3

    def test_beyond_hi_returns_none(self) -> None:
        assert RB.reanchor(D("-0.75"), TICK) is None
        assert RB.reanchor(D("0.10"), TICK) is None

    def test_below_lo_clamps_to_lo(self) -> None:
        nb = RB.reanchor(D("-0.95"), TICK)
        assert nb is not None and nb.lo == RB.lo and nb.hi == RB.hi

    def test_at_hi_single_attempt(self) -> None:
        nb = RB.reanchor(RB.hi, TICK)
        assert nb is not None and nb.lo == nb.hi == RB.hi and nb.max_steps == 0

    def test_bad_tick(self) -> None:
        with pytest.raises(ValueError, match="tick"):
            RB.reanchor(D("-0.8"), D("0"))

    @given(
        lo=st.decimals(min_value=-5, max_value=5, places=2),
        width=st.decimals(min_value=0, max_value=2, places=2),
        mid=st.decimals(min_value=-8, max_value=8, places=4),
        steps=st.integers(min_value=0, max_value=5),
    )
    def test_never_widens(self, lo: D, width: D, mid: D, steps: int) -> None:
        band = PriceBand(lo=lo, hi=lo + width, max_steps=steps)
        nb = band.reanchor(mid, TICK)
        if nb is None:
            assert mid > band.hi or (mid / TICK).to_integral_value() * TICK > band.hi
            return
        assert band.lo <= nb.lo <= nb.hi == band.hi
        for p in nb.ladder(TICK):
            assert band.contains(p)


# ---------------------------------------------------------------------------
# Gate rules: combo NBBO, band, worst-case, closing
# ---------------------------------------------------------------------------


class TestBandRules:
    def test_combo_nbbo(self) -> None:
        legs = G.bull_put().legs
        assert R.combo_nbbo(legs, G.mkt()) == (D("-0.95"), D("-0.75"))

    def test_combo_nbbo_missing_or_crossed(self) -> None:
        legs = G.bull_put().legs
        assert R.combo_nbbo(legs, G.mkt(quotes={G.LP: G.quote("1.20", "1.30")})) is None
        crossed = {G.LP: G.quote("1.40", "1.30"), G.SP: G.quote("2.05", "2.15")}
        assert R.combo_nbbo(legs, G.mkt(quotes=crossed)) is None

    def test_price_band_sub_cent_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="whole cents"):
            R.price_band(G.bull_put().legs, D("-0.855"), G.mkt(), cfg())

    def test_price_band_default(self) -> None:
        assert R.proposal_band(G.make_proposal(), G.mkt(), cfg()) == BAND

    def test_price_band_uses_explicit_limit(self) -> None:
        p = G.make_proposal(limit_price=D("-0.84"))
        assert R.proposal_band(p, G.mkt(), cfg()).lo == D("-0.84")

    def test_price_band_no_nbbo_or_off_tick_is_single_attempt(self) -> None:
        legs = G.bull_put().legs
        no_q = R.price_band(legs, D("-0.85"), G.mkt(quotes={}), cfg())
        assert (no_q.hi, no_q.max_steps) == (D("-0.85"), 0)
        off = R.price_band(legs, D("-0.85"), G.mkt(), cfg(limit_tick=0.05))
        assert off.max_steps == 3  # -0.85 is on a 0.05 tick
        off2 = R.price_band(legs, D("-0.84"), G.mkt(), cfg(limit_tick=0.05))
        assert (off2.hi, off2.max_steps) == (D("-0.84"), 0)

    def test_evaluate_with_band_passes(self) -> None:
        d = R.evaluate(
            G.make_proposal(), G.acct(), Portfolio(), cfg(), market=G.mkt(), now=NOW, band=BAND
        )
        assert d.passed, d.violations

    def test_band_must_start_at_limit(self) -> None:
        bad = PriceBand(lo=D("-0.84"), hi=D("-0.75"), max_steps=3)
        d = R.evaluate(
            G.make_proposal(), G.acct(), Portfolio(), cfg(), market=G.mkt(), now=NOW, band=bad
        )
        assert G.codes(d) == [RuleCode.BAND.value]
        assert "not at the limit" in d.violations[0]

    def test_band_steps_over_config(self) -> None:
        d = R.evaluate(
            G.make_proposal(),
            G.acct(),
            Portfolio(),
            cfg(execution_improvement_steps=2),
            market=G.mkt(),
            now=NOW,
            band=BAND,
        )
        assert G.codes(d) == [RuleCode.BAND.value]
        assert "3 steps > max 2" in d.violations[0]

    def test_band_worst_price_outside_nbbo(self) -> None:
        wide = PriceBand(lo=D("-0.85"), hi=D("-0.70"), max_steps=3)
        d = R.evaluate(
            G.make_proposal(), G.acct(), Portfolio(), cfg(), market=G.mkt(), now=NOW, band=wide
        )
        assert G.codes(d) == [RuleCode.BAND.value]
        assert "worst price" in d.violations[0]

    def test_band_ignores_spread_width_violations(self) -> None:
        """Only NBBO/tick violations at the worst price count as band violations."""
        wide_q = {G.LP: G.quote("1.00", "1.50"), G.SP: G.quote("1.85", "2.35")}
        band = PriceBand(lo=D("-0.85"), hi=D("-0.35"), max_steps=3)
        d = R.evaluate(
            G.make_proposal(),
            G.acct(),
            Portfolio(),
            cfg(),
            market=G.mkt(quotes=wide_q),
            now=NOW,
            band=band,
        )
        assert RuleCode.BAND.value not in G.codes(d)
        assert RuleCode.SPREAD.value in G.codes(d)

    def test_worst_case_max_loss_hits_cap(self) -> None:
        """$4,980 at mid passes 5% of $100k; at the band's worst price it does not."""
        p = G.make_proposal(sizing=G.Sizing(contracts=12, notional=D("1"), pct_equity=0.05))
        ok = R.evaluate(p, G.acct(), Portfolio(), cfg(), market=G.mkt(), now=NOW)
        assert ok.passed, ok.violations
        d = R.evaluate(p, G.acct(), Portfolio(), cfg(), market=G.mkt(), now=NOW, band=BAND)
        assert G.codes(d) == [RuleCode.PER_UNDERLYING.value]

    def test_worst_case_unbounded_stays_none(self) -> None:
        d = derive(G.make_proposal())._replace(max_loss_total=None)
        assert R.worst_case(d, BAND, 2).max_loss_total is None
        dd = derive(G.make_proposal())
        assert R.worst_case(dd, BAND, 2).max_loss_total == D("850")


# ---------------------------------------------------------------------------
# Max gain must stay > 0 at every price the band can reach (review round 1)
# ---------------------------------------------------------------------------

EXP_R = G.dt.date(2026, 10, 30)
C736 = G.format_occ("SPY", EXP_R, "call", 736)
C737 = G.format_occ("SPY", EXP_R, "call", 737)
# Live paper quotes that produced a +2.79 fill on a $1-wide call spread (10:41 ET).
REVIEW_Q = {C736: G.quote("36.77", "38.48"), C737: G.quote("35.97", "36.30")}


def _call_spread(long_p: str = "37.625", short_p: str = "36.135"):
    return debit_vertical(
        "call",
        "SPY",
        EXP_R,
        long_strike=736,
        long_premium=long_p,
        short_strike=737,
        short_premium=short_p,
        as_of=G.AS_OF,
        market=G.MarketInputs(spot=772.0, r=0.04, ivs={C736: 0.2, C737: 0.2}),
    )


class TestMaxGain:
    def test_price_ceiling(self) -> None:
        assert R.price_ceiling(_call_spread().legs) == D("1.00")  # the width
        assert R.price_ceiling(G.bull_put().legs) == D("0")  # credit must stay > 0
        assert R.price_ceiling(G.long_call("SPY", EXP_R, 736, "37.6").legs) is None

    def test_max_gain_cap(self) -> None:
        assert R.max_gain_cap(_call_spread().legs, TICK) == D("0.99")
        assert R.max_gain_cap(_call_spread().legs, D("0.05")) == D("0.95")
        assert R.max_gain_cap(G.bull_put().legs, TICK) == D("-0.01")
        assert R.max_gain_cap(G.long_call("SPY", EXP_R, 736, "37.6").legs, TICK) is None

    def test_regression_review_quotes_band_capped_below_width(self) -> None:
        """SPY 736/737 C, combo NBBO [0.47, 2.51]: the far touch is past the $1 width."""
        legs = _call_spread().legs
        assert R.combo_nbbo(legs, G.mkt(quotes=REVIEW_Q)) == (D("0.47"), D("2.51"))
        band = R.price_band(legs, D("0.60"), G.mkt(quotes=REVIEW_Q), cfg())
        assert band.hi == D("0.99") and band.max_steps == 3
        assert all(p < D("1.00") for p in band.ladder(TICK))

    def test_regression_review_band_is_refused(self) -> None:
        """The exact band from the review (1.99 -> 3.75) fails the gate at mid and worst."""
        p = G.make_proposal(
            structure=_call_spread(),
            limit_price=D("1.99"),
            sizing=G.Sizing(contracts=1, notional=D("199"), pct_equity=0.002),
        )
        bad = PriceBand(lo=D("1.99"), hi=D("3.75"), max_steps=3)
        d = R.evaluate(
            p, G.acct(), Portfolio(), cfg(), market=G.mkt(quotes=REVIEW_Q), now=NOW, band=bad
        )
        assert not d.passed
        assert RuleCode.NO_MAX_GAIN.value in G.codes(d)
        assert any("worst price" in v and "max gain" in v for v in d.violations)

    def test_mid_at_or_above_width_has_no_band(self) -> None:
        """When even the mid leaves no max gain, the gate fails (no band can fix it)."""
        legs = _call_spread().legs
        band = R.price_band(legs, D("1.20"), G.mkt(quotes=REVIEW_Q), cfg())
        assert (band.lo, band.hi, band.max_steps) == (D("1.20"), D("1.20"), 0)
        p = G.make_proposal(
            structure=_call_spread(),
            limit_price=D("1.00"),
            sizing=G.Sizing(contracts=1, notional=D("100"), pct_equity=0.001),
        )
        d = R.evaluate(p, G.acct(), Portfolio(), cfg(), market=G.mkt(quotes=REVIEW_Q), now=NOW)
        assert RuleCode.NO_MAX_GAIN.value in G.codes(d)

    def test_credit_band_never_reaches_zero_credit(self) -> None:
        """Bull put with a far touch at +0.10: the band stops at -0.01 (still a credit)."""
        wide = {G.LP: G.quote("1.20", "2.20"), G.SP: G.quote("2.10", "2.15")}
        band = R.price_band(G.bull_put().legs, D("-0.85"), G.mkt(quotes=wide), cfg())
        assert band.hi == D("-0.01")

    def test_check_max_gain_passes_inside(self) -> None:
        assert R.check_max_gain(derive(G.make_proposal())) == []

    @given(
        st.integers(1, 20),  # width in $
        st.integers(1, 5_000),  # mid, cents
        st.integers(0, 10_000),  # far touch beyond mid, cents
        st.integers(0, MAX_BAND_STEPS),
        st.sampled_from([D("0.01"), D("0.05")]),
        st.sampled_from([D("1"), D("0.5")]),
    )
    def test_property_every_ladder_price_keeps_max_gain(
        self, width: int, mid_c: int, extra_c: int, n: int, tick: D, reach: D
    ) -> None:
        """Every ladder price the band allows leaves max gain > 0 whenever the band
        steps at all; and the gate passes the band's worst price only if it does."""
        mid = D(mid_c) / 100
        mid = (mid / tick).to_integral_value() * tick
        legs = debit_vertical(
            "call",
            "SPY",
            EXP_R,
            long_strike=700,
            long_premium="50",
            short_strike=700 + width,
            short_premium="45",
            as_of=G.AS_OF,
        ).legs
        cap = R.max_gain_cap(legs, tick)
        assert cap is not None
        band = band_from_nbbo(
            mid, mid + D(extra_c) / 100, max_steps=n, reach=reach, tick=tick, cap=cap
        )
        ceiling = R.price_ceiling(legs)
        assert ceiling is not None and ceiling == D(width)
        if mid < ceiling:
            assert all(p < ceiling for p in band.ladder(tick))
        else:
            assert band.max_steps == 0 and band.hi == mid


class TestClosing:
    def _close(self):
        """Closing the baseline bull put = buying back a 570/565 bear put (debit 0.85)."""
        st_ = debit_vertical(
            "put",
            "SPY",
            G.EXP,
            long_strike=570,
            long_premium="0.85",
            short_strike=565,
            short_premium="0.075",
            as_of=G.AS_OF,
        )
        return G.make_proposal(structure=st_, limit_price=D("0.78"))

    def test_close_structure(self) -> None:
        p = self._close()
        assert {(leg.occ_symbol, leg.side) for leg in p.structure.legs} == {
            (G.SP, "long"),
            (G.LP, "short"),
        }

    def test_closing_reduces_held_legs(self) -> None:
        p = self._close()
        pf = Portfolio(legs={G.SP: -2, G.LP: 2})
        assert R.check_closing(p, pf) == []

    def test_closing_mismatch(self) -> None:
        p = self._close()
        out = R.check_closing(p, Portfolio(legs={G.SP: -1, G.LP: 0}))
        assert [v.code for v in out] == [RuleCode.CLOSE_MISMATCH, RuleCode.CLOSE_MISMATCH]
        assert "buy 2" in out[0].detail and "sell 2" in out[1].detail

    def test_evaluate_closing_skips_opening_rules(self) -> None:
        """Daily-loss entry block, position count and whitelist don't stop an exit."""
        p = self._close()
        pf = Portfolio(
            legs={G.SP: -2, G.LP: 2},
            positions=[G.Position(underlying="SPY", max_loss=D("830"))],
        )
        acct = G.acct(last_equity=D("200000"))  # daily loss far past the limit
        close_q = {G.SP: G.quote("0.80", "0.90"), G.LP: G.quote("0.05", "0.10")}
        band = R.proposal_band(p, G.mkt(quotes=close_q), cfg())
        d = R.evaluate(
            p,
            acct,
            pf,
            cfg(max_open_positions=1),
            market=G.mkt(quotes=close_q),
            now=NOW,
            band=band,
            closing=True,
        )
        assert d.passed, d.violations
        opening = R.evaluate(p, acct, pf, cfg(), market=G.mkt(quotes=close_q), now=NOW)
        assert not opening.passed

    def test_evaluate_closing_still_halts(self) -> None:
        p = self._close()
        pf = Portfolio(legs={G.SP: -2, G.LP: 2})
        close_q = {G.SP: G.quote("0.80", "0.90"), G.LP: G.quote("0.05", "0.10")}
        d = R.evaluate(
            p, G.acct(halted=True), pf, cfg(), market=G.mkt(quotes=close_q), now=NOW, closing=True
        )
        assert RuleCode.HALTED.value in G.codes(d)


# ---------------------------------------------------------------------------
# arc2 token
# ---------------------------------------------------------------------------


class TestArc2Token:
    def test_round_trip(self) -> None:
        p = G.make_proposal()
        tok = band_token(p)
        assert tok.startswith("arc2.") and len(client_order_id(tok, 9)) <= 128
        t = verify_band(tok, secret=SECRET, now=NOW, proposal_hash=proposal_hash(p))
        assert t.band == BAND and t.expires_at == p.expires_at
        assert isinstance(parse_any(tok), BandToken)
        arc1 = issue_token(passed(p), p, secret=SECRET, now=NOW).token
        assert not isinstance(parse_any(arc1), BandToken)
        assert isinstance(verify_any(tok, secret=SECRET, now=NOW), BandToken)
        for price in BAND.ladder(TICK):
            verify_band(tok, secret=SECRET, now=NOW, order=at(str(price)), step=3)

    def test_issue_token_with_band(self) -> None:
        p = G.make_proposal()
        d = issue_token(passed(p), p, secret=SECRET, now=NOW, band=BAND)
        assert d.token is not None and d.token.startswith("arc2.")

    @pytest.mark.parametrize("price", ["-0.86", "-0.74"])
    def test_out_of_band(self, price: str) -> None:
        with pytest.raises(TokenError) as e:
            verify_band(band_token(), secret=SECRET, now=NOW, order=at(price))
        assert code_of(e) is TokenErrorCode.OUT_OF_BAND

    def test_other_legs_or_qty(self) -> None:
        other = order_payload(G.make_proposal()).model_copy(update={"qty": 3})
        with pytest.raises(TokenError) as e:
            verify_band(band_token(), secret=SECRET, now=NOW, order=other)
        assert code_of(e) is TokenErrorCode.ORDER_MISMATCH

    def test_other_proposal(self) -> None:
        other = proposal_hash(G.make_proposal(thesis="x"))
        with pytest.raises(TokenError) as e:
            verify_band(band_token(), secret=SECRET, now=NOW, proposal_hash=other)
        assert code_of(e) is TokenErrorCode.PROPOSAL_MISMATCH

    def test_step_out_of_range(self) -> None:
        with pytest.raises(TokenError) as e:
            verify_band(band_token(), secret=SECRET, now=NOW, step=4)
        assert code_of(e) is TokenErrorCode.BAD_STEP
        with pytest.raises(TokenError):
            client_order_id(band_token(), 10)

    def test_wrong_secret_and_widened_band(self) -> None:
        tok = band_token()
        with pytest.raises(TokenError) as e:
            verify_band(tok, secret=OTHER, now=NOW)
        assert code_of(e) is TokenErrorCode.BAD_SIGNATURE
        widened = tok.replace(".-75.", ".-70.")
        assert widened != tok
        with pytest.raises(TokenError) as e:
            verify_band(widened, secret=SECRET, now=NOW)
        assert code_of(e) is TokenErrorCode.BAD_SIGNATURE

    def test_expired(self) -> None:
        p = G.make_proposal()
        with pytest.raises(TokenError) as e:
            verify_band(band_token(p), secret=SECRET, now=p.expires_at)
        assert code_of(e) is TokenErrorCode.EXPIRED

    def test_signed_invalid_band_is_malformed(self) -> None:
        """Even a correctly signed token with lo > hi is refused."""
        t = BandToken.parse(band_token())
        bad = t.model_copy(update={"lo_cents": -70, "signature": ""})
        tok = bad.model_copy(update={"signature": T._sign(SECRET, bad.body)}).encode()
        with pytest.raises(TokenError) as e:
            verify_band(tok, secret=SECRET, now=NOW)
        assert code_of(e) is TokenErrorCode.MALFORMED

    @pytest.mark.parametrize("bad", [None, ""])
    def test_parse_missing(self, bad: object) -> None:
        with pytest.raises(TokenError) as e:
            BandToken.parse(bad)
        assert code_of(e) is TokenErrorCode.MISSING

    @pytest.mark.parametrize("bad", [3, "x" * 200, "arc2.nope", "arc1.x"])
    def test_parse_malformed(self, bad: object) -> None:
        with pytest.raises(TokenError) as e:
            BandToken.parse(bad)
        assert code_of(e) is TokenErrorCode.MALFORMED

    def test_mint_refusals(self) -> None:
        p = G.make_proposal()
        failed = GateDecision(proposal_hash=proposal_hash(p), passed=False, violations=["x"])
        with pytest.raises(TokenError) as e:
            mint_band(
                proposal_hash(p),
                failed,
                order=order_payload(p),
                band=BAND,
                secret=SECRET,
                expires_at=p.expires_at,
                now=NOW,
            )
        assert code_of(e) is TokenErrorCode.NOT_PASSED
        other = proposal_hash(G.make_proposal(thesis="x"))
        with pytest.raises(TokenError) as e:
            mint_band(
                other,
                passed(p),
                order=order_payload(p),
                band=BAND,
                secret=SECRET,
                expires_at=p.expires_at,
                now=NOW,
            )
        assert code_of(e) is TokenErrorCode.PROPOSAL_MISMATCH
        with pytest.raises(TokenError) as e:
            band_token(p, band=PriceBand(lo=D("-0.84"), hi=D("-0.75"), max_steps=3))
        assert code_of(e) is TokenErrorCode.BAD_PAYLOAD
        with pytest.raises(TokenError) as e:
            band_token(p, expires_at=NOW)
        assert code_of(e) is TokenErrorCode.BAD_EXPIRY

    def test_band_price_too_large_for_the_id(self) -> None:
        with pytest.raises(TokenError) as e:
            T._to_cents(D("10000.00"))
        assert code_of(e) is TokenErrorCode.BAD_PAYLOAD
        with pytest.raises(TokenError):
            T._to_cents(D("1.001"))

    @given(st.integers(0, 3), st.integers(-85, -75))
    def test_property_every_step_inside_band_verifies(self, k: int, cents: int) -> None:
        coid = client_order_id(band_token(), k)
        tok, step = verify_client_order_id(
            coid, secret=SECRET, now=NOW, order=at(str(D(cents) / 100))
        )
        assert step == k and isinstance(tok, BandToken)


class TestClientOrderId:
    def test_arc1_bare_token(self) -> None:
        p = G.make_proposal()
        d = issue_token(passed(p), p, secret=SECRET, now=NOW)
        assert d.token is not None
        tok, step = verify_client_order_id(d.token, secret=SECRET, now=NOW, order=order_payload(p))
        assert step == 0 and not isinstance(tok, BandToken)

    @pytest.mark.parametrize(
        ("coid", "code"), [(None, "missing"), ("", "missing"), (5, "malformed")]
    )
    def test_bad_ids(self, coid: object, code: str) -> None:
        with pytest.raises(TokenError) as e:
            verify_client_order_id(coid, secret=SECRET, now=NOW, order=at("-0.85"))
        assert code_of(e).value == code

    def test_arc2_without_step_suffix(self) -> None:
        with pytest.raises(TokenError) as e:
            verify_client_order_id(band_token(), secret=SECRET, now=NOW, order=at("-0.85"))
        assert code_of(e) is TokenErrorCode.BAD_STEP


# ---------------------------------------------------------------------------
# pre_tool_call hook: arc2
# ---------------------------------------------------------------------------


def mleg(coid: str, limit: str = "-0.85") -> dict[str, object]:
    return {
        "qty": "2",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": limit,
        "order_class": "mleg",
        "client_order_id": coid,
        "legs": [
            {"symbol": G.SP, "side": "sell", "ratio_qty": "1"},
            {"symbol": G.LP, "side": "buy", "ratio_qty": "1"},
        ],
    }


class TestHookArc2:
    def test_step_inside_band_allowed(self) -> None:
        v = check_tool_call(
            TOOL, mleg(client_order_id(band_token(), 2), "-0.79"), secret=SECRET, now=NOW
        )
        assert v.allow, v.message

    def test_out_of_band_price_refused(self) -> None:
        v = check_tool_call(
            TOOL, mleg(client_order_id(band_token(), 3), "-0.74"), secret=SECRET, now=NOW
        )
        assert not v.allow and "out_of_band" in v.message

    def test_reused_step_id_refused(self) -> None:
        coid = client_order_id(band_token(), 1)
        v = check_tool_call(
            TOOL, mleg(coid, "-0.82"), secret=SECRET, now=NOW, used_order_ids={coid}
        )
        assert not v.allow and "reused_order_id" in v.message

    def test_step_beyond_band_refused(self) -> None:
        v = check_tool_call(TOOL, mleg(band_token() + ".s7", "-0.80"), secret=SECRET, now=NOW)
        assert not v.allow and "bad_step" in v.message

    def test_arc1_still_verifies(self) -> None:
        """Decision recorded in the PR: arc1 tokens keep verifying until they expire."""
        p = G.make_proposal()
        d = issue_token(passed(p), p, secret=SECRET, now=NOW)
        assert d.token is not None
        assert check_tool_call(TOOL, mleg(d.token), secret=SECRET, now=NOW).allow
        assert not check_tool_call(TOOL, mleg(d.token, "-0.84"), secret=SECRET, now=NOW).allow

    def test_arc_execute_accepts_arc2(self) -> None:
        cmd = f"arc execute --proposal abc --token {band_token()}"
        assert check_tool_call("terminal", {"command": cmd}, secret=SECRET, now=NOW).allow
