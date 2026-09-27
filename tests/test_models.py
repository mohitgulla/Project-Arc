"""Tests for arc.models data contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from arc.models import (
    ApprovalDecision,
    ApprovalRecord,
    Candidate,
    CatalystType,
    GateDecision,
    Leg,
    LegIntent,
    Order,
    OrderState,
    Proposal,
    QuantMetrics,
    Sizing,
    Structure,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def test_candidate_roundtrip() -> None:
    c = Candidate(
        ticker="AAPL",
        stance="bullish",
        catalyst_type=CatalystType.EARNINGS,
        confidence=0.75,
        sources=["reuters"],
        created_at=_now(),
    )
    assert c.ticker == "AAPL"
    data = c.model_dump()
    c2 = Candidate.model_validate(data)
    assert c2 == c


def test_structure_with_legs() -> None:
    leg = Leg(occ_symbol="AAPL  260117C00200000", side=LegIntent.LONG, ratio=1)
    s = Structure(
        legs=[leg],
        net_debit_credit=Decimal("3.50"),
        max_gain=Decimal("100"),
        max_loss=Decimal("3.50"),
        breakevens=[Decimal("203.50")],
        dte=45,
    )
    assert len(s.legs) == 1
    assert s.greeks.delta == 0.0


def test_proposal_constructs() -> None:
    leg = Leg(occ_symbol="SPY   260117P00400000", side=LegIntent.SHORT, ratio=1)
    structure = Structure(
        legs=[leg],
        net_debit_credit=Decimal("-1.20"),
        dte=35,
    )
    p = Proposal(
        candidate_id="cand_001",
        structure=structure,
        thesis="Short put on SPY — neutral/bullish view.",
        quant=QuantMetrics(pop=0.72, ev=Decimal("0.85"), cost_bps=12.5),
        sizing=Sizing(contracts=2, notional=Decimal("800"), pct_equity=0.04),
        expires_at=_now(),
    )
    assert p.quant.pop == 0.72


def test_gate_decision_passed() -> None:
    gd = GateDecision(
        proposal_hash="abc123",
        passed=True,
        token="hmac_token_here",
    )
    assert gd.passed
    assert gd.violations == []


def test_gate_decision_failed() -> None:
    gd = GateDecision(
        proposal_hash="abc123",
        passed=False,
        violations=["exceeds_5pct_allocation", "earnings_blackout"],
    )
    assert not gd.passed
    assert len(gd.violations) == 2


def test_approval_record() -> None:
    ar = ApprovalRecord(
        proposal_hash="abc123",
        slack_user="U0C5KUMH28G",
        slack_ts="1790545984.000100",
        decision=ApprovalDecision.APPROVED,
        at=_now(),
    )
    assert ar.decision == ApprovalDecision.APPROVED


def test_order_initial_state() -> None:
    now = _now()
    o = Order(
        id="ord_001",
        proposal_hash="abc123",
        created_at=now,
        updated_at=now,
    )
    assert o.state == OrderState.PROPOSED
    assert o.events == []
