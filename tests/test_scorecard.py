"""E7.3: weekly paper scorecard — aggregation, D19 shadow, Markdown, Slack card, routine, CLI."""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.journal.attribution import expiry_value
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.scorecard import (
    OrderBudgetLimits,
    Scorecard,
    build_scorecard,
    calibration_points,
    render_markdown,
    week_window,
)
from arc.journal.store import JournalStore
from arc.models import Structure
from arc.pipeline.runner import fixture_run
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, resolve_handler
from arc.routines.scorecard import budget_limits, report_path, scorecard
from arc.slack.blocks import MAX_BLOCKS
from arc.slack.scorecard import scorecard_card
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.repos import PnlSnapshotRepo
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

FRI = _dt.datetime(2026, 9, 25, 16, 45, tzinfo=ET)  # the fixture pipeline's day
START, END = week_window(FRI)
AFTER_EXPIRY = _dt.datetime(2026, 11, 2, 12, tzinfo=ET)  # the fixture legs expire 2026-10-30


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


def _open_row(conn: sqlite3.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM proposals WHERE kind = 'open'").fetchone())


def _at(day: int, hour: int = 11) -> _dt.datetime:
    return _dt.datetime(2026, 9, day, hour, tzinfo=ET)


def _order(conn: sqlite3.Connection, phash: str, at: _dt.datetime) -> None:
    conn.execute(
        """INSERT INTO orders (id, proposal_hash, client_order_id, state, created_at, updated_at)
           VALUES (?, ?, ?, 'filled', ?, ?)""",
        (uuid.uuid4().hex, phash, uuid.uuid4().hex, at.isoformat(), at.isoformat()),
    )


def _close_proposal(conn: sqlite3.Connection, src: dict[str, Any], *, swap_id: str | None) -> str:
    phash = "close-" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO proposals (candidate_id, proposal_hash, structure_json, thesis, quant_json,
                                 sizing_json, expires_at, created_at, day, ticker, kind, swap_id)
           VALUES (?, ?, ?, 'close', '{}', '{}', ?, ?, NULL, ?, 'close', ?)""",
        (
            src["candidate_id"],
            phash,
            src["structure_json"],
            _at(25, 15).isoformat(),
            _at(25, 10).isoformat(),
            src["ticker"],
            swap_id,
        ),
    )
    return phash


def open_filled(conn: sqlite3.Connection, *, contracts: int = 2) -> tuple[str, Structure]:
    """The fixture proposal approved, filled 1c worse than mid and opened as a structure."""
    p = _open_row(conn)
    st = Structure.model_validate_json(p["structure_json"])
    fill = st.net_debit_credit + Decimal("0.01")
    ex = ExecutionRepo(conn)
    ex.start(
        proposal_hash=p["proposal_hash"],
        kind="open",
        token_version="v1",
        band_lo=fill,
        band_hi=fill,
        max_steps=3,
        contracts=contracts,
        now=_at(23, 10),
    )
    ex.finish(
        p["proposal_hash"], status="filled", now=_at(23, 10), filled_qty=contracts, fill_price=fill
    )
    _order(conn, p["proposal_hash"], _at(23, 10))
    _order(conn, p["proposal_hash"], _at(23, 10))
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               channel, expires_at, created_at, decided_at, decided_by)
           VALUES (?, ?, '2026-09-23', '{}', 'approved', 'log', ?, ?, ?, 'U0OWNER')""",
        (p["proposal_hash"], p["ticker"], _at(23, 15).isoformat(), _at(23, 9).isoformat(),
         _at(23, 9).isoformat()),
    )  # fmt: skip
    sid = OpenStructureRepo(conn).open(
        ticker=p["ticker"],
        open_proposal_hash=p["proposal_hash"],
        candidate_id=p["candidate_id"],
        structure_json=p["structure_json"],
        contracts=contracts,
        entry_net=fill,
        now=_at(23, 10),
    )
    return sid, st


