"""E9.3: retrospective journal views — explain, scorecard attribution, counterfactual."""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.data.history.base import OptionEodRow, OptionRight
from arc.data.history.store import ParquetHistoryStore
from arc.journal.models import DecisionReview, OutcomeRecord, OutcomeStatus
from arc.journal.reasons import (
    Choice,
    JournalPersona,
    ReasonCode,
    Reviewer,
    ReviewLabel,
    RootCause,
    Stage,
)
from arc.journal.report import ShadowPricer, counterfactual, gaps
from arc.journal.store import JournalStore
from arc.journal.views import MIN_SAMPLE, attribution, explain, parse_by
from arc.models import OrderState, Structure
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.store.db import connect_ro
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.repos import FillRepo, OrderRepo
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

NOW = _dt.datetime(2026, 11, 2, 12, tzinfo=ET)  # after the fixture legs expire (2026-10-30)


def _at(day: int, hour: int = 11, month: int = 9) -> _dt.datetime:
    return _dt.datetime(2026, month, day, hour, tzinfo=ET)


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    conn, report = fixture_run(
        ArcSettings(_env_file=None, account_profile="margin"),  # type: ignore[call-arg]
        load_routines(),
    )
    assert len(report.proposals) == 1
    return conn.serialize()


@pytest.fixture
def conn(_pipeline_db: bytes) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.deserialize(_pipeline_db)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _row(conn: sqlite3.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM proposals WHERE kind = 'open'").fetchone())


def _to_disk(conn: sqlite3.Connection, path: Path) -> Path:
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    return path


def _trade_and_close(conn: sqlite3.Connection, *, contracts: int = 2) -> dict[str, Any]:
    """Approve, fill (orders + events + fills), open, close early; journal + outcome rows."""
    p = _row(conn)
    ph = p["proposal_hash"]
    st = Structure.model_validate_json(p["structure_json"])
    fill = st.net_debit_credit + Decimal("0.01")
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               channel, expires_at, created_at, decided_at, decided_by)
           VALUES (?, ?, '2026-09-25', '{}', 'approved', 'log', ?, ?, ?, 'U0OWNER')""",
        (ph, p["ticker"], _at(25, 15).isoformat(), _at(25, 9).isoformat(),
         _at(25, 9).isoformat()),
    )  # fmt: skip
    conn.execute(
        """INSERT INTO approvals (id, proposal_hash, slack_user, slack_ts, decision, decided_at)
           VALUES ('appr-1', ?, 'U0OWNER', '1.2', 'approved', ?)""",
        (ph, _at(25, 9).isoformat()),
    )
    conn.execute("UPDATE gate_decisions SET token = 'arc2.secret-token'")
    orders = OrderRepo(conn)
    oid = orders.create(
        proposal_hash=ph, client_order_id="arc2.secret-token.s0", created_at=_at(25, 10).isoformat()
    )
    for i, state in enumerate(
        (OrderState.GATED, OrderState.APPROVED, OrderState.SUBMITTED, OrderState.FILLED)
    ):
        orders.transition(
            order_id=oid,
            to_state=state,
            actor="ladder",
            event_at=(_at(25, 10) + _dt.timedelta(seconds=i + 1)).isoformat(),
        )
    FillRepo(conn).insert(
        order_id=oid,
        qty=contracts,
        price=str(fill),
        filled_at=(_at(25, 10) + _dt.timedelta(seconds=4)).isoformat(),
    )
    ex = ExecutionRepo(conn)
    ex.start(proposal_hash=ph, kind="open", token_version="arc2", band_lo=fill, band_hi=fill,
             max_steps=3, contracts=contracts, now=_at(25, 10))  # fmt: skip
    ex.finish(ph, status="filled", now=_at(25, 10), filled_qty=contracts, fill_price=fill)
    repo = OpenStructureRepo(conn)
    sid = repo.open(
        ticker=p["ticker"],
        open_proposal_hash=ph,
        candidate_id=p["candidate_id"],
        structure_json=p["structure_json"],
        contracts=contracts,
        entry_net=fill,
        now=_at(25, 10),
    )
    close_net = Decimal("0.70")
    repo.set_exit(sid, proposal_hash="close-x", reason="take_profit", day="2026-10-05")
    repo.reduce(sid, closed_qty=contracts, close_net=close_net, now=_at(5, 14, month=10))
    pnl = -(fill + close_net) * 100 * contracts
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.INVESTOR,
            stage=Stage.EXIT,
            subject=p["ticker"],
            choice=Choice.FILLED,
            reason_code=ReasonCode.EXIT_CLOSED,
            payload={"structure_id": sid, "realized_pnl": str(pnl)},
            at=_at(5, 14, month=10),
        )
        JournalStore(conn).record_outcome(
            OutcomeRecord(
                proposal_hash=ph,
                status=OutcomeStatus.CLOSED,
                contracts=contracts,
                limit_price=st.net_debit_credit,
                entry_fill=fill,
                slippage_usd=Decimal("2"),
                slippage_bps=3.0,
                cost_bps=203.9,
                exit_fill=-close_net,
                realised_pnl=pnl,
                exit_reason="take_profit",
                ev_total=Decimal("10"),
                pnl_vs_ev=pnl - 10,
                hold_to_expiry_shadow_pnl=Decimal("330"),
                at=_at(5, 14, month=10),
            )
        )
    return {"hash": ph, "sid": sid, "pnl": pnl, "order_id": oid, "fill": fill}


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def test_explain_full_tree_for_closed_proposal(conn: sqlite3.Connection) -> None:
    t = _trade_and_close(conn)
    ph = t["hash"]
    j = JournalStore(conn)
    cite = j.decisions(proposal_hash=ph)[0].id
    j.add_review(
        DecisionReview(
            proposal_hash=ph,
            label=ReviewLabel.GOOD_DECISION_GOOD_OUTCOME,
            root_cause=RootCause.TIMING,
            reviewer=Reviewer.OWNER,
            cites=[cite],
            at=_at(6, month=10),
        )
    )
    rep = explain(conn, ph[:10])
    (doc,) = rep.proposals
    assert doc.proposal_hash == ph and doc.status == "closed" and doc.ticker == "SPY"
    assert rep.chain_run_id and rep.chain_run_id.startswith("chain-")
    # persona prompts' sha256 + replies
    assert [c.persona for c in doc.persona_calls] == ["director", "quant", "risk"]
    assert all(len(c.prompt_sha256) == 64 and c.raw_response for c in doc.persona_calls)
    # gate verdict + violations, token withheld
    assert doc.gate is not None and doc.gate.passed and doc.gate.violations == []
    assert doc.gate.token_present
    # approval record
    assert doc.approval is not None
    assert (doc.approval.request_status, doc.approval.decision) == ("approved", "approved")
    assert doc.approval.approver == "U0OWNER"
    # order / fill timeline, in time order
    kinds = [(e.event, e.to_state) for e in doc.timeline]
    assert kinds == [
        ("order_created", "filled"),
        ("transition", "gated"),
        ("transition", "approved"),
        ("transition", "submitted"),
        ("transition", "filled"),
        ("fill", None),
    ]
    assert doc.timeline[-1].qty == 2 and Decimal(doc.timeline[-1].price or "0") == t["fill"]
    # outcome row
    o = doc.outcome
    assert o is not None and o.realised_pnl == t["pnl"] and o.exit_reason == "take_profit"
    assert o.pnl_vs_ev == t["pnl"] - 10 and o.slippage_bps == 3.0
    assert o.hold_to_expiry_shadow_pnl == Decimal("330")
    assert doc.position is not None and doc.position["status"] == "closed"
    assert len(doc.reviews) == 1 and doc.reviews[0].cites == [cite]
    assert {d.stage for d in doc.decisions} >= {Stage.PROPOSE, Stage.SIZING, Stage.GATE}
    # the JSON document never carries the gate token (nor the client order id built from it)
    text = rep.model_dump_json()
    assert "secret-token" not in text
    assert json.loads(text)["proposals"][0]["outcome"]["exit_reason"] == "take_profit"


def test_explain_open_proposal_has_no_outcome(conn: sqlite3.Connection) -> None:
    ph = _row(conn)["proposal_hash"]
    rep = explain(conn, ph)
    (doc,) = rep.proposals
    assert doc.outcome is None and doc.timeline == [] and doc.position is None
    assert doc.status == "proposed" and doc.gate is not None and not doc.gate.token_present
    # rejected at the approval step: still explained, still no outcome
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               reason, channel, expires_at, created_at, decided_at, decided_by)
           VALUES (?, 'SPY', '2026-09-25', '{}', 'rejected', 'IV too low', 'log', ?, ?, ?, 'U0')""",
        (ph, _at(25, 15).isoformat(), _at(25, 9).isoformat(), _at(25, 9).isoformat()),
    )  # fmt: skip
    doc = explain(conn, ph).proposals[0]
    assert doc.status == "rejected" and doc.outcome is None
    assert doc.approval is not None and doc.approval.reason == "IV too low"


