"""E7.5a: the scorecard gate in front of D34 auto-approve.

The fixture pipeline leaves one gate-passed, tokened open proposal (SPY iron condor,
2026-09-25). Closed trade history is added by cloning that proposal: each clone is
filled (with a chosen entry slippage), opened as a structure and closed by an order
with a chosen realised P&L, exactly as the ladder and the exit path record them.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from decimal import Decimal
from typing import Any

import pytest
import structlog

from arc.approvals.service import AUTO_APPROVER, ApprovalService, LogCardPoster, approval_record
from arc.config import ArcSettings
from arc.control.effective import effective_settings
from arc.control.registry import lookup
from arc.control.service import LOCAL_ACTOR, ControlService
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.scorecard import auto_approve_readiness, slippage_by_kind
from arc.journal.store import JournalStore
from arc.models import Structure
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.utils.calendar import ET
from tests.pre_d85_env import reset_env

NOW = _dt.datetime(2026, 9, 25, 16, 0, tzinfo=ET)  # fixture proposal expires 16:20 ET
# Fixture condor: modelled spread of all legs = $12.90 per unit, so half = $6.45;
# at the default tolerance 1.5 the slippage limit is $9.675 per contract.
HALF_SPREAD_UNIT = 6.45
FEES_UNIT = 0.18297  # entry fees per unit frozen in the fixture analytics


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
    c.execute("UPDATE gate_decisions SET token = 'tok-fixture'")
    c.commit()
    return c


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_env(
        monkeypatch,
        "ARC_AUTO_APPROVE",
        "ARC_AUTO_APPROVE_SCORECARD_GATE",
        "ARC_AUTO_APPROVE_MIN_CLOSED_TRADES",
        "ARC_AUTO_APPROVE_SLIPPAGE_TOLERANCE",
    )


def settings(**kw: Any) -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None, account_profile="margin", auto_approve=True, **kw
    )


def _phash(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT proposal_hash FROM proposals WHERE day IS NOT NULL").fetchone()
    return str(row[0])


def add_closed_trade(
    conn: sqlite3.Connection,
    i: int,
    *,
    pnl_per_contract: float,
    slip: Decimal = Decimal("0.01"),
    contracts: int = 1,
    closed: _dt.datetime | None = None,
) -> str:
    """Clone the fixture proposal as history ``hist-<i>``: filled, opened, closed early."""
    src = dict(conn.execute("SELECT * FROM proposals WHERE day IS NOT NULL").fetchone())
    phash = f"hist-{i:04d}"
    at = closed or (NOW - _dt.timedelta(days=60) + _dt.timedelta(hours=i))
    row = {
        **src,
        "id": f"p-{phash}",
        "proposal_hash": phash,
        "day": None,
        "chain_run_id": None,
        "fingerprint": None,
    }
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO proposals ({cols}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        tuple(row.values()),
    )
    mc = conn.execute(
        "SELECT payload FROM market_contexts WHERE proposal_hash = ?", (src["proposal_hash"],)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO market_contexts (id, proposal_hash, payload, created_at) VALUES (?, ?, ?, ?)",
        (f"mc-{phash}", phash, mc, at.isoformat()),
    )
    st = Structure.model_validate_json(src["structure_json"])
    fill = st.net_debit_credit + slip
    ex = ExecutionRepo(conn)
    ex.start(
        proposal_hash=phash,
        kind="open",
        token_version="v1",
        band_lo=fill,
        band_hi=fill,
        max_steps=3,
        contracts=contracts,
        now=at - _dt.timedelta(minutes=30),
    )
    ex.finish(phash, status="filled", now=at, filled_qty=contracts, fill_price=fill)
    repo = OpenStructureRepo(conn)
    sid = repo.open(
        ticker=src["ticker"],
        open_proposal_hash=phash,
        candidate_id=src["candidate_id"],
        structure_json=src["structure_json"],
        contracts=contracts,
        entry_net=fill,
        now=at - _dt.timedelta(minutes=30),
    )
    close_net = -fill - Decimal(str(pnl_per_contract)) / 100
    repo.reduce(sid, closed_qty=contracts, close_net=close_net, now=at)
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.INVESTOR,
            stage=Stage.EXIT,
            subject=src["ticker"],
            choice=Choice.FILLED,
            reason_code=ReasonCode.EXIT_CLOSED,
            payload={"structure_id": sid, "realized_pnl": str(pnl_per_contract * contracts)},
            at=at,
        )
    return sid


def history(conn: sqlite3.Connection, n: int, **kw: Any) -> None:
    for i in range(n):
        add_closed_trade(conn, i, **kw)


def gated_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {**dict(r), "payload": json.loads(r["payload"])}
        for r in conn.execute(
            "SELECT * FROM decisions WHERE reason_code = ? ORDER BY at", ("auto_approve_gated",)
        )
    ]


def publish(conn: sqlite3.Connection, s: ArcSettings) -> tuple[Any, LogCardPoster]:
    poster = LogCardPoster()
    return ApprovalService(conn, s, poster).publish_pending(NOW), poster


def assert_waits_for_click(conn: sqlite3.Connection, rep: Any, poster: LogCardPoster) -> None:
    ph = _phash(conn)
    assert rep.auto_approved == [] and rep.auto_gated == [ph]
    assert approval_record(conn, ph) is None
    status = conn.execute(
        "SELECT status FROM approval_requests WHERE proposal_hash = ?", (ph,)
    ).fetchone()[0]
    assert status == "pending"
    text = json.dumps(poster.posted[0][1].blocks)
    assert "arc_approval_" in text and "Approve" in text  # buttons: manual !approve path
    assert "Auto-approve held back (scorecard gate)" in text


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_auto_approve_gated_when_too_few_closed_trades(conn: sqlite3.Connection) -> None:
    history(conn, 29, pnl_per_contract=50.0)
    rep, poster = publish(conn, settings())
    assert_waits_for_click(conn, rep, poster)
    (row,) = gated_rows(conn)
    assert row["stage"] == "approval" and row["persona"] == "system"
    assert row["choice"] == "noted" and row["proposal_hash"] == _phash(conn)
    assert row["payload"]["failing"] == ["min_closed_trades"]
    assert row["payload"]["closed_trades"] == 29 and row["payload"]["env"] == "paper"
    assert "29 closed trades < 30 required" in row["reason_text"]


def test_auto_approve_gated_negative_realised_ev(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=-10.0)
    rep, poster = publish(conn, settings())
    assert_waits_for_click(conn, rep, poster)
    (row,) = gated_rows(conn)
    assert row["payload"]["failing"] == ["negative_realised_ev"]
    assert row["payload"]["realised_net_ev"] == pytest.approx(-10.0 - 2 * FEES_UNIT, abs=1e-3)


def test_fees_can_make_realised_ev_negative(conn: sqlite3.Connection) -> None:
    """Net EV is after fees: +$0.30/trade gross minus ~$0.37 of fees is negative."""
    history(conn, 30, pnl_per_contract=0.30)
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.failing == ["negative_realised_ev"] and r.realised_net_ev is not None
    assert r.realised_net_ev < 0 < r.realised_pnl


def test_auto_approve_gated_slippage_over_tolerance(conn: sqlite3.Connection) -> None:
    # $10.00 per contract > $6.45 x 1.5 = $9.675
    history(conn, 30, pnl_per_contract=50.0, slip=Decimal("0.10"))
    rep, poster = publish(conn, settings())
    assert_waits_for_click(conn, rep, poster)
    (row,) = gated_rows(conn)
    p = row["payload"]
    assert p["failing"] == ["slippage_over_tolerance"]
    assert p["realised_slippage"] == pytest.approx(300.0)
    assert p["half_spread"] == pytest.approx(30 * HALF_SPREAD_UNIT)


def test_slippage_just_inside_tolerance_passes(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0, slip=Decimal("0.09"))  # $9.00 <= $9.675
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.ok and r.failing == []


def test_auto_approve_allowed_when_gate_met(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0)
    rep, poster = publish(conn, settings())
    ph = _phash(conn)
    assert rep.auto_approved == [ph] and rep.auto_gated == []
    rec = approval_record(conn, ph)
    assert rec is not None and rec.slack_user == AUTO_APPROVER
    assert gated_rows(conn) == []
    text = conn.execute(
        "SELECT reason_text FROM decisions WHERE reason_code = 'auto_approve'"
    ).fetchone()[0]
    assert "scorecard gate met" in text
    assert "Auto-approved (paper)" in json.dumps(poster.posted[0][1].blocks)


def test_scorecard_gate_off_logs_warning_and_approves(conn: sqlite3.Connection) -> None:
    s = settings(auto_approve_scorecard_gate=False)
    with structlog.testing.capture_logs() as logs:
        rep, _ = publish(conn, s)
    ph = _phash(conn)
    assert rep.auto_approved == [ph] and rep.auto_gated == []
    warn = [e for e in logs if e["event"] == "approvals.auto_approve_scorecard_gate_off"]
    assert len(warn) == 1 and warn[0]["log_level"] == "warning"
    assert warn[0]["proposal_hash"] == ph and warn[0]["env"] == "paper"
    text = conn.execute(
        "SELECT reason_text FROM decisions WHERE reason_code = 'auto_approve'"
    ).fetchone()[0]
    assert "scorecard gate off" in text
    assert gated_rows(conn) == []


# ---------------------------------------------------------------------------
# Scope and window
# ---------------------------------------------------------------------------


def test_gate_default_off_in_paper_and_keys_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # D85: the shipped paper default is off (was on); D70 forces it on in live.
    monkeypatch.delenv("ARC_AUTO_APPROVE_SCORECARD_GATE", raising=False)
    s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.auto_approve_scorecard_gate is False
    assert s.auto_approve_min_closed_trades == 30
    assert s.auto_approve_slippage_tolerance == 1.5


def test_gate_off_is_noop_when_auto_approve_off(conn: sqlite3.Connection) -> None:
    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    rep, poster = publish(conn, s)
    assert rep.auto_approved == [] and rep.auto_gated == [] and gated_rows(conn) == []
    assert "held back" not in json.dumps(poster.posted[0][1].blocks)


def test_closes_are_not_gated(conn: sqlite3.Connection) -> None:
    """A close reduces risk: D34 still auto-approves it with no track record."""
    conn.execute("UPDATE proposals SET kind = 'close' WHERE day IS NOT NULL")
    conn.commit()
    rep, _ = publish(conn, settings())
    assert rep.auto_approved == [_phash(conn)] and gated_rows(conn) == []


def test_window_is_the_latest_n_closed_trades(conn: sqlite3.Connection) -> None:
    """Old losers roll out of the window; the gate reads the latest N only."""
    for i in range(10):
        add_closed_trade(conn, i, pnl_per_contract=-500.0)
    for i in range(10, 40):
        add_closed_trade(conn, i, pnl_per_contract=20.0)
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.ok and r.closed_trades == 40 and r.window_trades == 30
    assert r.realised_pnl == pytest.approx(600.0)


def test_trades_closed_after_now_do_not_count(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0, closed=NOW + _dt.timedelta(hours=1))
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.failing == ["min_closed_trades"] and r.closed_trades == 0


def test_slippage_unknown_fails_closed(conn: sqlite3.Connection) -> None:
    history(conn, 30, pnl_per_contract=50.0)
    conn.execute("DELETE FROM executions")  # no fills to measure slippage on
    conn.commit()
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.failing == ["slippage_unknown"] and not r.ok
    assert "no fill with a modelled spread" in r.summary()


def test_several_failures_are_all_reported(conn: sqlite3.Connection) -> None:
    history(conn, 5, pnl_per_contract=-10.0, slip=Decimal("0.50"))
    r = auto_approve_readiness(conn, now=NOW, min_closed_trades=30, slippage_tolerance=1.5)
    assert r.failing == ["min_closed_trades", "negative_realised_ev", "slippage_over_tolerance"]
    text = r.summary()
    assert "5 closed trades < 30" in text and "< $0" in text and "x 1.5" in text


def test_readiness_is_computed_once_per_sweep(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    import arc.approvals.service as svc_mod

    real = svc_mod.auto_approve_readiness

    def counting(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(svc_mod, "auto_approve_readiness", counting)
    svc = ApprovalService(conn, settings(), LogCardPoster())
    svc.publish_pending(NOW)
    assert calls == [1]


# ---------------------------------------------------------------------------
# Config: registry keys, and a D26 change reaches the gate
# ---------------------------------------------------------------------------


def test_registry_keys() -> None:
    gate = lookup("auto_approve.scorecard_gate")
    assert gate.field == "auto_approve_scorecard_gate" and gate.risk.value == "false"
    n = lookup("auto_approve.min_closed_trades")
    assert n.field == "auto_approve_min_closed_trades" and n.hard_ceiling == 10
    tol = lookup("auto_approve.slippage_tolerance")
    assert tol.field == "auto_approve_slippage_tolerance" and tol.hard_ceiling == 3.0
    assert lookup("scorecard_gate") is gate


def test_config_store_change_reaches_the_gate(conn: sqlite3.Connection) -> None:
    """min_closed_trades lowered through the control service: 10 trades now suffice."""
    history(conn, 10, pnl_per_contract=50.0)
    base = settings()
    svc = ControlService(conn, base=base)
    res = svc.set("auto_approve.min_closed_trades", "10", actor=LOCAL_ACTOR, source="cli")
    if res.outcome == "pending" and res.pending is not None:
        res = svc.confirm(res.pending.code, actor=LOCAL_ACTOR, source="cli")
    assert res.outcome == "applied"
    eff = effective_settings(conn, base=base)
    assert eff.auto_approve_min_closed_trades == 10
    rep, _ = publish(conn, eff)
    assert rep.auto_approved == [_phash(conn)]


def test_turning_the_gate_off_needs_a_confirm(conn: sqlite3.Connection) -> None:
    svc = ControlService(conn, base=settings())
    res = svc.set("auto_approve.scorecard_gate", "off", actor=LOCAL_ACTOR, source="cli")
    assert res.outcome == "pending"  # riskier direction


# ---------------------------------------------------------------------------
# Realised slippage per structure kind (feeds the backtest cost model)
# ---------------------------------------------------------------------------


def test_slippage_by_kind_as_fraction_of_spread(conn: sqlite3.Connection) -> None:
    from arc.journal.scorecard import build_scorecard, render_markdown

    history(conn, 4, pnl_per_contract=50.0, slip=Decimal("0.0645"), contracts=2)
    sc = build_scorecard(conn, start=NOW - _dt.timedelta(days=90), end=NOW, now=NOW)
    kinds = sc.slippage.by_kind
    assert set(kinds) == {"iron_condor"}
    k = kinds["iron_condor"]
    assert k.fills == 4 and k.spread_usd == pytest.approx(4 * 2 * 12.90)
    assert k.frac == pytest.approx(0.5)  # $6.45 of a $12.90 spread
    assert slippage_by_kind(sc.slippage.rows) == kinds
    md = render_markdown(sc)
    assert "Entry slippage by structure kind" in md and "| iron_condor | 4 |" in md