def close_early(
    conn: sqlite3.Connection,
    sid: str,
    close_net: Decimal,
    *,
    qty: int,
    at: _dt.datetime,
    swap_id: str | None = None,
    reason: str = "take_profit",
) -> Decimal:
    """What ``arc.execution.ladder`` does on a filled close: reduce + ``exit:closed`` row."""
    repo = OpenStructureRepo(conn)
    row = repo.get(sid)
    assert row is not None
    src = dict(conn.execute("SELECT * FROM proposals WHERE proposal_hash = ?",
                            (row["open_proposal_hash"],)).fetchone())  # fmt: skip
    cphash = _close_proposal(conn, src, swap_id=swap_id)
    repo.set_exit(sid, proposal_hash=cphash, reason=reason, day=at.date().isoformat())
    _order(conn, cphash, at)
    repo.reduce(sid, closed_qty=qty, close_net=close_net, now=at)
    pnl = -(Decimal(row["entry_net"]) + close_net) * 100 * qty
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.INVESTOR,
            stage=Stage.EXIT,
            subject=row["ticker"],
            choice=Choice.FILLED,
            reason_code=ReasonCode.EXIT_CLOSED,
            proposal_hash=cphash,
            payload={"structure_id": sid, "realized_pnl": str(pnl)},
            at=at,
        )
    return pnl


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def test_week_window_is_monday_to_monday_et() -> None:
    start, end = week_window(_dt.datetime(2026, 9, 27, 23, 30, tzinfo=ET))  # Sunday night
    assert start == _dt.datetime(2026, 9, 21, tzinfo=ET) and end - start == _dt.timedelta(days=7)
    # 02:00 UTC Monday is still Sunday in New York
    assert week_window(_dt.datetime(2026, 9, 28, 2, tzinfo=_dt.UTC))[0] == start


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_empty_week_is_all_zero(conn: sqlite3.Connection) -> None:
    start, end = week_window(_dt.datetime(2026, 10, 7, tzinfo=ET))
    sc = build_scorecard(conn, start=start, end=end, now=end)
    assert sc.funnel.proposals == 0 and sc.pnl.closed == 0 and not sc.budget_days
    assert sc.pnl.win_rate is None and sc.pnl.equity_change is None
    md = render_markdown(sc)
    assert "No broker orders." in md and "No fills." in md and "n/a: no closed trades" in md


def test_funnel_gate_and_approvals(conn: sqlite3.Connection) -> None:
    p = _open_row(conn)
    conn.execute(
        """INSERT INTO gate_decisions (proposal_hash, passed, violations_json, decided_at)
           VALUES (?, 0, ?, ?)""",
        (p["proposal_hash"], json.dumps(["max_loss: 400 > 300", "max_loss: again",
                                          "min_dte: 3 < 7"]), _at(24).isoformat()),
    )  # fmt: skip
    conn.execute(
        """INSERT INTO gate_decisions (proposal_hash, passed, violations_json, decided_at)
           VALUES (?, 0, '["late: outside window"]', ?)""",
        (p["proposal_hash"], _at(28).isoformat()),  # next week: not counted
    )
    conn.execute(
        """INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status,
               channel, expires_at, created_at, decided_at, decided_by)
           VALUES (?, 'SPY', '2026-09-25', '{}', 'approved', 'log', ?, ?, ?, 'arc:auto-approve')""",
        (p["proposal_hash"], _at(25, 15).isoformat(), _at(25).isoformat(), _at(25).isoformat()),
    )  # fmt: skip
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    f = sc.funnel
    assert (f.proposals, f.proposals_open, f.proposals_close) == (1, 1, 0)
    assert (f.gate_pass, f.gate_fail) == (1, 1)
    assert sc.gate_violations == {"max_loss": 2, "min_dte": 1}
    assert f.approvals.auto_approved == 1 and f.approvals.click_approved == 0
    assert "| `max_loss` | 2 |" in render_markdown(sc)