def test_explain_by_run_and_chain_id(conn: sqlite3.Connection) -> None:
    p = _row(conn)
    by_run = explain(conn, p["run_id"])
    by_chain = explain(conn, p["chain_run_id"])
    assert [d.proposal_hash for d in by_run.proposals] == [p["proposal_hash"]]
    assert by_run.chain_run_id == by_chain.chain_run_id == p["chain_run_id"]
    # decisions of the chain not tied to a proposal (e.g. the Director's drops) are kept
    assert any(d.stage is Stage.SHORTLIST for d in by_chain.chain_decisions)
    with pytest.raises(LookupError):
        explain(conn, "run-does-not-exist")
    with pytest.raises(LookupError):
        explain(conn, "ffff")


def test_explain_run_outside_a_chain_lists_its_decisions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, reason, scheduled_for, status)
           VALUES ('run-recon', 'reconcile', 'manual', '2026-09-29T20:31:41Z', 'ok')"""
    )
    JournalStore(conn).record(
        persona=JournalPersona.AUDITOR,
        stage=Stage.RECONCILE,
        subject="SPY",
        choice=Choice.FAILED,
        reason_code=ReasonCode.RECONCILE_MISMATCH,
        run_id="run-recon",
        at=_at(29),
    )
    rep = explain(conn, "run-recon")
    assert rep.chain_run_id is None and rep.proposals == []
    assert [(d.subject, d.reason_code) for d in rep.chain_decisions] == [
        ("SPY", ReasonCode.RECONCILE_MISMATCH)
    ]


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_attribution_flags_low_sample(conn: sqlite3.Connection) -> None:
    t = _trade_and_close(conn)
    rep = attribution(conn, since=_at(1), until=NOW, by="kind,regime,persona_model")
    assert rep.trades == 1 and rep.by == ["kind", "regime", "persona_model"]
    (b,) = rep.buckets
    assert b.key == {
        "kind": "iron_condor",
        "regime": "risk_on",
        "persona_model": "director=fixture,quant=fixture,risk=fixture",
    }
    assert b.n == 1 and b.low_sample is True and rep.min_sample == MIN_SAMPLE == 30
    assert b.realised_pnl == pytest.approx(float(t["pnl"]))
    assert b.win_rate == (1.0 if t["pnl"] > 0 else 0.0)
    ev = json.loads(_row(conn)["quant_json"])["ev"]
    assert b.avg_pnl_vs_ev == pytest.approx(float(t["pnl"]) - float(ev) * 2)
    # realised vs modelled entry slippage: 1c worse than mid on 2 contracts = $2
    assert b.n_slippage == 1 and b.slippage_realised_usd == pytest.approx(2.0)
    assert b.slippage_modelled_usd is not None and b.slippage_modelled_usd > 0
    # a window after the close sees nothing; outside the window nothing leaks in
    assert attribution(conn, since=_at(6, month=10), until=NOW).trades == 0
    assert attribution(conn, since=None, until=_at(1, month=10)).trades == 0


def test_attribution_bucket_at_min_sample_is_not_low(conn: sqlite3.Connection) -> None:
    """Outcome rows (no local position) count too; n == MIN_SAMPLE clears the flag."""
    src = _row(conn)
    for i in range(MIN_SAMPLE):
        ph = f"h{i:02d}-" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
                   quant_json, sizing_json, expires_at, created_at, ticker, kind, regime)
               VALUES (?, ?, ?, ?, 't', ?, '{}', ?, ?, 'SPY', 'open', 'trend')""",
            (uuid.uuid4().hex, src["candidate_id"], ph, src["structure_json"],
             src["quant_json"], src["expires_at"], src["created_at"]),
        )  # fmt: skip
        JournalStore(conn).record_outcome(
            OutcomeRecord(
                proposal_hash=ph,
                status=OutcomeStatus.CLOSED,
                realised_pnl=Decimal(i % 3 - 1),
                pnl_vs_ev=Decimal(0),
                at=_at(26),
            )
        )
    rep = attribution(conn, since=None, until=NOW, by=["regime"])
    by = {b.key["regime"]: b for b in rep.buckets}
    assert by["trend"].n == MIN_SAMPLE and by["trend"].low_sample is False
    assert by["trend"].win_rate == pytest.approx(10 / 30)
    assert all(r.source == "outcome" for r in rep.rows)


