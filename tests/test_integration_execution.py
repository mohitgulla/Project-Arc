"""E6.2 paper integration: gate (band) → arc2 token → approval → ladder → audit trail.

Runs against the Alpaca **paper** account during RTH only; outside RTH it is
skipped (an off-hours run is not a pass for the E6.2 acceptance). Keys come
from ``~/.hermes/.env`` via ``ArcSettings`` / the Alpaca adapters.

The proposal is a 1-lot SPY bull call vertical ~30-45 DTE, priced at the live
mid. Steps are shortened to 5 s so the whole 4-attempt ladder takes ~30 s. The
order may fill or be cancelled after the last step; either is a pass as long
as every attempt's price was inside the approved band and the audit trail
(orders, order events, execution row, journal, fills/position on a fill) is
complete.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal as D

import pytest

from arc.utils.calendar import is_open, now_et
from tests.vertical_legs import select_bull_call_vertical

_HAS_KEYS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_KEYS, reason="ALPACA_API_KEY/SECRET not set"),
]


def test_approve_then_work_band_on_paper() -> None:
    from arc.approvals.service import ApprovalService, LogCardPoster, approval_record
    from arc.broker.alpaca_paper import AlpacaPaperBroker
    from arc.config import get_settings
    from arc.context.store import ContextStore
    from arc.data.alpaca import AlpacaMarketData
    from arc.execution.ladder import ExecStatus, execute
    from arc.gate import HaltSwitch, Portfolio, proposal_hash
    from arc.gate.halt import evaluate_with_halt
    from arc.gate.rules import price_band
    from arc.gate.token import TokenError, gate_secret, issue_token
    from arc.models import LegIntent, Proposal, QuantMetrics, Sizing
    from arc.pipeline.market import account_snapshot, limit_price, market_snapshot, price_structure
    from arc.store.db import connect
    from arc.store.execution import ExecutionRepo, OpenStructureRepo
    from arc.store.migrate import migrate
    from arc.store.repos import CandidateRepo, GateDecisionRepo, HaltRepo, ProposalRepo

    now = now_et()
    if not is_open(now):
        pytest.skip("RTH closed: E6.2 paper integration must run during market hours")

    settings = get_settings().model_copy(
        update={"execution_step_seconds": 5, "execution_poll_seconds": 1.0}
    )
    try:
        gate_secret(settings)
    except TokenError:
        pytest.skip("ARC_GATE_SECRET not set: no gate token can be minted")
    approver = settings.approver_slack_user_ids[0]
    broker = AlpacaPaperBroker()
    data = AlpacaMarketData()
    conn = connect(":memory:")
    migrate(conn)

    today = now.date()
    chain = data.option_chain("SPY", today + dt.timedelta(days=30), today + dt.timedelta(days=45))
    picked = select_bull_call_vertical(chain)
    assert picked is not None, "no usable SPY vertical"
    long_leg, short_leg = picked
    legs = [(long_leg.symbol, LegIntent.LONG, 1), (short_leg.symbol, LegIntent.SHORT, 1)]
    priced = price_structure(data, legs, as_of=today, r=0.04)
    limit = limit_price(priced.structure.net_debit_credit, settings.limit_tick)
    snap = market_snapshot(priced.contracts, {"SPY": None})
    band = price_band(priced.structure.legs, limit, snap, settings)

    now = now_et()  # after the quotes were fetched (gate refuses future quotes)
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="integration", confidence=0.5
    )
    acct = account_snapshot(broker.account(), now)
    proposal = Proposal(
        candidate_id=cid,
        structure=priced.structure,
        thesis="E6.2 paper integration: approve -> band ladder -> audit",
        quant=QuantMetrics(pop=0.5, ev=0),
        risk_narrative="1-lot defined-risk debit vertical on paper",
        sizing=Sizing(
            contracts=1,
            notional=abs(limit) * 100,
            pct_equity=float(abs(limit) * 100 / acct.equity),
        ),
        expires_at=now + dt.timedelta(minutes=10),
        limit_price=limit,
    )
    phash = proposal_hash(proposal)
    switch = HaltSwitch(HaltRepo(conn))
    decision = evaluate_with_halt(
        switch, proposal, acct, Portfolio(), settings, market=snap, now=now, band=band
    )
    assert decision.passed, decision.violations
    decision = issue_token(decision, proposal, secret=gate_secret(settings), now=now, band=band)
    assert decision.token is not None and decision.token.startswith("arc2.")

    ProposalRepo(conn).insert(
        candidate_id=cid,
        proposal_hash=phash,
        structure_json=proposal.structure.model_dump_json(),
        thesis=proposal.thesis,
        quant_json=proposal.quant.model_dump_json(),
        sizing_json=proposal.sizing.model_dump_json(),
        expires_at=proposal.expires_at.isoformat(),
        ticker="SPY",
        day=today.isoformat(),
        run_id="e62-integration",
    )
    ContextStore(conn).write(
        kind="proposal",
        subject="SPY",
        payload=proposal,
        produced_by="director",
        ttl="1h",
        run_id="e62-integration",
        now=now,
    )
    GateDecisionRepo(conn).insert(
        proposal_hash=phash,
        passed=True,
        violations=[],
        token=decision.token,
        account_snapshot=decision.account_snapshot,
        decided_at=now.isoformat(),
    )
    svc = ApprovalService(conn, settings, LogCardPoster())
    sweep = svc.publish_pending(now)
    assert sweep.published == [phash], sweep
    res = svc.decide(phash, user=approver, approve=True, now=now_et())
    assert res.outcome.value == "approved", res
    record = approval_record(conn, phash)
    assert record is not None

    out = execute(
        proposal,
        decision,
        record,
        conn=conn,
        broker=broker,
        config=settings,
        halt=switch,
        clock=now_et,
        sleep=time.sleep,
        ticker="SPY",
    )

    # -- outcome --------------------------------------------------------------
    assert out.status in (ExecStatus.FILLED, ExecStatus.CANCELLED, ExecStatus.PARTIALLY_FILLED), (
        out.status,
        out.detail,
    )
    assert out.attempts, "at least one attempt was sent"
    assert all(band.contains(a.limit_price) for a in out.attempts), [
        a.limit_price for a in out.attempts
    ]
    assert [a.limit_price for a in out.attempts] == list(band.ladder(D(str(settings.limit_tick))))[
        : len(out.attempts)
    ]
    assert len({a.client_order_id for a in out.attempts}) == len(out.attempts)
    assert all(a.broker_order_id for a in out.attempts)

    # -- audit trail ----------------------------------------------------------
    orders = conn.execute(
        "SELECT id, state, client_order_id, broker_order_id FROM orders WHERE proposal_hash = ?",
        (phash,),
    ).fetchall()
    assert len(orders) == len(out.attempts)
    assert all(o["state"] in ("filled", "cancelled", "partially_filled") for o in orders)
    for o in orders:
        events = conn.execute(
            "SELECT to_state FROM order_events WHERE order_id = ? ORDER BY id", (o["id"],)
        ).fetchall()
        trail = [e[0] for e in events]
        assert trail[:3] == ["gated", "approved", "submitted"], trail
        assert trail[-1] == o["state"], trail
    ex = ExecutionRepo(conn).get(phash)
    assert ex is not None and ex["status"] == str(out.status) and ex["attempts"] == len(orders)
    codes = [
        r[0]
        for r in conn.execute(
            "SELECT reason_code FROM decisions WHERE proposal_hash = ? ORDER BY rowid", (phash,)
        )
    ]
    assert codes[0] == "owner_approve", codes
    assert codes.count("order:step") == len(out.attempts), codes
    if out.status is ExecStatus.FILLED:
        assert out.filled_qty == 1 and out.structure_id is not None
        assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] >= 1
        assert OpenStructureRepo(conn).get(out.structure_id) is not None
    else:
        final = broker.order_status(out.attempts[-1].broker_order_id or "")
        assert final.status in ("canceled", "filled", "expired"), final.status
