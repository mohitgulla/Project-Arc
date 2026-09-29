"""E6.2 paper integration: gate (band) → arc2 token → approval → ladder → audit trail.

**Opt-in** (E6.2a): it trades real paper spreads, so it runs only with
``ARC_LIVE_EXEC_TESTS=1`` (plus paper keys and ``ARC_GATE_SECRET``) and only
during RTH; otherwise it is skipped (an off-hours run is not a pass for the
E6.2 acceptance). Keys come from ``~/.hermes/.env`` via ``ArcSettings`` / the
Alpaca adapters. See docs/OPS.md "Live execution test".

The proposal is a 1-lot SPY bull call vertical ~30-45 DTE near the money, whose
combo far touch is below its width (:func:`select_sane_bull_call_vertical`), so
every ladder price leaves max gain > 0; the gate's max-gain cap is asserted on
top. Steps are shortened to 5 s so the whole 4-attempt ladder takes ~30 s. The
order may fill or be cancelled after the last step; either is a pass as long as
every attempt's price was inside the approved band and the audit trail
(orders, order events, execution row, journal, fills/position on a fill) is
complete.

E6.2a quote check: every price (open and close) goes through the same
:func:`arc.execution.exits.check_close_quotes` the live exit paths use. Before
opening, the test prices the *close* from fresh quotes; if they are unusable it
skips with that reason (nothing traded).

Cleanup: on a fill the test closes the position through the same path (quote
check → closing gate → arc2 token → approval → ladder, ``kind='close'``). If the
close does not fill, or its quotes are unusable, it re-fetches quotes and tries
once more. If that also fails, the test fails and names the open legs so they
can be closed by hand.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal as D
from typing import Any

import pytest

from arc.utils.calendar import is_open, now_et
from tests.vertical_legs import select_sane_bull_call_vertical

_HAS_KEYS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_LIVE_EXEC = os.environ.get("ARC_LIVE_EXEC_TESTS") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_exec,
    pytest.mark.skipif(not _HAS_KEYS, reason="ALPACA_API_KEY/SECRET not set"),
    pytest.mark.skipif(
        not _LIVE_EXEC, reason="trades live paper spreads: opt in with ARC_LIVE_EXEC_TESTS=1"
    ),
]


_READS = 5  # quote reads per step before giving up (open, preflight, each close try)
_READ_GAP_S = 5


class QuotesUnusableError(RuntimeError):
    """The legs' quotes failed the E6.2a check: nothing was proposed or sent."""


def _row(structure_id: str | None) -> dict[str, Any]:
    return {"ticker": "SPY", "id": structure_id or "e62-integration"}


def _quote_check(conn: Any, priced: Any, settings: Any, *, fired: str, sid: str | None) -> None:
    """The live exit paths' quote check (same function, no test-only shortcut)."""
    from arc.execution.exits import check_close_quotes
    from arc.journal.reasons import JournalPersona

    problems, _ = check_close_quotes(
        conn,
        row=_row(sid),
        priced=priced,
        settings=settings,
        now=now_et(),
        persona=JournalPersona.INVESTOR,
        run_id=f"e62-integration-{fired}",
        fired=fired,
    )
    if problems:
        raise QuotesUnusableError("; ".join(problems))


