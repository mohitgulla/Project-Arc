"""D116 (E3.8, "option C"): the delta caps count debit verticals / long options at
sign(Δ) × max loss; every other kind keeps Δ × spot (``arc.gate.delta``).

Worked example from the 2026-10-14 UNH rejection (equity $24,600): book AAPL 345/335
put debit ×2 (max loss $650, Δ −41.7) and INTC 100/120 call debit ×2 (max loss
$1,123.90, Δ +63.1); proposal UNH 400/370 put debit ×1 (max loss $1,357.50, Δ −32.0,
spot $384.90). Old unit: AAPL + UNH alone was −$26,532 > the $24,637 cap.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import AccountSnapshot, MarketSnapshot, Portfolio, RuleCode
from arc.gate import rules as R
from arc.gate.delta import MAX_LOSS_DELTA_KINDS, delta_exposure, kind_of
from arc.models import Greeks, Leg, LegIntent, Proposal, QuantMetrics, Sizing, Structure
from arc.models import StructureKind as K
from arc.structures import analyze, credit_vertical, long_put
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 14, 10, 32, tzinfo=ET)
EQ = D("24600")


def cfg(dollar: float = 1.0, beta: float = 4.0) -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        account_profile="margin",
        portfolio_dollar_delta_cap_pct=dollar,
        portfolio_beta_delta_cap_pct=beta,
    )


def acct(equity: D = EQ) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity, last_equity=equity, options_buying_power=equity, as_of=NOW
    )


def _st(legs: list[tuple[str, str, str]], delta: float) -> Structure:
    built = [
        Leg(occ_symbol=s, side=LegIntent(side), ratio=1, premium=D(prem)) for s, side, prem in legs
    ]
    # as_of pinned: the fixtures' expiries (MU 10-23, AAPL 10-30, INTC 11-20) must not
    # turn into wall-clock time bombs
    return analyze(built, as_of=NOW.date()).model_copy(
        update={"greeks": Greeks(delta=delta, vega=1.0)}
    )


AAPL = _st(
    [("AAPL261030P00345000", "long", "5.915"), ("AAPL261030P00335000", "short", "2.665")],
    -20.84667968385837,
)
INTC = _st(
    [("INTC261120C00100000", "long", "8.1945"), ("INTC261120C00120000", "short", "2.575")],
    31.571152919563144,
)
UNH = _st(
    [("UNH261120P00400000", "long", "20.095"), ("UNH261120P00370000", "short", "6.52")],
    -32.0181973655809,
)


def prop(s: Structure, n: int = 1) -> Proposal:
    return Proposal(
        candidate_id="c1",
        structure=s,
        thesis="t",
        quant=QuantMetrics(pop=0.5, ev=D("1"), cost_bps=1.0),
        sizing=Sizing(contracts=n, notional=D("1"), pct_equity=0.01),
        expires_at=NOW + dt.timedelta(minutes=10),
    )


def mkt(root: str, spot: str, beta: str | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        quotes={},
        underlying_spot={root: D(spot)},
        underlying_beta={} if beta is None else {root: D(beta)},
    )


def _book_term(s: Structure, n: int) -> D:
    out = delta_exposure(kind_of(s.legs), s.greeks.delta * n, None, s.max_loss * n)
    assert out is not None
    return out


def test_kinds_and_terms() -> None:
    assert {kind_of(AAPL.legs), kind_of(INTC.legs), kind_of(UNH.legs)} == {K.VERTICAL_DEBIT}
    assert {K.VERTICAL_DEBIT, K.LONG_CALL, K.LONG_PUT} == MAX_LOSS_DELTA_KINDS
    assert _book_term(AAPL, 2) == D("-650.000")
    assert _book_term(INTC, 2) == D("1123.9000")
    assert _book_term(UNH, 1) == D("-1357.500")


def test_worked_example_unh_passes_the_net_dollar_delta_cap() -> None:
    """Book AAPL ×2 + INTC ×2 at $24,600 equity: UNH ×1 now passes ($Δ −$883.60)."""
    book = _book_term(AAPL, 2) + _book_term(INTC, 2)
    assert book == D("473.9000")
    pf = Portfolio(dollar_delta=book, beta_dollar_delta=book)
    assert R.check_greek_caps(prop(UNH), acct(), pf, mkt("UNH", "384.9", "1.0"), cfg()) == []
    # The live rejection (2026-10-14 10:32 ET, equity $24,636.87; INTC passed one second
    # later in the same run, so the book was AAPL alone) in the old Δ × spot unit:
    old_post = D("-14208.62") + D(str(UNH.greeks.delta)) * D("384.9")  # AAPL Δ × spot then
    assert old_post.quantize(D("0.01")) == D("-26532.42")  # "$26,532.42 > cap $24,636.87"
    # ... and in the new unit, on that same AAPL-only book and equity: passes
    aapl = Portfolio(dollar_delta=_book_term(AAPL, 2))
    out = R.check_greek_caps(prop(UNH), acct(D("24636.87")), aapl, mkt("UNH", "384.9"), cfg())
    assert out == []  # |−650 − 1,357.50| = $2,007.50 ≤ $24,636.87


def test_credit_vertical_with_the_same_delta_keeps_delta_times_spot() -> None:
    """A credit vertical carrying UNH's Δ is still measured Δ × spot: −$12,323.80."""
    cv = credit_vertical(
        "call", "UNH", dt.date(2026, 11, 20), short_strike=400, short_premium="6.00",
        long_strike=420, long_premium="2.00",
    ).model_copy(update={"greeks": Greeks(delta=UNH.greeks.delta, vega=1.0)})  # fmt: skip
    assert kind_of(cv.legs) == K.VERTICAL_CREDIT
    term = delta_exposure(kind_of(cv.legs), cv.greeks.delta, D("384.9"), cv.max_loss)
    assert term == pytest.approx(D("-12323.80"), abs=D("0.01"))
    # at a $24,600 × 0.50 = $12,300 cap the credit vertical fails, the debit one passes
    half = cfg(dollar=0.50)
    [v] = R.check_greek_caps(prop(cv), acct(), Portfolio(), mkt("UNH", "384.9"), half)
    assert v.code == RuleCode.DELTA_CAP and "$12,323.80 > cap $12,300.00" in v.detail
    assert R.check_greek_caps(prop(UNH), acct(), Portfolio(), mkt("UNH", "384.9"), half) == []