def test_early_close_realised_shadow_and_model(conn: sqlite3.Connection) -> None:
    sid, st = open_filled(conn, contracts=2)
    entry = st.net_debit_credit + Decimal("0.01")
    p1 = close_early(conn, sid, Decimal("0.80"), qty=1, at=_at(24, 14))  # partial
    p2 = close_early(conn, sid, Decimal("0.70"), qty=1, at=_at(25, 14))  # rest
    settle = Decimal("770")
    sc = build_scorecard(
        conn, start=START, end=END, now=AFTER_EXPIRY, settle_price=lambda root, exp: settle
    )
    assert sc.pnl.closed == 1 and len(sc.closed) == 1
    c = sc.closed[0]
    assert c.early and c.exit_reason == "take_profit" and c.contracts == 2
    assert c.realised_pnl == pytest.approx(float(p1 + p2))
    hold = (expiry_value(st.legs, settle) - entry) * 100 * 2
    assert c.shadow_hold_pnl == pytest.approx(float(hold))
    assert c.early_exit_edge == pytest.approx(float(p1 + p2 - hold))
    # D23: the E2.4 model frozen with the proposal, scaled by contracts
    assert c.managed_net_ev is not None and c.static_net_ev is not None
    m = sc.model_vs_realised
    assert m.n == 1 and m.realised == pytest.approx(c.realised_pnl) and m.n_shadow == 1
    assert m.managed_net_ev == pytest.approx(c.managed_net_ev)
    assert m.win_rate == (1.0 if c.realised_pnl > 0 else 0.0)
    # order budget: 2 opening attempts on Wed, 1 close each on Thu / Fri
    assert [(d.day.day, d.orders, d.opens, d.closes) for d in sc.budget_days] == [
        (23, 2, 2, 0), (24, 1, 0, 1), (25, 1, 0, 1)]  # fmt: skip
    # slippage: filled 1c worse than mid on 2 contracts = $2
    assert sc.slippage.fills == 1 and sc.slippage.realised_usd == pytest.approx(2.0)
    row = sc.slippage.rows[0]
    assert row.expected_usd is not None and row.expected_usd > 0 and row.cost_bps == 203.9
    md = render_markdown(sc)
    assert "Early exits vs hold to expiry (D19)" in md and "take_profit" in md
    assert "1 of 1 known" in md


def test_shadow_pending_before_expiry(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("0.80"), qty=1, at=_at(25, 14))
    called: list[Any] = []
    sc = build_scorecard(
        conn, start=START, end=END, now=FRI, settle_price=lambda *a: called.append(a)
    )
    assert sc.closed[0].shadow_hold_pnl is None and sc.closed[0].early_exit_edge is None
    assert not called  # never asks for a settle the market hasn't printed
    assert "pending" in render_markdown(sc)


def test_expired_position_is_not_early_and_shadow_is_actual(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    OpenStructureRepo(conn).reduce(sid, closed_qty=1, close_net=Decimal(0), now=_at(25, 17))
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.AUDITOR, stage=Stage.RECONCILE, subject="SPY",
            choice=Choice.NOTED, reason_code=ReasonCode.RECONCILE_EXPIRED,
            payload={"structure_id": sid, "realized_pnl": "164.55", "settle": "770"},
            at=_at(25, 17),
        )  # fmt: skip
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    c = sc.closed[0]
    assert not c.early and c.exit_reason == "expiry" and c.realised_pnl == pytest.approx(164.55)
    assert c.shadow_hold_pnl == c.realised_pnl and not sc.early_closed
    assert sc.pnl.wins == 1 and sc.pnl.win_rate == 1.0


def test_close_without_journal_row_falls_back_to_close_net(conn: sqlite3.Connection) -> None:
    sid, st = open_filled(conn, contracts=1)
    OpenStructureRepo(conn).reduce(sid, closed_qty=1, close_net=Decimal("0.5"), now=_at(24))
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    entry = st.net_debit_credit + Decimal("0.01")
    assert sc.closed[0].realised_pnl == pytest.approx(float(-(entry + Decimal("0.5")) * 100))
    assert sc.closed[0].early