def test_attribution_rejects_unknown_dimension() -> None:
    assert parse_by(" kind , regime,kind") == ["kind", "regime"]
    with pytest.raises(ValueError, match="--by"):
        parse_by("kind,colour")
    with pytest.raises(ValueError, match="--by"):
        parse_by("")


# ---------------------------------------------------------------------------
# counterfactual
# ---------------------------------------------------------------------------


def _eod(root: Path, legs: list[dict[str, Any]], day: _dt.date, price: float) -> None:
    """Cache one EOD session where every leg closes at *price* (bid = ask)."""
    rows = []
    for leg in legs:
        occ = parse_occ(leg["occ_symbol"])
        rows.append(
            OptionEodRow(
                provider="alpaca",
                underlying=occ.root,
                date=day,
                symbol=occ.format().replace(" ", ""),
                expiration=occ.expiration,
                strike=float(occ.strike),
                right=OptionRight.CALL if occ.kind.value == "c" else OptionRight.PUT,
                close=price,
                bid=price,
                ask=price,
            )
        )
    ParquetHistoryStore(root).write_day("alpaca", "SPY", day, rows)


def test_counterfactual_matches_gaps_report(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ph = _row(conn)["proposal_hash"]
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               channel, expires_at, created_at, decided_at, decided_by)
           VALUES (?, 'SPY', '2026-09-25', '{}', 'expired', 'log', ?, ?, ?, 'arc:ttl')""",
        (ph, _at(25, 15).isoformat(), _at(25, 9).isoformat(), _at(25, 15).isoformat()),
    )  # fmt: skip
    legs = json.loads(_row(conn)["structure_json"])["legs"]
    _eod(tmp_path, legs, _dt.date(2026, 10, 9), 1.0)
    pricer = ShadowPricer(tmp_path)
    g = gaps(conn, pricer=pricer)
    cf = counterfactual(conn, since=None, until=NOW, pricer=pricer)
    # one implementation: the counterfactual's not-traded rows are the gaps rows
    assert len(g.not_traded) == len(cf.not_traded) == 1
    (a,), (b,) = g.not_traded, cf.not_traded
    assert (a.proposal_hash, a.status, a.contracts) == (b.proposal_hash, b.status, b.contracts)
    assert b.status == "expired" and b.as_of == _dt.date(2026, 10, 9)
    assert a.shadow_pnl is not None and b.shadow_pnl == pytest.approx(float(a.shadow_pnl))
    # every leg at 1.00: condor net mark 0 → P&L = -entry x 100 x contracts
    entry = Decimal(json.loads(_row(conn)["structure_json"])["net_debit_credit"])
    assert a.shadow_pnl == -entry * 100 * a.contracts
    assert cf.not_traded_shadow_total == pytest.approx(float(a.shadow_pnl))
    assert [x.better_by for x in cf.alternatives] == [
        None if y.better_by is None else float(y.better_by) for y in g.alternatives
    ]
    assert "── proposals not traded" in "\n".join(g.lines())
    assert cf.closed == []


def test_counterfactual_closed_trade_vs_hold_and_no_trade(conn: sqlite3.Connection) -> None:
    t = _trade_and_close(conn)
    cf = counterfactual(conn, since=_at(1), until=NOW, pricer=None)
    (c,) = cf.closed
    assert c.proposal_hash == t["hash"] and c.source == "position" and c.early
    assert c.realised_pnl == pytest.approx(float(t["pnl"]))
    assert c.no_trade_pnl == 0.0 and c.no_trade_minus_realised == pytest.approx(-float(t["pnl"]))
    # no settlement known offline and no reconcile row: the shadow stays pending
    assert c.hold_to_expiry_shadow_pnl is None and cf.hold_total is None
    assert cf.not_traded == [] and cf.shadow_source.startswith("n/a")


def test_counterfactual_uses_outcome_shadow_without_a_position(conn: sqlite3.Connection) -> None:
    ph = _row(conn)["proposal_hash"]
    JournalStore(conn).record_outcome(
        OutcomeRecord(
            proposal_hash=ph,
            status=OutcomeStatus.CLOSED,
            realised_pnl=Decimal("120"),
            hold_to_expiry_shadow_pnl=Decimal("200"),
            exit_reason="profit_take_50",
            at=_at(9, month=10),
        )
    )
    cf = counterfactual(conn, since=None, until=NOW)
    (c,) = cf.closed
    assert c.source == "outcome" and c.early is True
    assert c.hold_minus_realised == pytest.approx(80.0)
    assert cf.hold_total == pytest.approx(200.0) and cf.realised_on_hold_known == 120.0


# ---------------------------------------------------------------------------
# read-only CLI
# ---------------------------------------------------------------------------

_WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")


def test_journal_cli_is_read_only(
    conn: sqlite3.Connection,
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arc.cli import main
    from arc.store import db as store_db

    _trade_and_close(conn)
    db = _to_disk(conn, tmp_path / "arc.db")
    capsys.readouterr()  # drop the setup's log lines
    before = db.read_bytes()
    statements: list[str] = []
    opened: list[str] = []
    real = store_db.connect_ro

    def traced(path: Any = None) -> sqlite3.Connection:
        c = real(path)
        c.set_trace_callback(statements.append)
        opened.append(str(path))
        return c

    monkeypatch.setattr(store_db, "connect_ro", traced)

    def forbid(*a: Any, **k: Any) -> None:
        raise AssertionError("read-only views must not open a writable connection")

    monkeypatch.setattr(store_db, "connect", forbid)
    ph = _row(conn)["proposal_hash"]
    runs = [
        ["journal", "explain", ph[:12], "--json", "--db", str(db)],
        ["journal", "explain", ph[:12], "--db", str(db)],
        ["journal", "counterfactual", "--since", "2026-09-01", "--until", "2026-11-02", "--json",
         "--db", str(db), "--data-dir", str(tmp_path)],
        ["journal", "counterfactual", "--db", str(db), "--data-dir", str(tmp_path)],
        ["scorecard", "attribution", "--since", "2026-09-01", "--until", "2026-11-02",
         "--by", "kind,regime,persona_model", "--json", "--db", str(db)],
        ["scorecard", "attribution", "--db", str(db)],
    ]  # fmt: skip
    outs = []
    for argv in runs:
        assert main(argv) == 0, argv
        outs.append(capsys.readouterr().out)
    assert len(opened) == len(runs) and statements
    writes = [s for s in statements if s.lstrip().upper().startswith(_WRITE_WORDS)]
    assert writes == []
    assert db.read_bytes() == before
    assert json.loads(outs[0])["proposals"][0]["status"] == "closed"
    assert json.loads(outs[2])["closed"][0]["source"] == "position"
    assert json.loads(outs[4])["buckets"][0]["low_sample"] is True
    # mode=ro really refuses a write, and a missing store is never created
    with real(db) as ro, pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO halts (id, at) VALUES ('x', 'y')")
    missing = tmp_path / "nope.db"
    assert main(["journal", "explain", ph[:12], "--db", str(missing)]) == 2
    assert main(["scorecard", "attribution", "--db", str(missing)]) == 2
    assert not missing.exists()
    assert main(["scorecard", "attribution", "--by", "colour", "--db", str(db)]) == 2


def test_connect_ro_refuses_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="never create"):
        connect_ro(tmp_path / "missing.db")