def test_beta_cap_weights_the_max_loss_term() -> None:
    """MU 2026-10-13: one put debit (max loss $1,983.50, β 3.20) was −$69.3k β-weighted;
    now −$6,347.20 = −1,983.50 × 3.20."""
    mu = _st(
        [("MU261023P00950000", "long", "34.91"), ("MU261023P00900000", "short", "15.075")],
        -22.982692606750053,
    )
    m = mkt("MU", "942.13", "3.20")
    assert R.check_greek_caps(prop(mu), acct(), Portfolio(), m, cfg(beta=0.26)) == []
    [v] = R.check_greek_caps(prop(mu), acct(), Portfolio(), m, cfg(beta=0.25))
    assert v.code == RuleCode.BETA_DELTA_CAP
    assert v.detail == "post-trade |β$Δ| $6,347.20 > cap $6,150.00 (MU β 3.20)"


def test_long_put_negative_and_zero_delta() -> None:
    lp = long_put("SPY", dt.date(2026, 11, 20), strike=500, premium="2.50")
    assert kind_of(lp.legs) == K.LONG_PUT
    assert delta_exposure(K.LONG_PUT, -40.0, None, D("250")) == D("-250")
    assert delta_exposure(K.LONG_PUT, 0.0, None, D("250")) == D(0)
    assert delta_exposure(K.LONG_CALL, 40, D("1"), D("-250")) == D("250")  # |max loss|
    zero = prop(lp.model_copy(update={"greeks": Greeks(delta=0.0, vega=1.0)}), n=3)
    out = R.check_greek_caps(zero, acct(), Portfolio(dollar_delta=EQ), mkt("SPY", "580"), cfg())
    assert out == []  # Δ == 0 contributes 0: the book at the cap stays at the cap


def test_other_kinds_and_unbounded_fall_back_to_spot() -> None:
    assert delta_exposure(K.IRON_CONDOR, 2, D("100"), D("400")) == D("200")
    assert delta_exposure(K.OTHER, 2, None, D("400")) is None
    assert delta_exposure(K.LONG_CALL, 2, D("100"), None) == D("200")  # unbounded: Δ × spot
    assert delta_exposure(K.LONG_CALL, 2, None, None) is None


def test_kind_of_malformed_is_other() -> None:
    bad = [
        Leg(occ_symbol="SPY261120C00500000", side=LegIntent.LONG, ratio=1, premium=D("1")),
        Leg(occ_symbol="QQQ261120C00500000", side=LegIntent.SHORT, ratio=1, premium=D("1")),
    ]
    assert kind_of(bad) == K.OTHER


def test_missing_spot_still_fails_closed_for_debit_kinds() -> None:
    """The max-loss unit needs no spot, but missing_spot stays fail-closed for every kind."""
    m = MarketSnapshot(quotes={}, underlying_spot={})
    out = R.check_greek_caps(prop(UNH), acct(), Portfolio(), m, cfg())
    assert [v.code for v in out] == [RuleCode.MISSING_SPOT]


_ML = st.decimals(min_value=D("0.01"), max_value=D("5000"), places=2)


@given(
    delta=st.floats(min_value=-500, max_value=500, allow_nan=False),
    spot=st.decimals(min_value=D("1"), max_value=D("2000"), places=2),
    ml=_ML,
    n=st.integers(1, 20),
)
def test_debit_term_is_signed_max_loss_and_never_above_it(
    delta: float, spot: D, ml: D, n: int
) -> None:
    """|term| ≤ max loss, sign follows Δ, independent of spot, linear in contracts."""
    for kind in MAX_LOSS_DELTA_KINDS:
        t = delta_exposure(kind, D(str(delta)) * n, spot, ml * n)
        assert t is not None and abs(t) <= ml * n
        if delta == 0:
            assert t == 0
        else:
            assert (t > 0) == (delta > 0) and abs(t) == ml * n
        assert t == delta_exposure(kind, D(str(delta)) * n, None, ml * n)
    t = delta_exposure(K.VERTICAL_CREDIT, D(str(delta)) * n, spot, ml * n)
    assert t == D(str(delta)) * n * spot


@given(book=st.integers(min_value=-60_000, max_value=60_000), n=st.integers(1, 10))
def test_gate_cap_boundary_in_the_max_loss_unit(book: int, n: int) -> None:
    """Post-trade $Δ = book + sign(Δ) × max loss × n; ≤ 1.00 × equity passes."""
    out = R.check_greek_caps(
        prop(UNH, n), acct(), Portfolio(dollar_delta=D(book)), mkt("UNH", "384.9"), cfg()
    )
    post = D(book) - D("1357.500") * n
    assert (RuleCode.DELTA_CAP not in {v.code for v in out}) == (abs(post) <= EQ)
