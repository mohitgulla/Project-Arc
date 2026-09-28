"""E6.2 exits (E2.4 policy on open structures) and the Investor handler.

Market: the bundled SPY recording (``PipelineEnv.fixtures()``, as of 2026-09-25
16:00 ET). The open structure is the 711/710 Oct-30 bull put that
tests/test_routines_e53.py values (mid close debit ≈ 0.30/share).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest

from arc.approvals.service import approval_record
from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.execution.exits import exit_legs, propose_exits
from arc.gate import HaltSwitch, Portfolio
from arc.gate.inputs import AccountSnapshot
from arc.models import LegIntent, Structure
from arc.routines.config import RoutinesConfig
from arc.routines.handlers import JobContext, JobSkippedError
from arc.routines.investor import investor, load_approved
from arc.routines.runs import RoutineEvent
from arc.store.db import connect
from arc.store.execution import OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo, HaltRepo, ProposalRepo
from arc.structures import credit_vertical
from arc.utils.calendar import ET
from tests.test_execution_ladder import ScriptedBroker

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 9, 25, 16, 0, 5, tzinfo=ET)  # quotes are 15:59:59 ET
EXP = dt.date(2026, 10, 30)
SECRET = "g" * 40


def settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "gate_secret": SECRET, "env": "paper"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def bull_put() -> Structure:
    return credit_vertical(
        "put", "SPY", EXP, short_strike=711, short_premium="5.60",
        long_strike=710, long_premium="4.70", as_of=NOW.date(),
    )  # fmt: skip


def open_structure(conn: sqlite3.Connection, entry: str = "-0.90", n: int = 2) -> str:
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7
    )
    st = bull_put()
    ProposalRepo(conn).insert(
        candidate_id=cid,
        proposal_hash="a" * 64,
        structure_json=st.model_dump_json(),
        thesis="entry",
        quant_json="{}",
        sizing_json="{}",
        expires_at=NOW.isoformat(),
        ticker="SPY",
    )
    return OpenStructureRepo(conn).open(
        ticker="SPY",
        open_proposal_hash="a" * 64,
        candidate_id=cid,
        structure_json=st.model_dump_json(),
        contracts=n,
        entry_net=D(entry),
        now=NOW - dt.timedelta(days=5),
    )


def held(n: int = 2) -> Portfolio:
    legs = {leg.occ_symbol: (n if leg.side == LegIntent.LONG else -n) for leg in bull_put().legs}
    return Portfolio(legs=legs)


def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=D("100000"), last_equity=D("100000"), as_of=NOW - dt.timedelta(seconds=5)
    )


def market() -> Any:
    from arc.pipeline.env import PipelineEnv

    return PipelineEnv.fixtures().market


class Writes:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind: str, subject: str, payload: Any) -> None:
        self.calls.append((kind, subject))
        ContextStore(self.conn).write(
            kind=kind, subject=subject, payload=payload, produced_by="monitor",
            ttl="6h", run_id="r", now=NOW,
        )  # fmt: skip


def propose(conn: sqlite3.Connection, *, now: dt.datetime = NOW, **kw: Any):
    base: dict[str, Any] = {
        "market": market(),
        "settings": settings(),
        "account": account(),
        "portfolio": held(),
        "switch": HaltSwitch(HaltRepo(conn)),
        "now": now,
        "run_id": "r",
        "write_context": Writes(conn),
        "mint": True,
    }
    base.update(kw)
    return propose_exits(conn, **base)


# ---------------------------------------------------------------------------
# propose_exits
# ---------------------------------------------------------------------------


def test_exit_legs_invert_every_intent() -> None:
    legs = exit_legs(bull_put())
    flip = {LegIntent.LONG: LegIntent.SHORT, LegIntent.SHORT: LegIntent.LONG}
    want = {(leg.occ_symbol, flip[LegIntent(leg.side)]) for leg in bull_put().legs}
    assert {(s, i) for s, i, _ in legs} == want


def test_take_profit_fires_and_proposes_close(conn: sqlite3.Connection) -> None:
    sid = open_structure(conn)
    run = propose(conn)
    assert run.evaluated == 1 and len(run.proposed) == 1, (run.lines, run.errors)
    phash = run.proposed[0]
    assert "exit take_profit" in run.lines[0] and "gate PASS" in run.lines[0], run.lines
    p = conn.execute("SELECT kind, day FROM proposals WHERE proposal_hash = ?", (phash,)).fetchone()
    assert p["kind"] == "close" and p["day"] == "2026-09-25"
    g = conn.execute("SELECT passed, token FROM gate_decisions WHERE proposal_hash = ?",
                     (phash,)).fetchone()  # fmt: skip
    assert g["passed"] == 1 and g["token"].startswith("arc2.")
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None and row["exit_proposal_hash"] == phash
    assert row["exit_reason"] == "take_profit"
    j = conn.execute("SELECT reason_code FROM decisions WHERE proposal_hash = ?", (phash,))
    assert [r[0] for r in j] == ["exit:take_profit"]


def test_one_exit_per_structure_per_day(conn: sqlite3.Connection) -> None:
    open_structure(conn)
    assert len(propose(conn).proposed) == 1
    again = propose(conn, now=NOW + dt.timedelta(minutes=30))
    assert again.proposed == [] and again.evaluated == 0


def test_nothing_fires_when_far_from_target(conn: sqlite3.Connection) -> None:
    open_structure(conn, entry="-0.03")  # close debit 0.02: P&L 0.01 of 0.03 max gain, no TP
    run = propose(conn)
    assert run.evaluated == 1 and run.proposed == []


def test_no_exit_proposals_while_halted(conn: sqlite3.Connection) -> None:
    open_structure(conn)
    sw = HaltSwitch(HaltRepo(conn))
    sw.halt(actor="U1", reason="x", now=NOW)
    run = propose(conn, switch=sw)
    assert run.proposed == [] and run.lines == ["halted: no exit proposals"]


def test_dry_run_never_mints(conn: sqlite3.Connection) -> None:
    open_structure(conn)
    run = propose(conn, mint=False)
    (phash,) = run.proposed
    tok = conn.execute("SELECT token FROM gate_decisions WHERE proposal_hash = ?", (phash,))
    assert tok.fetchone()[0] is None


def test_close_mismatch_fails_gate(conn: sqlite3.Connection) -> None:
    """Arc thinks it holds the structure, the broker doesn't: the exit is not actionable."""
    open_structure(conn)
    run = propose(conn, portfolio=Portfolio())
    (phash,) = run.proposed
    assert "close_mismatch" in run.lines[0]
    j = conn.execute("SELECT reason_code FROM decisions WHERE proposal_hash = ?", (phash,))
    assert [r[0] for r in j] == ["exit:not_proposed"]


