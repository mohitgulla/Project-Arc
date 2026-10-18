"""D131 (E28.3): portfolio open-risk cap — ``arc.gate.caps.open_risk`` and rule open_risk_cap.

deployed (open + reserved in-flight opens, pre-trade) + the new open's max loss must stay
≤ ``max_open_risk_pct`` (40%) × equity. Baseline proposal (tests/test_gate.py): SPY bull
put, 2 contracts, $830 max loss, equity $100,000 → the cap is $40,000.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import Portfolio, Position, RuleCode, derive
from arc.gate import rules as R
from arc.gate.caps import AllocTier, OpenRisk, alloc_tier, open_risk, tier_open_risk
from arc.models import Sizing
from tests.test_gate import acct, make_proposal, run

EQ = D("100000")


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"max_alloc_pct": 0.15, "max_open_risk_pct": 0.40}
    base.update(kw)
    return ArcSettings(_env_file=None, account_profile="margin", **base)  # type: ignore[call-arg]


def book(*losses: str, reserved: str = "0") -> Portfolio:
    names = ("QQQ", "IWM", "AAPL", "MSFT", "NVDA")
    return Portfolio(
        positions=[
            Position(underlying=n, max_loss=D(x)) for n, x in zip(names, losses, strict=False)
        ],
        reserved_max_loss=D(reserved),
    )


# ---------------------------------------------------------------------------
# arc.gate.caps.open_risk
# ---------------------------------------------------------------------------


def test_shipped_default_is_40_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    # the suite pins the 100% rollback (tests/pre_d85_env.py)
    monkeypatch.delenv("ARC_MAX_OPEN_RISK_PCT", raising=False)
    assert ArcSettings(_env_file=None).max_open_risk_pct == 0.40  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [0.09, 1.01])
def test_setting_bounds(bad: float) -> None:
    with pytest.raises(ValueError, match="max_open_risk_pct"):
        cfg(max_open_risk_pct=bad)


def test_open_risk_headroom_and_describe() -> None:
    r = open_risk(D("12000"), EQ, 0.40)
    assert r == OpenRisk(D("0.4"), D("12000"), D("0.12"), D("28000.0"))
    assert r.describe() == "open risk 12% / 40%"
    assert r.shown and r.clause() == ", open risk 12% / 40%"


def test_headroom_floors_at_zero_over_the_cap() -> None:
    r = open_risk(D("45000"), EQ, D("0.40"))
    assert r.headroom == 0 and r.describe() == "open risk 45% / 40%"


@pytest.mark.parametrize("equity", ["0", "-1"])
def test_non_positive_equity_has_no_headroom(equity: str) -> None:
    r = open_risk(D("10"), D(equity), 0.40)
    assert r.deployed_pct is None and r.headroom is None
    assert r.describe() == "open risk n/a / 40%"


def test_cap_of_100_percent_is_the_rollback_and_not_shown() -> None:
    r = open_risk(D("10"), EQ, 1.0)
    assert not r.shown and r.clause() == ""


def test_tier_open_risk_reuses_the_tier_deployed_sum() -> None:
    t = alloc_tier(D("30000"), EQ, cfg())
    assert tier_open_risk(t, EQ, 0.40) == open_risk(D("30000"), EQ, 0.40)
    unvalued = t._replace(deployed_pct=None)  # an ungroupable book (book_alloc_tier)
    r = tier_open_risk(unvalued, EQ, 0.40)
    assert r.deployed_pct is None and r.describe() == "open risk n/a / 40%"
    assert isinstance(t, AllocTier)


@given(
    deployed_cents=st.integers(min_value=0, max_value=20_000_000),
    new_cents=st.integers(min_value=1, max_value=20_000_000),
    equity_cents=st.integers(min_value=1, max_value=20_000_000),
    cap_bp=st.integers(min_value=1000, max_value=10000),
)
def test_rule_passes_iff_within_cap(
    deployed_cents: int, new_cents: int, equity_cents: int, cap_bp: int
) -> None:
    dep, new, eq = D(deployed_cents) / 100, D(new_cents) / 100, D(equity_cents) / 100
    cap = D(cap_bp) / 10000
    d = derive(make_proposal())._replace(max_loss_total=new)
    pf = Portfolio(positions=[Position(underlying="QQQ", max_loss=dep)])
    out = R.check_open_risk(d, acct(equity=eq), pf, cfg(max_open_risk_pct=float(cap)))
    assert (out == []) == (dep + new <= cap * eq)


# ---------------------------------------------------------------------------
# rule open_risk_cap
# ---------------------------------------------------------------------------


def test_rule_allows_exactly_the_cap() -> None:
    d = derive(make_proposal())  # $830 new
    assert R.check_open_risk(d, acct(), book("20000", "19170"), cfg()) == []  # = 40,000
    [v] = R.check_open_risk(d, acct(), book("20000", "19170.01"), cfg())
    assert v.code == RuleCode.OPEN_RISK_CAP
    assert "deployed 39170.01 + new 830" in v.detail and "open risk 39% / 40%" in v.detail


def test_rule_counts_reserved_in_flight_opens() -> None:
    d = derive(make_proposal())
    assert R.check_open_risk(d, acct(), book("39170"), cfg()) == []
    [v] = R.check_open_risk(d, acct(), book("39170", reserved="0.01"), cfg())
    assert v.code == RuleCode.OPEN_RISK_CAP


def test_rule_scales_with_contracts() -> None:
    # 415/unit; 2 units fit 830 of headroom, 3 do not
    p3 = make_proposal().model_copy(
        update={"sizing": Sizing(contracts=3, notional=D("1245"), pct_equity=0.01)}
    )
    assert R.check_open_risk(derive(make_proposal()), acct(), book("39170"), cfg()) == []
    assert R.check_open_risk(derive(p3), acct(), book("39170"), cfg())


def test_rule_unbounded_max_loss_fails() -> None:
    d = derive(make_proposal())._replace(max_loss_total=None)
    [v] = R.check_open_risk(d, acct(), Portfolio(), cfg())
    assert v.detail == "max loss is unbounded"


@pytest.mark.parametrize("equity", ["0", "-10"])
def test_rule_fails_closed_on_non_positive_equity(equity: str) -> None:
    [v] = R.check_open_risk(derive(make_proposal()), acct(equity=D(equity)), Portfolio(), cfg())
    assert "invalid equity" in v.detail


def test_rule_at_100_percent_is_off_for_any_bounded_book() -> None:
    d = derive(make_proposal())
    assert R.check_open_risk(d, acct(), book("99170"), cfg(max_open_risk_pct=1.0)) == []


def test_evaluate_runs_the_rule_for_opens() -> None:
    dec = run(portfolio=book("20000", "19171"), config=cfg())
    assert any(v.startswith("open_risk_cap:") for v in dec.violations)
    ok = run(portfolio=book("20000", "19170"), config=cfg())
    assert not any(v.startswith("open_risk_cap:") for v in ok.violations)


def test_evaluate_exempts_closes() -> None:
    from arc.gate import evaluate
    from tests.test_gate import LP, NOW, SP, mkt

    # a book far over the cap: a close of the held spread still never hits the rule
    held = Portfolio(
        legs={SP: -2, LP: 2},
        positions=[Position(underlying="QQQ", max_loss=D("99999"))],
    )
    dec = evaluate(make_proposal(), acct(), held, cfg(), market=mkt(), now=NOW, closing=True)
    assert not any(v.startswith("open_risk_cap:") for v in dec.violations)
    opening = evaluate(make_proposal(), acct(), held, cfg(), market=mkt(), now=NOW)
    assert any(v.startswith("open_risk_cap:") for v in opening.violations)