def _gate_approve_execute(
    *,
    conn: Any,
    broker: Any,
    data: Any,
    settings: Any,
    legs: list[Any],
    candidate_id: str,
    run_id: str,
    portfolio: Any,
    closing: bool,
    structure_id: str | None = None,
) -> tuple[Any, Any, Any, str]:
    """Price → quote check → band → gate → arc2 token → proposal/decision → approval → ladder."""
    from arc.approvals.service import ApprovalService, LogCardPoster, approval_record
    from arc.context.store import ContextStore
    from arc.execution.ladder import execute
    from arc.gate import HaltSwitch, proposal_hash
    from arc.gate.halt import evaluate_with_halt
    from arc.gate.rules import max_gain_cap, price_band
    from arc.gate.token import gate_secret, issue_token
    from arc.models import Proposal, QuantMetrics, Sizing
    from arc.pipeline.market import account_snapshot, limit_price, market_snapshot, price_structure
    from arc.store.repos import GateDecisionRepo, HaltRepo, ProposalRepo

    today = now_et().date()
    priced = price_structure(data, legs, as_of=today, r=0.04, require_iv=not closing)
    _quote_check(conn, priced, settings, fired="close" if closing else "open", sid=structure_id)
    limit = limit_price(priced.structure.net_debit_credit, settings.limit_tick)
    snap = market_snapshot(priced.contracts, {"SPY": None})
    band = price_band(priced.structure.legs, limit, snap, settings)
    tick = D(str(settings.limit_tick))
    cap = max_gain_cap(priced.structure.legs, tick)
    assert cap is not None and band.hi <= cap, (band, cap)  # max gain > 0 at every step

    now = now_et()  # after the quotes were fetched (gate refuses future quotes)
    acct = account_snapshot(broker.account(), now)
    proposal = Proposal(
        candidate_id=candidate_id,
        structure=priced.structure,
        thesis=f"E6.2 paper integration ({'close' if closing else 'open'}): band ladder + audit",
        quant=QuantMetrics(pop=0.5, ev=0),
        risk_narrative="1-lot defined-risk vertical on paper",
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
        switch,
        proposal,
        acct,
        portfolio,
        settings,
        market=snap,
        now=now,
        band=band,
        closing=closing,
    )
    assert decision.passed, decision.violations
    decision = issue_token(decision, proposal, secret=gate_secret(settings), now=now, band=band)
    assert decision.token is not None and decision.token.startswith("arc2.")

    ProposalRepo(conn).insert(
        candidate_id=candidate_id,
        proposal_hash=phash,
        structure_json=proposal.structure.model_dump_json(),
        thesis=proposal.thesis,
        quant_json=proposal.quant.model_dump_json(),
        sizing_json=proposal.sizing.model_dump_json(),
        expires_at=proposal.expires_at.isoformat(),
        ticker="SPY",
        day=today.isoformat(),
        run_id=run_id,
        kind="close" if closing else "open",
    )
    ContextStore(conn).write(
        kind="proposal",
        subject="SPY",
        payload=proposal,
        produced_by="director",
        ttl="1h",
        run_id=run_id,
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
    res = svc.decide(phash, user=settings.approver_slack_user_ids[0], approve=True, now=now_et())
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
        kind="close" if closing else "open",
        structure_id=structure_id,
        ticker="SPY",
    )
    return proposal, band, out, phash


def _held(broker: Any, symbols: set[str]) -> dict[str, int]:
    return {
        p.symbol: int(p.qty) for p in broker.positions() if p.symbol in symbols and int(p.qty) != 0
    }


def _close_with_retry(
    *, conn: Any, broker: Any, data: Any, settings: Any, legs: list[Any], cid: str,
    held: dict[str, int], structure_id: str | None,
) -> list[str]:  # fmt: skip
    """Close through the live path; one more try from freshly fetched quotes. Outcome lines."""
    from arc.execution.ladder import ExecStatus
    from arc.gate import Portfolio
    from arc.pipeline.market import price_structure

    tries: list[str] = []
    for attempt in (1, 2):
        for _ in range(_READS - 1):  # re-read while only the quotes are unusable
            pre = price_structure(data, legs, as_of=now_et().date(), r=0.04, require_iv=False)
            try:
                _quote_check(conn, pre, settings, fired="close-preread", sid=structure_id)
                break
            except QuotesUnusableError:
                time.sleep(_READ_GAP_S)
        try:
            _, close_band, closed, _ = _gate_approve_execute(
                conn=conn,
                broker=broker,
                data=data,
                settings=settings,
                legs=legs,
                candidate_id=cid,
                run_id=f"e62-integration-close-{attempt}",
                portfolio=Portfolio(legs=held),
                closing=True,
                structure_id=structure_id,
            )
        except QuotesUnusableError as exc:
            tries.append(f"close try {attempt}: quotes unusable ({exc})")
        else:
            assert all(close_band.contains(a.limit_price) for a in closed.attempts)
            tries.append(f"close try {attempt}: band {close_band.lo}..{close_band.hi} "
                         f"-> {closed.summary()}")  # fmt: skip
            if closed.status is ExecStatus.FILLED:
                return tries
            if closed.filled_qty:  # partial: close only what is still held
                held = {s: q - (closed.filled_qty if q > 0 else -closed.filled_qty)
                        for s, q in held.items()}  # fmt: skip
        if attempt == 1:
            time.sleep(5)  # fresh quotes for the retry
    return tries