def test_unpriceable_structure_is_reported(conn: sqlite3.Connection) -> None:
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7
    )
    st = credit_vertical(
        "put", "SPY", dt.date(2027, 1, 15), short_strike=711, short_premium="5",
        long_strike=710, long_premium="4", as_of=NOW.date(),
    )  # fmt: skip
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash="b" * 64, structure_json=st.model_dump_json(),
        thesis="t", quant_json="{}", sizing_json="{}", expires_at=NOW.isoformat(), ticker="SPY",
    )  # fmt: skip
    OpenStructureRepo(conn).open(
        ticker="SPY", open_proposal_hash="b" * 64, candidate_id=cid,
        structure_json=st.model_dump_json(), contracts=1, entry_net=D("-1"), now=NOW,
    )  # fmt: skip
    run = propose(conn)
    assert run.proposed == [] and "cannot evaluate exit" in run.errors[0]


# ---------------------------------------------------------------------------
# Investor handler: approval event -> ladder -> card
# ---------------------------------------------------------------------------


def _approve(conn: sqlite3.Connection, phash: str) -> None:
    from arc.approvals.service import ApprovalService, LogCardPoster

    svc = ApprovalService(conn, settings(), LogCardPoster())
    svc.publish_pending(NOW)
    res = svc.decide(phash, user="U0C5KUMH28G", approve=True, now=NOW + dt.timedelta(seconds=30))
    assert res.outcome.value == "approved", res


def _ctx(conn: sqlite3.Connection, phash: str | None, now: dt.datetime) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"personas": {"investor": {"trigger": "approval", "llm": False, "notify": "card"}}}
    )
    kind, step = routines.step("investor")
    ev = (
        RoutineEvent(id="e1", name="approval", payload={"proposal_hash": phash}, created_at=now)
        if phash
        else None
    )
    return JobContext(
        job="investor", kind=kind, spec=step, run_id="run-i", chain_run_id=None,
        scheduled_for=now, now=now, conn=conn, snapshot=ContextStore(conn).snapshot(now),
        routines=routines, event=ev, settings_factory=lambda: settings(),
    )  # fmt: skip