def test_swap_net_vs_hold(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    p = _open_row(conn)
    conn.execute(
        """INSERT INTO swaps (id, day, status, close_structure_id, close_ticker, open_ticker,
                              source_ref, suggestion_json, created_at, updated_at,
                              open_proposal_hash)
           VALUES ('sw-1', '2026-09-25', 'open_proposed', ?, 'SPY', 'QQQ', 'x', '{}', ?, ?, ?)""",
        (sid, _at(25).isoformat(), _at(25).isoformat(), None),
    )
    pnl = close_early(conn, sid, Decimal("0.80"), qty=1, at=_at(25, 14), swap_id="sw-1",
                      reason="reallocate")  # fmt: skip
    # the swap's open leg: a QQQ proposal, filled, marked by position_review
    qphash = _close_proposal(conn, p, swap_id="sw-1")
    conn.execute("UPDATE proposals SET kind = 'open', ticker = 'QQQ' WHERE proposal_hash = ?",
                 (qphash,))  # fmt: skip
    sid2 = OpenStructureRepo(conn).open(
        ticker="QQQ", open_proposal_hash=qphash, candidate_id=p["candidate_id"],
        structure_json=p["structure_json"], contracts=1, entry_net=Decimal("-1"), now=_at(25, 15),
    )  # fmt: skip
    conn.execute("UPDATE swaps SET open_proposal_hash = ? WHERE id = 'sw-1'", (qphash,))
    conn.execute(
        """INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by,
                                        created_at, valid_from)
           VALUES ('ce-r', 'position_review', ?, '{"pnl_total": 12.5}', 1, 'test', ?, ?)""",
        (sid2, _at(25, 16).isoformat(), _at(25, 16).isoformat()),
    )
    sc = build_scorecard(
        conn, start=START, end=END, now=AFTER_EXPIRY, settle_price=lambda *a: Decimal("770")
    )
    assert len(sc.swaps) == 1
    s = sc.swaps[0]
    assert s.close_realised == pytest.approx(float(pnl)) and s.closed_hold_shadow is not None
    assert s.open_pnl == 12.5 and s.open_marked
    assert s.net == pytest.approx(float(pnl) + 12.5) and s.vs_hold is not None
    assert s.vs_hold == pytest.approx(s.net - s.closed_hold_shadow)
    assert sc.closed[0].swap_id == "sw-1"
    assert "SPY → QQQ" in render_markdown(sc) and "(mark)" in render_markdown(sc)


def test_equity_change_and_open_marks(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    repo = PnlSnapshotRepo(conn)
    for day, eq in (("2026-09-18", "100000"), ("2026-09-24", "100300"), ("2026-09-25", "100450"),
                    ("2026-09-28", "90000")):  # fmt: skip
        repo.insert(realized="0", unrealized="0", total="0",
                    details_json=json.dumps({"day": day, "equity": eq}))  # fmt: skip
    conn.execute(
        """INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by,
                                        created_at, valid_from)
           VALUES ('ce-1', 'position_review', ?, '{"pnl_total": -20.0}', 1, 't', ?, ?)""",
        (sid, _at(25).isoformat(), _at(25).isoformat()),
    )
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    assert sc.pnl.equity_start == 100000 and sc.pnl.equity_end == 100450
    assert sc.pnl.equity_change == 450 and sc.pnl.open_positions == 1
    assert sc.pnl.open_marked_pnl == -20.0


@pytest.mark.parametrize(
    ("n", "tier"),
    [(0, "normal"), (99, "normal"), (100, "restrictive"), (175, "opens_exhausted"),
     (200, "exhausted")],
)  # fmt: skip
def test_budget_tiers(n: int, tier: str) -> None:
    from arc.journal.scorecard import _tier

    assert _tier(n, OrderBudgetLimits()) == tier
    assert OrderBudgetLimits().open_stop == 175


def test_calibration_uses_shared_points(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("0.10"), qty=1, at=_at(24))  # cheap buy-back: a win
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    phash = _open_row(conn)["proposal_hash"]
    pts = calibration_points(conn, [(phash, True)])
    assert {p for p, _, _ in pts} == {"quant_pop", "research", "quant"}
    assert sc.calibration_trades == 1
    assert {r.persona for r in sc.calibration} == {"quant_pop", "research", "quant"}
    assert all(r.hit_rate == 1.0 for r in sc.calibration)


def test_journal_gaps_still_calibrates(conn: sqlite3.Connection) -> None:
    """The E7.4 report shares the extracted helper; it must behave as before."""
    from arc.journal.report import gaps

    rep = gaps(conn)
    assert rep.calibration == []  # no realised outcomes in the fixture


# ---------------------------------------------------------------------------
# Slack card
# ---------------------------------------------------------------------------


def test_card_layout(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("0.80"), qty=1, at=_at(25, 14))
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    view = scorecard_card(sc, report_path="docs/RESEARCH/weekly/2026-09-21.md", run_id="run-1")
    assert view.text.startswith("⚙️ [Ops] Scorecard: Sep 21 – Sep 27, 2026")
    assert len(view.blocks) <= MAX_BLOCKS
    text = json.dumps(view.blocks)
    assert "Early exits vs hold to expiry (D19)" in text and "2026-09-21.md" in text
    assert "Order budget" in text and "run-1" in text


def test_card_caps_long_lists() -> None:
    sc = Scorecard.model_validate(
        {
            "start": START, "end": END, "generated_at": FRI,
            "funnel": {}, "order_budget": {}, "pnl": {}, "model_vs_realised": {}, "slippage": {},
            "gate_violations": {f"rule_{i}": 1 for i in range(30)},
        }
    )  # fmt: skip
    text = json.dumps(scorecard_card(sc).blocks)
    assert "rule_7" in text and "rule_8" not in text and "22 more" in text


# ---------------------------------------------------------------------------
# Routine + CLI
# ---------------------------------------------------------------------------


def _ctx(conn: sqlite3.Connection, **opts: Any) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"personas": {"scorecard": {"schedule": ["16:45"], "days": ["fri"], "llm": False,
                                    "halt_exempt": True, "ttl": "6h", "notify": "card",
                                    "writes": [], **opts}}}
    )  # fmt: skip
    kind, step = routines.step("scorecard")
    return JobContext(
        job="scorecard", kind=kind, spec=step, run_id="run-s", chain_run_id=None,
        scheduled_for=FRI, now=FRI, conn=conn, snapshot=ContextStore(conn).snapshot(FRI),
        routines=routines, settings_factory=lambda: ArcSettings(_env_file=None),  # type: ignore[call-arg]
    )  # fmt: skip