def test_approve_then_work_band_on_paper() -> None:
    from arc.broker.alpaca_paper import AlpacaPaperBroker
    from arc.config import get_settings
    from arc.data.alpaca import AlpacaMarketData
    from arc.execution.exits import exit_legs
    from arc.execution.ladder import ExecStatus
    from arc.gate import Portfolio
    from arc.gate.token import TokenError, gate_secret
    from arc.models import LegIntent
    from arc.pipeline.market import price_structure
    from arc.store.db import connect
    from arc.store.execution import ExecutionRepo, OpenStructureRepo
    from arc.store.migrate import migrate
    from arc.store.repos import CandidateRepo

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
    broker = AlpacaPaperBroker()
    data = AlpacaMarketData()
    conn = connect(":memory:")
    migrate(conn)

    today = now.date()
    chain = data.option_chain("SPY", today + dt.timedelta(days=30), today + dt.timedelta(days=45))
    picked = select_sane_bull_call_vertical(chain)
    assert picked is not None, "no near-the-money SPY vertical with far touch < width"
    long_leg, short_leg = picked
    symbols = {long_leg.symbol, short_leg.symbol}
    before = _held(broker, symbols)
    legs = [(long_leg.symbol, LegIntent.LONG, 1), (short_leg.symbol, LegIntent.SHORT, 1)]
    close_legs = [(s, LegIntent.SHORT if i == LegIntent.LONG else LegIntent.LONG, n)
                  for s, i, n in legs]  # fmt: skip
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="integration", confidence=0.5
    )

    # (a) before opening: the close must be priceable from fresh quotes, else skip.
    # The indicative feed jitters per read (docs/OPS.md 5.11), so re-read a few times;
    # every read goes through the unchanged check.
    reads: list[str] = []
    for _ in range(_READS):
        pre = price_structure(data, close_legs, as_of=today, r=0.04, require_iv=False)
        try:
            _quote_check(conn, pre, settings, fired="preflight", sid=None)
        except QuotesUnusableError as exc:
            reads.append(str(exc))
            time.sleep(_READ_GAP_S)
        else:
            break
    else:
        pytest.skip(f"close quotes unusable on {_READS} reads, nothing traded: {reads}")

    opened = None
    for _ in range(_READS):
        try:
            opened = _gate_approve_execute(
                conn=conn,
                broker=broker,
                data=data,
                settings=settings,
                legs=legs,
                candidate_id=cid,
                run_id="e62-integration-open",
                portfolio=Portfolio(),
                closing=False,
            )
        except QuotesUnusableError as exc:  # raised before any proposal or order
            reads.append(str(exc))
            time.sleep(_READ_GAP_S)
        else:
            break
    if opened is None:
        pytest.skip(f"open quotes unusable on {_READS} reads, nothing traded: {reads}")
    proposal, band, out, phash = opened
    width = D(str(short_leg.strike - long_leg.strike))

    try:
        # -- outcome ----------------------------------------------------------
        assert out.status in (
            ExecStatus.FILLED,
            ExecStatus.CANCELLED,
            ExecStatus.PARTIALLY_FILLED,
        ), (out.status, out.detail)
        assert out.attempts, "at least one attempt was sent"
        prices = [a.limit_price for a in out.attempts]
        assert all(band.contains(p) for p in prices), prices
        assert all(p < width for p in prices), (prices, width)  # max gain > 0 every step
        assert prices == list(band.ladder(D(str(settings.limit_tick))))[: len(out.attempts)]
        assert len({a.client_order_id for a in out.attempts}) == len(out.attempts)
        assert all(a.broker_order_id for a in out.attempts)
        if out.fill_price is not None:
            assert out.fill_price < width, (out.fill_price, width)

        # -- audit trail --------------------------------------------------------
        orders = conn.execute(
            "SELECT id, state FROM orders WHERE proposal_hash = ?", (phash,)
        ).fetchall()
        assert len(orders) == len(out.attempts)
        assert all(o["state"] in ("filled", "cancelled", "partially_filled") for o in orders)
        for o in orders:
            trail = [
                e[0]
                for e in conn.execute(
                    "SELECT to_state FROM order_events WHERE order_id = ? ORDER BY id", (o["id"],)
                )
            ]
            assert trail[:3] == ["gated", "approved", "submitted"], trail
            assert trail[-1] == o["state"], trail
        ex = ExecutionRepo(conn).get(phash)
        assert ex is not None and ex["status"] == str(out.status)
        assert ex["attempts"] == len(orders)
        codes = [
            r[0]
            for r in conn.execute(
                "SELECT reason_code FROM decisions WHERE proposal_hash = ? ORDER BY rowid", (phash,)
            )
        ]
        assert codes[0] == "owner_approve", codes
        assert codes.count("order:step") == len(out.attempts), codes
        if out.filled_qty:
            assert out.structure_id is not None
            assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] >= 1
            assert OpenStructureRepo(conn).get(out.structure_id) is not None
        else:
            final = broker.order_status(out.attempts[-1].broker_order_id or "")
            assert final.status in ("canceled", "filled", "expired"), final.status
    finally:
        # -- cleanup: close whatever filled through the same check/gate/approval/ladder --
        if out.filled_qty:
            held = {long_leg.symbol: out.filled_qty, short_leg.symbol: -out.filled_qty}
            tries = _close_with_retry(
                conn=conn, broker=broker, data=data, settings=settings,
                legs=exit_legs(proposal.structure), cid=cid, held=held,
                structure_id=out.structure_id,
            )  # fmt: skip
            left = {
                s: q - before.get(s, 0)
                for s, q in _held(broker, symbols).items()
                if q != before.get(s, 0)
            }
            # (c) a final failure still fails the test and names the legs
            assert not left, (
                f"cleanup close did not fill ({' | '.join(tries)}); "
                f"close these paper legs by hand: {left}"
            )
