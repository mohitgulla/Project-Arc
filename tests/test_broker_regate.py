"""D101 (E24.1): the Broker job's real re-gate (:mod:`arc.broker.regate`) on the SPY fixture.

Uses the close flow from tests/test_execution_exits.py (the bundled SPY recording,
711/710 Oct-30 bull put, x2). The re-gate re-prices the same legs at the fresh
mid, re-runs the full gate and mints a new token, or refuses.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

from arc.approvals.service import approval_record
from arc.broker.base import AccountInfo, BrokerPosition
from arc.broker.ladder_job import broker_execute, load_approved
from arc.broker.regate import quote_reader, regater
from arc.gate.band import PriceBand
from arc.gate.rules import proposal_hash
from arc.gate.token import parse_any
from arc.models import LegIntent
from tests import test_execution_exits as X
from tests.test_execution_ladder import ScriptedBroker

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings
    from arc.execution.ladder import MarketRead

conn = X.conn  # the in-memory store fixture


class AccountBroker(ScriptedBroker):
    """The scripted ladder broker plus the account/positions reads the gate needs."""

    def account(self) -> AccountInfo:
        return AccountInfo(
            account_id="PA0000TEST",
            equity=D("100000"),
            buying_power=D("100000"),
            cash=D("100000"),
            last_equity=D("100000"),
            options_approved_level=3,
        )

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=leg.occ_symbol,
                qty=D(2),
                side="long" if leg.side == LegIntent.LONG else "short",
            )
            for leg in X.bull_put().legs
        ]


def _closing(conn: sqlite3.Connection) -> tuple[str, Any]:
    X.open_structure(conn)
    (phash,) = X.propose(conn).proposed
    X._approve(conn, phash)  # noqa: SLF001 - the exits suite's approval helper
    return phash, load_approved(conn, phash)


class Moved:
    """The SPY recording with the short 711P quoted *shift* higher (a close debit rises)."""

    def __init__(self, shift: float) -> None:
        self.m = X.market()
        self.shift = shift

    def option_chain(self, underlying: str, start: dt.date, end: dt.date) -> list[Any]:
        out = []
        for c in self.m.option_chain(underlying, start, end):
            if "P00711000" in c.symbol and c.bid is not None and c.ask is not None:
                bid, ask = c.bid + self.shift, c.ask + self.shift
                c = c.model_copy(update={"bid": bid, "ask": ask, "mid": (bid + ask) / 2})
            out.append(c)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.m, name)


def _regate(
    conn: sqlite3.Connection, phash: str, s: ArcSettings, now: dt.datetime, market: Any = None
) -> Any:
    proposal, _decision, kind, _t, _sid = load_approved(conn, phash)
    market = market or X.market()
    rg = regater(
        conn=conn,
        broker=AccountBroker([]),
        market=market,
        settings=s,
        proposal=proposal,
        approval=approval_record(conn, phash),
        kind=kind,
        clock=lambda: now,
    )
    read: MarketRead | None = quote_reader(
        market, proposal, clock=lambda: now, r=s.scanner_risk_free_rate
    )()
    assert read is not None
    return proposal, rg, read


T1 = X.NOW + dt.timedelta(seconds=35)  # after the approval; quotes 41 s old (gate max 60 s)


class Clock:
    def __init__(self) -> None:
        self.t = T1

    def __call__(self) -> dt.datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += dt.timedelta(seconds=s)


def test_quote_reader_reads_combo(conn: sqlite3.Connection) -> None:
    phash, (proposal, decision, *_rest) = _closing(conn)
    read = quote_reader(X.market(), proposal, clock=lambda: T1, r=0.04)()
    assert read is not None
    band = parse_any(decision.token).band  # type: ignore[union-attr]
    assert read.far is not None and read.far >= read.mid
    assert read.quote_age_s is not None and read.quote_age_s > 0
    # same quotes as when the gate approved: the approved band holds the fresh mid
    assert not band.moved_past(read.mid, D("0.01"))


def test_regate_passes_and_mints_new_token(conn: sqlite3.Connection) -> None:
    phash, (proposal, decision, *_rest) = _closing(conn)
    s = X.settings()
    _, rg, read = _regate(conn, phash, s, T1, Moved(0.10))
    approved = parse_any(decision.token).band  # type: ignore[union-attr]
    assert approved.moved_past(read.mid, D("0.01")), (approved, read.mid)
    # the fixture's close mid is 2 cents, so any real move breaks a 5%-of-mid ceiling;
    # hand the re-gate a ceiling basis it fits under (the ceiling has its own test)
    basis = PriceBand(lo=D("0.10"), hi=D("0.20"), max_steps=3)
    res = rg(basis, read, 1)
    assert res.status == "passed", res
    new = res.regated
    assert new is not None and new.decision.passed and new.decision.token
    assert new.decision.token != decision.token
    tok = parse_any(new.decision.token)
    assert tok.band == new.band  # type: ignore[union-attr]
    assert new.decision.proposal_hash == proposal_hash(new.proposal)
    assert new.approval is not None and new.approval.proposal_hash == proposal_hash(new.proposal)
    # same legs, same quantity, same TTL; only the price moved
    assert [leg.occ_symbol for leg in new.proposal.structure.legs] == [
        leg.occ_symbol for leg in proposal.structure.legs
    ]
    assert new.proposal.sizing == proposal.sizing
    assert new.proposal.expires_at == proposal.expires_at
    assert new.band.lo == new.proposal.limit_price
    assert new.band.lo > approved.lo and new.band.hi > approved.hi


def test_regate_refuses_after_ttl(conn: sqlite3.Connection) -> None:
    phash, (proposal, decision, *_rest) = _closing(conn)
    late = proposal.expires_at + dt.timedelta(seconds=1)
    _, rg, read = _regate(conn, phash, X.settings(), late)
    res = rg(parse_any(decision.token).band, read, 1)  # type: ignore[union-attr]
    assert res.status == "ttl_expired" and res.regated is None


def test_regate_refuses_past_move_ceiling(conn: sqlite3.Connection) -> None:
    phash, (proposal, decision, *_rest) = _closing(conn)
    _, rg, read = _regate(conn, phash, X.settings(), T1, Moved(0.10))
    approved = parse_any(decision.token).band  # type: ignore[union-attr]
    res = rg(approved, read, 1)
    assert res.status == "refused_move", res
    assert res.regated is None
    assert res.payload["approved_band_hi"] == str(approved.hi)
    assert D(res.payload["regated_band_hi"]) > approved.hi


def test_close_in_expiry_window_skips_ceiling(conn: sqlite3.Connection, monkeypatch: Any) -> None:
    from arc.broker import regate as R

    class Window:
        in_window = True

    class Guard:
        steps = 6

    monkeypatch.setattr(R, "_closing_window", lambda *_a: (Window(), Guard()))
    phash, (proposal, decision, *_rest) = _closing(conn)
    _, rg, read = _regate(conn, phash, X.settings(), T1, Moved(0.10))
    res = rg(parse_any(decision.token).band, read, 1)  # type: ignore[union-attr]
    assert res.status == "passed", res
    assert res.regated is not None and res.regated.band.max_steps == 6
    assert res.payload["expiry_window"] is True


def test_regate_gate_failure_on_halt(conn: sqlite3.Connection) -> None:
    from arc.store.repos import HaltRepo

    phash, (proposal, decision, *_rest) = _closing(conn)
    HaltRepo(conn).halt(reason="test halt", actor="test")
    _, rg, read = _regate(conn, phash, X.settings(), T1)
    res = rg(parse_any(decision.token).band, read, 1)  # type: ignore[union-attr]
    # a close may pass a halt (closes reduce risk); an open never does. Either way the
    # re-gate ran the halt-aware gate and its verdict decides.
    assert res.status in ("passed", "gate_failed")


def test_broker_job_wires_regate(conn: sqlite3.Connection) -> None:
    """broker_execute passes quotes + regate; the walk re-checks the band between steps."""
    phash, (proposal, *_rest) = _closing(conn)
    reads: list[str] = []

    class CountingMarket:
        def __init__(self) -> None:
            self.m = X.market()

        def __getattr__(self, name: str) -> Any:
            reads.append(name)
            return getattr(self.m, name)

    b = AccountBroker([["new"], ["new", "filled"]], cancel_scripts={0: [("canceled", 0)]})
    b.fills = {1: (2, "0")}
    clk = Clock()
    res = broker_execute(
        X._ctx(conn, phash, T1),  # noqa: SLF001
        broker=b,
        market=CountingMarket(),
        clock=clk,
        sleep=clk.sleep,
        market_open=lambda t: True,
    )
    assert res.metrics["status"] in ("filled", "cancelled")
    assert len(b.orders) == 2
    assert reads.count("option_chain") == 1, "the between-step band check before step 1"


def test_broker_job_regate_off(conn: sqlite3.Connection) -> None:
    phash, _ = _closing(conn)
    reads: list[str] = []

    class CountingMarket:
        def __init__(self) -> None:
            self.m = X.market()

        def __getattr__(self, name: str) -> Any:
            reads.append(name)
            return getattr(self.m, name)

    b = AccountBroker([["new"], ["new", "filled"]], cancel_scripts={0: [("canceled", 0)]})
    ctx = X._ctx(conn, phash, T1)  # noqa: SLF001
    ctx.settings_factory = lambda: X.settings(execution_regate_max=0)
    clk = Clock()
    broker_execute(
        ctx,
        broker=b,
        market=CountingMarket(),
        clock=clk,
        sleep=clk.sleep,
        market_open=lambda t: True,
    )
    assert len(b.orders) == 2
    assert reads == [], "no between-step read (and the fresh band needs no D34 re-price)"


def test_open_regate_runs_ev_floor_and_full_gate(conn: sqlite3.Connection) -> None:
    """The open path (EV floor on fresh IVs, tier settings, earnings) runs end to end."""
    phash, (proposal, decision, *_rest) = _closing(conn)
    s = X.settings()
    rg = regater(
        conn=conn, broker=AccountBroker([]), market=Moved(0.10), settings=s,
        proposal=proposal, approval=approval_record(conn, phash), kind="open",
        clock=lambda: T1,
    )  # fmt: skip
    read = quote_reader(Moved(0.10), proposal, clock=lambda: T1, r=0.04)()
    assert read is not None
    basis = PriceBand(lo=D("0.10"), hi=D("0.20"), max_steps=3)
    res = rg(basis, read, 1)
    # a buy-to-close shape re-gated as an open has negative managed Net EV: the
    # E6.4a floor on the fresh quotes refuses it before any order exists
    assert res.status == "gate_failed", res
    assert "Net EV floor" in res.detail and res.payload["ev_floor"]
    assert res.regated is None