def test_routine_writes_report_and_card(conn: sqlite3.Connection, tmp_path: Path) -> None:
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("0.80"), qty=1, at=_at(25, 14))
    ctx = _ctx(conn, report_dir=str(tmp_path / "weekly"))
    res = scorecard(ctx)
    out = tmp_path / "weekly" / "2026-09-21.md"
    assert out.read_text().startswith("# Paper scorecard: week of Sep 21 – Sep 27, 2026")
    assert res.card is not None and "⚙️ [Ops] Scorecard" in res.card.text
    assert res.metrics["closed"] == 1 and res.metrics["report"] == str(out)
    assert [i.name for i in ctx.external_inputs] == ["scorecard"]


def test_routine_can_skip_the_report(conn: sqlite3.Connection) -> None:
    res = scorecard(_ctx(conn, write_report=False))
    assert res.metrics["report"] is None and res.card is not None


def test_routine_registered_and_configured() -> None:
    spec = load_routines().step("scorecard")[1]
    assert resolve_handler("scorecard", spec).__name__ == "scorecard_step"
    assert spec.notify == "card" and spec.options["report_dir"] == "docs/RESEARCH/weekly"


def test_budget_limits_default_and_report_path() -> None:
    assert budget_limits(ArcSettings(_env_file=None)) == OrderBudgetLimits()  # type: ignore[call-arg]
    assert report_path("docs/RESEARCH/weekly", _dt.date(2026, 9, 21)).name == "2026-09-21.md"
    assert report_path("docs/RESEARCH/weekly", _dt.date(2026, 9, 21)).is_absolute()


def test_cli_scorecard(conn: sqlite3.Connection, tmp_path: Path, capsys: Any) -> None:
    from arc.cli import main

    db = tmp_path / "arc.db"
    disk = sqlite3.connect(db)
    conn.commit()
    conn.backup(disk)
    disk.close()
    assert main(["journal", "scorecard", "--db", str(db), "--week", "2026-09-25",
                 "--write", str(tmp_path / "w")]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "# Paper scorecard: week of Sep 21" in out
    assert (tmp_path / "w" / "2026-09-21.md").exists()
    assert main(["journal", "scorecard", "--db", str(db), "--week", "2026-09-25", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["funnel"]["proposals"] == 1


def test_same_day_exits_are_their_own_bucket(conn: sqlite3.Connection) -> None:
    """E6.4a: an early exit closed the day it opened (days_held 0) is reported apart."""
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("-0.80"), qty=1, at=_at(23, 14), reason="remaining_ev_floor")
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    (c,) = sc.closed
    assert c.days_held == 0 and c.early and sc.same_day_closed == [c]
    md = render_markdown(sc)
    assert "### Same-day exits (days held 0)" in md and "remaining_ev_floor" in md
    assert "1 of 1 early exit(s) closed the day they opened" in md
    card = json.dumps(scorecard_card(sc).blocks)
    assert "Same-day exits (days held 0)" in card


def test_next_day_exit_is_not_same_day(conn: sqlite3.Connection) -> None:
    sid, _ = open_filled(conn, contracts=1)
    close_early(conn, sid, Decimal("-0.80"), qty=1, at=_at(24, 14))
    sc = build_scorecard(conn, start=START, end=END, now=FRI)
    assert sc.closed[0].days_held == 1 and sc.same_day_closed == []
    assert "### Same-day exits (days held 0)\n\nNone." in render_markdown(sc)
