"""Tests for the Risk Proxy Gate rules engine (E3.1).

`make test-gate` runs this file with branch coverage on ``arc.gate`` and
fails under 100%.

Baseline (hand-computed): SPY 570/565 bull put, 2 contracts, as of
2026-10-09 10:00 ET, expiring 2026-11-20 (42 calendar DTE).
  net = 1.25 - 2.10 = -0.85 credit/share; max loss = (5 - 0.85) x 100 = $415/unit
  -> $830 for 2 contracts vs 5% x $100,000 = $5,000 limit.
  quotes 565P 1.20/1.30, 570P 2.05/2.15 -> combo NBBO [1.20-2.15, 1.30-2.05]
  = [-0.95, -0.75]; limit -0.85 is inside and on the $0.01 tick.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings, StructureKind
from arc.gate import (
    AccountSnapshot,
    ClosedLot,
    MarketSnapshot,
    Portfolio,
    Position,
    Quote,
    RuleCode,
    Violation,
    derive,
    evaluate,
    proposal_hash,
)
from arc.gate import rules as R
from arc.models import (
    Greeks,
    Leg,
    LegIntent,
    Proposal,
    QuantMetrics,
    Sizing,
    Structure,
)
from arc.models import StructureKind as MK
from arc.structures import MarketInputs, credit_vertical, debit_vertical, format_occ, long_call
from arc.utils.calendar import ET

EXP = dt.date(2026, 11, 20)
AS_OF = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)
# Re-pricing always carries IV on every leg (E5.2a), so the baseline has real Greeks.
MARKET = MarketInputs(spot=580.0, r=0.04, ivs={LP: 0.20, SP: 0.19})


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def cfg(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile="margin", **kw)  # type: ignore[call-arg]


def bull_put() -> Structure:
    return credit_vertical(
        "put",
        "SPY",
        EXP,
        short_strike=570,
        short_premium="2.10",
        long_strike=565,
        long_premium="1.25",
        as_of=AS_OF,
        market=MARKET,
    )


def make_proposal(structure: Structure | None = None, **kw: object) -> Proposal:
    base: dict[str, object] = {
        "candidate_id": "cand_1",
        "structure": structure if structure is not None else bull_put(),
        "thesis": "neutral-bullish SPY",
        "quant": QuantMetrics(pop=0.7, ev=D("10"), cost_bps=5.0),
        "sizing": Sizing(contracts=2, notional=D("830"), pct_equity=0.0083),
        "expires_at": NOW + dt.timedelta(minutes=10),
    }
    base.update(kw)
    return Proposal(**base)  # type: ignore[arg-type]


def acct(**kw: object) -> AccountSnapshot:
    base: dict[str, object] = {
        "equity": D("100000"),
        "last_equity": D("100000"),
        "as_of": NOW - dt.timedelta(seconds=5),
    }
    base.update(kw)
    return AccountSnapshot(**base)  # type: ignore[arg-type]


def quote(bid: str, ask: str, age_s: int = 5) -> Quote:
    return Quote(bid=D(bid), ask=D(ask), as_of=NOW - dt.timedelta(seconds=age_s))


def mkt(**kw: object) -> MarketSnapshot:
    base: dict[str, object] = {
        "quotes": {LP: quote("1.20", "1.30"), SP: quote("2.05", "2.15")},
        "next_earnings": {"SPY": None},
    }
    base.update(kw)
    return MarketSnapshot(**base)  # type: ignore[arg-type]


def run(
    proposal: Proposal | None = None,
    account: AccountSnapshot | None = None,
    portfolio: Portfolio | None = None,
    config: ArcSettings | None = None,
    market: MarketSnapshot | None = None,
    now: dt.datetime = NOW,
):
    return evaluate(
        proposal or make_proposal(),
        account or acct(),
        portfolio or Portfolio(),
        config or cfg(),
        market=market or mkt(),
        now=now,
    )


def codes(decision) -> list[str]:
    return [v.split(":", 1)[0] for v in decision.violations]


# ---------------------------------------------------------------------------
# evaluate(): baseline, enumeration, fail-closed
# ---------------------------------------------------------------------------


def test_baseline_passes() -> None:
    d = run()
    assert d.passed, d.violations
    assert d.violations == []
    assert d.token is None
    assert d.proposal_hash == proposal_hash(make_proposal())
    assert d.account_snapshot["equity"] == "100000"


def test_enumerates_every_violation_without_short_circuit() -> None:
    p = make_proposal(expires_at=NOW - dt.timedelta(seconds=1))
    a = acct(halted=True, equity=D("96000"), as_of=NOW - dt.timedelta(hours=1))
    pf = Portfolio(
        positions=[Position(underlying="SPY", max_loss=D("4500"))] * 8,
        closed_lots=[
            ClosedLot(underlying="SPY", closed_at=NOW - dt.timedelta(days=3), realized_pnl=D("-1"))
        ],
    )
    m = mkt(quotes={LP: quote("1.00", "1.50", age_s=600), SP: quote("2.05", "2.15")})
    d = run(p, a, pf, cfg(), m, NOW)
    got = set(codes(d))
    assert not d.passed
    assert {
        RuleCode.HALTED,
        RuleCode.DAILY_LOSS,
        RuleCode.MAX_POSITIONS,
        RuleCode.APPROVAL_TTL,
        RuleCode.STALE_DATA,
        RuleCode.PER_UNDERLYING,
        RuleCode.SPREAD,
        RuleCode.WASH_SALE,
    } <= got


def test_malformed_structure_reported_and_other_rules_still_run() -> None:
    bad = Structure(
        legs=[Leg(occ_symbol=SP, side=LegIntent.SHORT)],  # no premium -> derive raises
        net_debit_credit=D("-2.10"),
        dte=42,
    )
    d = run(make_proposal(bad), acct(halted=True))
    assert RuleCode.STRUCTURE_INVALID in codes(d)
    assert RuleCode.HALTED in codes(d)
    assert not d.passed


def test_rule_exception_fails_closed() -> None:
    # naive closed_at vs aware now -> TypeError inside the wash-sale rule
    lot = ClosedLot(underlying="SPY", closed_at=dt.datetime(2026, 10, 1), realized_pnl=D("-5"))
    d = run(portfolio=Portfolio(closed_lots=[lot]))
    assert not d.passed
    assert any(v.startswith("rule_error: wash_sale: TypeError") for v in d.violations)


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(now=dt.datetime(2026, 10, 9, 10, 0))


def test_proposal_hash_is_canonical_and_sensitive() -> None:
    assert proposal_hash(make_proposal()) == proposal_hash(make_proposal())
    assert proposal_hash(make_proposal()) != proposal_hash(make_proposal(limit_price=D("-0.86")))
    assert len(proposal_hash(make_proposal())) == 64


def test_violation_str() -> None:
    assert str(Violation(code=RuleCode.HALTED, detail="x")) == "halted: x"


def test_derive_uses_legs_not_persona_numbers() -> None:
    s = bull_put().model_copy(update={"net_debit_credit": D("-4.00"), "max_loss": D("1")})
    d = derive(make_proposal(s))
    assert d.underlying == "SPY"
    assert d.expiration == EXP
    assert d.kind == MK.VERTICAL_CREDIT
    assert d.defined_risk
    assert d.max_loss_total == D("830.00")
    assert d.net_price == D("-0.85")
    assert d.limit_price == D("-0.85")
    assert derive(make_proposal(s, limit_price=D("-0.80"))).limit_price == D("-0.80")


# ---------------------------------------------------------------------------
# Per-underlying 5% (max-loss basis)
# ---------------------------------------------------------------------------


def test_per_underlying_counts_existing_same_underlying_only() -> None:
    pf = Portfolio(
        positions=[
            Position(underlying="SPY", max_loss=D("4170")),  # 4170 + 830 = 5000 -> OK
            Position(underlying="QQQ", max_loss=D("99999")),
        ]
    )
    assert R.check_per_underlying(derive(make_proposal()), acct(), pf, cfg()) == []
    pf2 = Portfolio(positions=[Position(underlying="SPY", max_loss=D("4170.01"))])
    [v] = R.check_per_underlying(derive(make_proposal()), acct(), pf2, cfg())
    assert v.code == RuleCode.PER_UNDERLYING


def test_per_underlying_unbounded_loss() -> None:
    naked = Structure(
        legs=[Leg(occ_symbol=format_occ("SPY", EXP, "call", 600), side="short", premium=D("1"))],
        net_debit_credit=D("-1"),
        dte=42,
    )
    d = derive(make_proposal(naked))
    assert d.max_loss_total is None
    [v] = R.check_per_underlying(d, acct(), Portfolio(), cfg())
    assert "unbounded" in v.detail


@given(extra_cents=st.integers(min_value=-500_000, max_value=500_000))
def test_per_underlying_boundary(extra_cents: int) -> None:
    existing = D("4170") + D(extra_cents) / 100
    if existing < 0:
        return
    pf = Portfolio(positions=[Position(underlying="SPY", max_loss=existing)])
    out = R.check_per_underlying(derive(make_proposal()), acct(), pf, cfg())
    assert (out == []) == (existing + D("830") <= D("5000"))


# ---------------------------------------------------------------------------
# Daily loss halt, halt flag
# ---------------------------------------------------------------------------


def test_daily_loss_rules() -> None:
    c = cfg()
    assert R.check_daily_loss(acct(equity=D("97000.01")), c) == []
    assert R.check_daily_loss(acct(equity=D("105000")), c) == []  # up day
    [v] = R.check_daily_loss(acct(equity=D("97000")), c)  # exactly 3% -> halt
    assert v.code == RuleCode.DAILY_LOSS
    [v] = R.check_daily_loss(acct(last_equity=D("0")), c)
    assert "invalid" in v.detail


@given(equity_cents=st.integers(min_value=0, max_value=20_000_000))
def test_daily_loss_boundary(equity_cents: int) -> None:
    eq = D(equity_cents) / 100
    out = R.check_daily_loss(acct(equity=eq), cfg())
    assert (out == []) == ((D("100000") - eq) / D("100000") < D("0.03"))


def test_halt_flag() -> None:
    assert R.check_halt(acct()) == []
    assert R.check_halt(acct(halted=True))[0].code == RuleCode.HALTED


# ---------------------------------------------------------------------------
# Spread / NBBO / tick
# ---------------------------------------------------------------------------


def _spread(p: Proposal | None = None, m: MarketSnapshot | None = None, **cfg_kw: object):
    p = p or make_proposal()
    return R.check_spread_tick(p, derive(p), m or mkt(), cfg(**cfg_kw))


def test_spread_ok_by_pct_or_abs() -> None:
    assert _spread() == []  # 0.10 abs on both legs
    # 565P 5.00/5.40: spread 0.40 > $0.10 but 7.7% of 5.20 mid; limit re-centred
    m = mkt(quotes={LP: quote("5.00", "5.40"), SP: quote("5.90", "6.00")})
    p = make_proposal(limit_price=D("-0.75"))  # NBBO [5.00-6.00, 5.40-5.90] = [-1.00, -0.50]
    assert _spread(p, m) == []


def test_spread_too_wide() -> None:
    m = mkt(quotes={LP: quote("1.00", "1.50"), SP: quote("2.05", "2.15")})
    [v] = _spread(m=m)  # NBBO [-1.15, -0.55] still contains -0.85
    assert v.code == RuleCode.SPREAD


def test_zero_mid_uses_abs_rule() -> None:
    lc = long_call("SPY", EXP, strike=700, premium="0.01", as_of=AS_OF)
    p = make_proposal(lc, limit_price=D("0"))
    m = mkt(quotes={lc.legs[0].occ_symbol: quote("0", "0")})
    assert _spread(p, m) == []
    m2 = mkt(quotes={lc.legs[0].occ_symbol: quote("0", "0.20")})  # mid .10, spread .20
    assert [v.code for v in _spread(p, m2)] == [RuleCode.SPREAD]


def test_missing_and_crossed_quotes_skip_nbbo() -> None:
    out = _spread(m=mkt(quotes={SP: quote("2.05", "2.15")}))
    assert [v.code for v in out] == [RuleCode.SPREAD]
    assert "no quote" in out[0].detail
    out = _spread(m=mkt(quotes={LP: quote("1.40", "1.30"), SP: quote("2.05", "2.15")}))
    assert [v.code for v in out] == [RuleCode.SPREAD]
    assert "crossed" in out[0].detail


def test_limit_outside_nbbo_and_off_tick() -> None:
    out = _spread(make_proposal(limit_price=D("-0.96")))
    assert [v.code for v in out] == [RuleCode.LIMIT_OUTSIDE_NBBO]
    out = _spread(make_proposal(limit_price=D("-0.745")))
    assert [v.code for v in out] == [RuleCode.LIMIT_OUTSIDE_NBBO, RuleCode.TICK]
    out = _spread(make_proposal(limit_price=D("-0.855")))
    assert [v.code for v in out] == [RuleCode.TICK]


def test_debit_structure_nbbo_ratio() -> None:
    bc = debit_vertical(
        "call",
        "SPY",
        EXP,
        long_strike=580,
        long_premium="4.00",
        short_strike=590,
        short_premium="1.50",
        as_of=AS_OF,
    )
    lo, sh = (leg.occ_symbol for leg in bc.legs)
    m = mkt(quotes={lo: quote("3.95", "4.05"), sh: quote("1.45", "1.55")})
    # NBBO [3.95-1.55, 4.05-1.45] = [2.40, 2.60]
    assert _spread(make_proposal(bc), m) == []
    assert [v.code for v in _spread(make_proposal(bc, limit_price=D("2.61")), m)] == [
        RuleCode.LIMIT_OUTSIDE_NBBO
    ]


@given(half_spread_cents=st.integers(min_value=0, max_value=100))
def test_spread_boundary(half_spread_cents: int) -> None:
    # single long call, mid 3.00: passes iff spread <= max(0.30, 0.10)
    lc = long_call("SPY", EXP, strike=600, premium="3.00", as_of=AS_OF)
    h = D(half_spread_cents) / 100
    m = mkt(quotes={lc.legs[0].occ_symbol: quote(str(D("3") - h), str(D("3") + h))})
    out = _spread(make_proposal(lc, limit_price=D("3.00")), m)
    assert (out == []) == (2 * h <= D("0.30"))


# ---------------------------------------------------------------------------
# Wash sale
# ---------------------------------------------------------------------------


def _lot(days_ago: float, pnl: str, und: str = "SPY") -> ClosedLot:
    return ClosedLot(
        underlying=und, closed_at=NOW - dt.timedelta(days=days_ago), realized_pnl=D(pnl)
    )


def test_wash_sale() -> None:
    d, c = derive(make_proposal()), cfg()
    ok = Portfolio(closed_lots=[_lot(5, "10"), _lot(5, "-10", "QQQ"), _lot(30.01, "-10")])
    assert R.check_wash_sale(d, ok, c, NOW) == []
    bad = Portfolio(closed_lots=[_lot(30, "-10"), _lot(2, "-1"), _lot(1, "5")])
    [v] = R.check_wash_sale(d, bad, c, NOW)
    assert v.code == RuleCode.WASH_SALE
    assert (NOW - dt.timedelta(days=2)).isoformat() in v.detail


@given(minutes=st.integers(min_value=0, max_value=60 * 24 * 60))
def test_wash_sale_boundary(minutes: int) -> None:
    lot = ClosedLot(
        underlying="SPY", closed_at=NOW - dt.timedelta(minutes=minutes), realized_pnl=D("-1")
    )
    out = R.check_wash_sale(derive(make_proposal()), Portfolio(closed_lots=[lot]), cfg(), NOW)
    assert (out == []) == (minutes > 30 * 24 * 60)


def test_closed_lot_from_store_row() -> None:
    lot = ClosedLot.from_row(
        {"ticker": "SPY", "closed_at": "2026-10-01T15:00:00.000000Z", "realized_pnl": "-12.5"}
    )
    assert lot.closed_at == dt.datetime(2026, 10, 1, 15, tzinfo=dt.UTC)
    assert lot.realized_pnl == D("-12.5")
    naive = ClosedLot.from_row(
        {"ticker": "SPY", "closed_at": "2026-10-01T15:00:00", "realized_pnl": "1"}
    )
    assert naive.closed_at.tzinfo is dt.UTC


# ---------------------------------------------------------------------------
# Portfolio Greek caps
# ---------------------------------------------------------------------------


def test_greek_caps() -> None:
    c, a = cfg(), acct()  # delta cap 0.30 x 1000 = 300 sh-eq; vega cap $500/vol-pt
    s = bull_put().model_copy(update={"greeks": Greeks(delta=20.0, vega=-1000.0)})
    p = make_proposal(s)  # x2: delta 40, vega -2000 -> $20/vol-pt
    assert R.check_greek_caps(p, a, Portfolio(greeks=Greeks(delta=260.0)), c) == []
    [v] = R.check_greek_caps(p, a, Portfolio(greeks=Greeks(delta=261.0)), c)
    assert v.code == RuleCode.DELTA_CAP
    [v] = R.check_greek_caps(p, a, Portfolio(greeks=Greeks(delta=-400.0)), c)
    assert v.code == RuleCode.DELTA_CAP
    [v] = R.check_greek_caps(p, a, Portfolio(greeks=Greeks(vega=-48_001.0)), c)
    assert v.code == RuleCode.VEGA_CAP
    assert R.check_greek_caps(p, a, Portfolio(greeks=Greeks(vega=-48_000.0)), c) == []


@given(delta=st.floats(min_value=-1000, max_value=1000, allow_nan=False))
def test_delta_cap_boundary(delta: float) -> None:
    flat = make_proposal(bull_put().model_copy(update={"greeks": Greeks()}))  # portfolio Δ only
    out = R.check_greek_caps(flat, acct(), Portfolio(greeks=Greeks(delta=delta)), cfg())
    assert (out == []) == (abs(D(str(delta))) <= D("300"))


# ---------------------------------------------------------------------------
# Greeks present (E5.2a, PLAN §6.8)
# ---------------------------------------------------------------------------


def test_gate_rejects_zero_greeks_structure() -> None:
    zero = make_proposal(bull_put().model_copy(update={"greeks": Greeks()}))
    d = run(zero)
    assert not d.passed
    assert codes(d) == [RuleCode.MISSING_GREEKS]
    assert R.check_greeks_present(make_proposal()) == []


def test_greeks_present_single_leg_and_closing_exempt() -> None:
    # One long leg: zero Greeks cannot hide Greek-cap exposure in a spread; exempt.
    lc = make_proposal(long_call("SPY", EXP, strike=600, premium="3", as_of=AS_OF))
    assert R.check_greeks_present(lc) == []
    # Any single non-zero Greek counts as present.
    only_theta = bull_put().model_copy(update={"greeks": Greeks(theta=-1.0)})
    assert R.check_greeks_present(make_proposal(only_theta)) == []
    # Closing proposals skip the opening-risk rules, including this one.
    zero = make_proposal(bull_put().model_copy(update={"greeks": Greeks()}))
    held = Portfolio(legs={SP: -2, LP: 2})
    closing = evaluate(zero, acct(), held, cfg(), market=mkt(), now=NOW, closing=True)
    assert RuleCode.MISSING_GREEKS not in codes(closing)


# ---------------------------------------------------------------------------
# Structure whitelist
# ---------------------------------------------------------------------------


def test_whitelist_accepts_all_phase1_kinds() -> None:
    p = make_proposal()
    assert R.check_structure_whitelist(p, derive(p), cfg()) == []
    lc = make_proposal(long_call("SPY", EXP, strike=600, premium="3", as_of=AS_OF))
    assert R.check_structure_whitelist(lc, derive(lc), cfg()) == []


def test_whitelist_rejects_other_and_undefined_risk() -> None:
    naked = Structure(
        legs=[Leg(occ_symbol=SP, side="short", premium=D("2.10"))],
        net_debit_credit=D("-2.10"),
        dte=42,
    )
    p = make_proposal(naked)
    out = R.check_structure_whitelist(p, derive(p), cfg())
    assert len(out) == 2
    assert all(v.code == RuleCode.STRUCTURE_NOT_ALLOWED for v in out)


def test_whitelist_respects_config_and_label() -> None:
    p = make_proposal()
    [v] = R.check_structure_whitelist(
        p, derive(p), cfg(structure_whitelist=[StructureKind.LONG_CALL])
    )
    assert "not whitelisted" in v.detail
    lying = make_proposal(bull_put().model_copy(update={"kind": MK.IRON_CONDOR}))
    [v] = R.check_structure_whitelist(lying, derive(lying), cfg())
    assert "does not match" in v.detail
    unlabelled = make_proposal(bull_put().model_copy(update={"kind": None}))
    assert R.check_structure_whitelist(unlabelled, derive(unlabelled), cfg()) == []


# ---------------------------------------------------------------------------
# DTE window
# ---------------------------------------------------------------------------


@given(days_before=st.integers(min_value=-5, max_value=60))
def test_dte_window_boundary(days_before: int) -> None:
    now = dt.datetime.combine(EXP - dt.timedelta(days=days_before), dt.time(10), tzinfo=ET)
    out = R.check_dte_window(derive(make_proposal()), cfg(), now)
    assert (out == []) == (30 <= days_before <= 45)


def test_zero_dte_never_allowed_even_if_configured() -> None:
    now = dt.datetime.combine(EXP, dt.time(10), tzinfo=ET)
    [v] = R.check_dte_window(derive(make_proposal()), cfg(dte_min=0), now)
    assert v.code == RuleCode.DTE_WINDOW


def test_dte_uses_eastern_date() -> None:
    # 2026-10-06 23:30 PT = 2026-10-07 02:30 ET -> 44 DTE (would be 45 on the PT date)
    now = dt.datetime(2026, 10, 7, 6, 30, tzinfo=dt.UTC)
    assert R.check_dte_window(derive(make_proposal()), cfg(dte_max=44), now) == []


# ---------------------------------------------------------------------------
# Earnings blackout
# ---------------------------------------------------------------------------


def _earn(p: Proposal | None = None, m: MarketSnapshot | None = None, **kw: object):
    p = p or make_proposal()
    return R.check_earnings_blackout(p, derive(p), m or mkt(), cfg(**kw), NOW)


def test_earnings_blackout() -> None:
    thru = mkt(next_earnings={"SPY": dt.date(2026, 10, 30)})
    [v] = _earn(m=thru)
    assert v.code == RuleCode.EARNINGS_BLACKOUT
    assert _earn(m=mkt(next_earnings={"SPY": EXP})) != []  # on expiry day: still held
    assert _earn(m=mkt(next_earnings={"SPY": NOW.date()})) != []  # today
    assert _earn(m=mkt(next_earnings={"SPY": dt.date(2026, 11, 21)})) == []  # after expiry
    assert _earn(m=mkt(next_earnings={"SPY": dt.date(2026, 10, 8)})) == []  # already past
    assert _earn() == []  # known: none scheduled
    [v] = _earn(m=mkt(next_earnings={}))
    assert "unknown" in v.detail


def test_earnings_exemptions() -> None:
    thru = mkt(next_earnings={"SPY": dt.date(2026, 10, 30)})
    assert _earn(m=thru, earnings_blackout=False) == []
    assert _earn(make_proposal(earnings_play=True, risk_concurs=True), thru) == []
    assert _earn(make_proposal(earnings_play=True), thru) != []
    assert _earn(make_proposal(risk_concurs=True), thru) != []
    debit = make_proposal(long_call("SPY", EXP, strike=600, premium="3", as_of=AS_OF))
    assert _earn(debit, thru) == []  # long premium is not blacked out


# ---------------------------------------------------------------------------
# Max open positions
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=0, max_value=20))
def test_max_open_positions(n: int) -> None:
    pf = Portfolio(positions=[Position(underlying="QQQ", max_loss=D("1"))] * n)
    out = R.check_max_open_positions(pf, cfg())
    assert (out == []) == (n + 1 <= 8)


# ---------------------------------------------------------------------------
# Approval TTL
# ---------------------------------------------------------------------------


@given(seconds=st.integers(min_value=-3600, max_value=3600))
def test_approval_ttl_boundary(seconds: int) -> None:
    p = make_proposal(expires_at=NOW + dt.timedelta(seconds=seconds))
    out = R.check_approval_ttl(p, cfg(), NOW)
    assert (out == []) == (0 < seconds <= 1200)


def test_approval_ttl_messages() -> None:
    [v] = R.check_approval_ttl(make_proposal(expires_at=NOW), cfg(), NOW)
    assert "expired" in v.detail
    [v] = R.check_approval_ttl(make_proposal(expires_at=NOW + dt.timedelta(hours=1)), cfg(), NOW)
    assert "exceeds TTL" in v.detail


# ---------------------------------------------------------------------------
# Data freshness
# ---------------------------------------------------------------------------


def _fresh(a: AccountSnapshot | None = None, m: MarketSnapshot | None = None):
    return R.check_data_freshness(make_proposal(), a or acct(), m or mkt(), cfg(), NOW)


def test_data_freshness() -> None:
    assert _fresh() == []
    [v] = _fresh(acct(as_of=NOW - dt.timedelta(seconds=301)))
    assert "account snapshot" in v.detail
    [v] = _fresh(acct(as_of=NOW + dt.timedelta(seconds=1)))
    assert "future" in v.detail
    [v] = _fresh(m=mkt(quotes={LP: quote("1.20", "1.30", age_s=61), SP: quote("2.05", "2.15")}))
    assert LP in v.detail
    [v] = _fresh(m=mkt(quotes={SP: quote("2.05", "2.15")}))
    assert "no quote" in v.detail


@given(age=st.integers(min_value=0, max_value=200))
def test_quote_freshness_boundary(age: int) -> None:
    m = mkt(quotes={LP: quote("1.20", "1.30", age_s=age), SP: quote("2.05", "2.15")})
    assert (_fresh(m=m) == []) == (age <= 60)


# ---------------------------------------------------------------------------
# Purity guard (belt and braces alongside import-linter)
# ---------------------------------------------------------------------------


def test_gate_source_has_no_forbidden_imports() -> None:
    import pathlib

    import arc.gate

    root = pathlib.Path(arc.gate.__file__).parent
    banned = ("anthropic", "openai", "requests", "httpx", "alpaca", "slack_sdk", "sqlite3")
    for f in root.glob("*.py"):
        text = f.read_text()
        for name in banned:
            assert f"import {name}" not in text, f"{f.name} imports {name}"
            assert f"from {name}" not in text, f"{f.name} imports from {name}"


def test_gate_never_reads_context_or_notes() -> None:
    """D27: persona notes (and the context store) are never gate inputs."""
    import pathlib

    import arc.gate

    root = pathlib.Path(arc.gate.__file__).parent
    for f in root.rglob("*.py"):
        text = f.read_text()
        assert "arc.context" not in text, f"{f.name} references arc.context"
        assert "NotePayload" not in text, f"{f.name} references NotePayload"
        assert '"note"' not in text, f"{f.name} references the note kind"


# ---------------------------------------------------------------------------
# E6.4: typed capacity-only rejection (rejected_for) for close-to-reallocate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("violations", "expected"),
    [
        (["per_underlying_limit: SPY 5,682 > 5,000"], R.CapacityRejection.BUYING_POWER),
        (["account_profile_settled_cash: needs $612 > $100"], R.CapacityRejection.BUYING_POWER),
        (["max_open_positions: 9 > max 8"], R.CapacityRejection.PORTFOLIO_CAP),
        (
            ["per_underlying_limit: x", "max_open_positions: y"],
            R.CapacityRejection.BUYING_POWER,
        ),
        ([], None),
        (["per_underlying_limit: x", "trading_halted: manual"], None),
        (["stale_data: quote is 90s old"], None),
    ],
)
def test_capacity_rejection(violations: list[str], expected: object) -> None:
    assert R.capacity_rejection(violations) == expected


def test_capacity_rejection_matches_real_gate_output() -> None:
    """A decision the gate fails only on the per-underlying cap is capacity-only."""
    held = Portfolio(positions=[Position(underlying="SPY", max_loss=D("4900"))])
    d = run(portfolio=held)
    assert codes(d) == [RuleCode.PER_UNDERLYING.value]
    assert R.capacity_rejection(d.violations) is R.CapacityRejection.BUYING_POWER
    assert R.capacity_rejection(run().violations) is None  # passed: nothing to reallocate