def test_investor_closes_the_structure(conn: sqlite3.Connection) -> None:
    sid = open_structure(conn)
    (phash,) = propose(conn).proposed
    _approve(conn, phash)
    proposal, decision, kind, ticker, got_sid = load_approved(conn, phash)
    assert kind == "close" and got_sid == sid and ticker == "SPY"
    assert decision is not None and approval_record(conn, phash) is not None

    clock_t = [NOW + dt.timedelta(minutes=1)]
    b = ScriptedBroker([["filled"]], fills={0: (2, str(proposal.limit_price))})
    res = investor(
        _ctx(conn, phash, clock_t[0]),
        broker=b,
        clock=lambda: clock_t[0],
        sleep=lambda s: None,
        market_open=lambda t: True,
    )
    assert res.metrics["status"] == "filled" and res.metrics["steps_used"] == 0
    assert res.card is not None
    assert "[Investor] Order: SPY Close Position • x2" in res.card.text
    assert "Filled on attempt*\\n1 of" in str(res.card.blocks).replace("\n", "\\n")
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None and row["status"] == "closed"
    (order,) = b.orders
    assert {(leg.symbol, leg.side) for leg in order.legs} == {
        (s, "buy" if i == LegIntent.LONG else "sell") for s, i, _ in exit_legs(bull_put())
    }


def test_investor_refuses_outside_rth(conn: sqlite3.Connection) -> None:
    open_structure(conn)
    (phash,) = propose(conn).proposed
    _approve(conn, phash)
    b = ScriptedBroker([["filled"]])
    res = investor(
        _ctx(conn, phash, NOW), broker=b, clock=lambda: NOW, sleep=lambda s: None,
        market_open=lambda t: False,
    )  # fmt: skip
    assert "market closed" in res.summary and b.orders == []


def test_investor_needs_an_event(conn: sqlite3.Connection) -> None:
    with pytest.raises(JobSkippedError):
        investor(
            _ctx(conn, None, NOW), broker=ScriptedBroker([]), clock=lambda: NOW,
            sleep=lambda s: None, market_open=lambda t: True,
        )  # fmt: skip


def test_investor_handler_registered() -> None:
    from arc.routines.handlers import BUILTIN_HANDLERS

    assert BUILTIN_HANDLERS["investor"] == "arc.routines.investor:investor_step"


# ---------------------------------------------------------------------------
# Portfolio: two expirations on one root (Sentinel S-5)
# ---------------------------------------------------------------------------


def test_build_portfolio_two_expirations_same_root(conn: sqlite3.Connection) -> None:
    from arc.broker.base import BrokerPosition
    from arc.pipeline.market import build_portfolio

    def pos(sym: str, qty: int, px: str) -> BrokerPosition:
        return BrokerPosition(
            symbol=sym, qty=D(qty), side="long" if qty > 0 else "short", avg_entry_price=D(px)
        )

    positions = [
        pos("SPY261030P00711000", -1, "5.10"),
        pos("SPY261030P00710000", 1, "4.80"),
        pos("SPY261106P00716000", -1, "4.50"),
        pos("SPY261106P00715000", 1, "4.20"),
    ]
    one = build_portfolio(conn, positions[:2], market(), now=NOW, wash_sale_days=30, r=0.04)
    assert len(one.positions) == 1
    # Same root, different expiry must be two structures (no PortfolioError).
    grouped = build_portfolio(conn, positions, market(), now=NOW, wash_sale_days=30, r=0.04)
    assert len(grouped.positions) == 2
    assert grouped.legs["SPY261030P00711000"] == -1


def test_build_portfolio_unvaluable_position_raises(conn: sqlite3.Connection) -> None:
    """A naked short call has unbounded risk: the portfolio refuses to value it (S-5)."""
    from arc.broker.base import BrokerPosition
    from arc.pipeline.market import PortfolioError, build_portfolio

    naked = [
        BrokerPosition(symbol="SPY261030C00711000", qty=D(-1), side="short", avg_entry_price=D("5"))
    ]
    with pytest.raises(PortfolioError):
        build_portfolio(conn, naked, market(), now=NOW, wash_sale_days=30, r=0.04)
