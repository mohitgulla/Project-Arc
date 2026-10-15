"""D116 (E3.7): tiered per-underlying cap — ``arc.gate.caps`` and rule per_underlying_limit.

Defaults: 15% of equity while deployed max loss (open + in-flight opens, pre-trade) is
below 60% of equity, else 10%. Baseline proposal (tests/test_gate.py): SPY bull put,
2 contracts, $830 max loss, equity $100,000.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import Portfolio, Position, RuleCode, derive
from arc.gate import rules as R
from arc.gate.caps import AllocTier, alloc_pct_for, alloc_tier, deployed_max_loss
from tests.test_gate import acct, make_proposal, run

EQ = D("100000")


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {
        "max_alloc_pct": 0.15,
        "max_alloc_pct_reduced": 0.10,
        "alloc_tier_threshold_pct": 0.60,
    }
    base.update(kw)
    return ArcSettings(_env_file=None, account_profile="margin", **base)  # type: ignore[call-arg]


def book(spy: str = "0", other: str = "0", reserved: str = "0") -> Portfolio:
    return Portfolio(
        positions=[
            Position(underlying="SPY", max_loss=D(spy)),
            Position(underlying="QQQ", max_loss=D(other)),
        ],
        reserved_max_loss=D(reserved),
    )


# ---------------------------------------------------------------------------
# arc.gate.caps
# ---------------------------------------------------------------------------


def test_shipped_defaults_are_15_10_60() -> None:
    s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.alloc_tier_threshold_pct == 0.60
    assert s.max_alloc_pct_reduced == 0.10


def test_base_tier_below_threshold() -> None:
    t = alloc_tier(D("59999.99"), EQ, cfg())
    assert t.tier == "base" and t.pct == D("0.15")
    assert t.describe() == "15% tier, deployed 60% < 60%"  # 59.99999% rounds for display


def test_exact_threshold_takes_the_reduced_tier() -> None:
    t = alloc_tier(D("60000"), EQ, cfg())
    assert t.tier == "reduced" and t.pct == D("0.1")
    assert t.describe() == "10% tier, deployed 60% ≥ 60%"


def test_reduced_never_above_base_orphan_override() -> None:
    # a pre-D116 override that lowered max_alloc_pct to 5% caps both tiers at 5%
    c = cfg(max_alloc_pct=0.05)
    assert alloc_tier(D(0), EQ, c).pct == D("0.05")
    assert alloc_tier(D("90000"), EQ, c).pct == D("0.05")


def test_non_positive_equity_takes_the_reduced_tier() -> None:
    for eq in (D(0), D(-1)):
        t = alloc_tier(D(0), eq, cfg())
        assert t == AllocTier(D("0.1"), "reduced", D(0), None, D("0.6"))
        assert t.describe() == "10% tier, deployed n/a ≥ 60%"


def test_alloc_pct_for_matches_alloc_tier() -> None:
    assert alloc_pct_for(D("100"), EQ, cfg()) == D("0.15")
    assert alloc_pct_for(D("70000"), EQ, cfg()) == D("0.1")


def test_deployed_counts_every_position_plus_reserved() -> None:
    pf = book(spy="1000", other="2000", reserved="500")
    assert deployed_max_loss(pf) == D("3500")
    assert deployed_max_loss(pf, D("100")) == D("3100")  # explicit reserved wins
    assert deployed_max_loss(pf, D("-5")) == D("3000")  # negative reserve counts as 0


@given(
    deployed_cents=st.integers(min_value=0, max_value=20_000_000),
    equity_cents=st.integers(min_value=1, max_value=20_000_000),
)
def test_tier_is_monotone_and_bounded(deployed_cents: int, equity_cents: int) -> None:
    dep, eq = D(deployed_cents) / 100, D(equity_cents) / 100
    t = alloc_tier(dep, eq, cfg())
    assert (t.tier == "base") == (dep < D("0.6") * eq)
    assert t.pct == (D("0.15") if t.tier == "base" else D("0.1"))
    assert t.pct <= D("0.15")


# ---------------------------------------------------------------------------
# rule per_underlying_limit
# ---------------------------------------------------------------------------


def test_rule_base_tier_allows_up_to_15_percent() -> None:
    d = derive(make_proposal())  # $830 new
    assert R.check_per_underlying(d, acct(), book(spy="14170"), cfg()) == []  # = 15,000
    [v] = R.check_per_underlying(d, acct(), book(spy="14170.01"), cfg())
    assert v.code == RuleCode.PER_UNDERLYING and "15% tier" in v.detail


def test_rule_reduced_tier_once_deployed_reaches_60_percent() -> None:
    d = derive(make_proposal())
    # SPY 9,170 + QQQ 50,830 = 60,000 deployed -> 10% tier: 9,170 + 830 = 10,000 OK
    assert R.check_per_underlying(d, acct(), book(spy="9170", other="50830"), cfg()) == []
    [v] = R.check_per_underlying(d, acct(), book(spy="9170.01", other="50830"), cfg())
    assert "10% tier, deployed 60% ≥ 60%" in v.detail


def test_rule_counts_reserved_in_flight_opens() -> None:
    d = derive(make_proposal())
    # 59,999 held + 1 reserved = threshold -> reduced; the same book without it is base
    assert R.check_per_underlying(d, acct(), book(spy="10000", other="49999"), cfg()) == []
    [v] = R.check_per_underlying(d, acct(), book(spy="10000", other="49999", reserved="1"), cfg())
    assert "10% tier" in v.detail


def test_one_trade_can_take_the_book_from_59_to_74_percent() -> None:
    # deployed excludes the trade checked: 59% + a 15% name = 74% after the fill
    pf = book(other="59000")
    big = make_proposal()  # 830 per 2 contracts -> 415/unit; 36 units = 14,940 ≤ 15,000
    from arc.models import Sizing

    p = big.model_copy(
        update={"sizing": Sizing(contracts=36, notional=D("14940"), pct_equity=0.15)}
    )
    assert R.check_per_underlying(derive(p), acct(), pf, cfg()) == []
    p37 = big.model_copy(
        update={"sizing": Sizing(contracts=37, notional=D("15355"), pct_equity=0.15)}
    )
    [v] = R.check_per_underlying(derive(p37), acct(), pf, cfg())
    assert "15% tier, deployed 59% < 60%" in v.detail


@pytest.mark.parametrize("equity", ["0", "-10"])
def test_rule_fails_closed_on_non_positive_equity(equity: str) -> None:
    [v] = R.check_per_underlying(
        derive(make_proposal()), acct(equity=D(equity)), Portfolio(), cfg()
    )
    assert "invalid equity" in v.detail


def test_evaluate_reports_the_tier_in_the_violation() -> None:
    dec = run(portfolio=book(spy="9171", other="50829"), config=cfg())
    assert any("PER_UNDERLYING" in v.upper() and "10% tier" in v for v in dec.violations)
