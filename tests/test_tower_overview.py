"""E8.7a control tower v2 Overview: loaders, /api/overview, /api/positions, fixture DB."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from arc.context.ttl import to_db
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import PnlSnapshotRepo
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_overview import (
    RANGES,
    OverviewResponse,
    PositionsResponse,
    equity_intraday,
    load_overview,
    load_positions,
    range_start,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 28, 15, 40, tzinfo=ET)  # Monday
STALE = dt.timedelta(minutes=15)


def _fixture_module():
    spec = importlib.util.spec_from_file_location(
        "tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("tower_fixture_db", mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _fixture_module()


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("fx") / "arc.db", NOW)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


def _overview(conn: sqlite3.Connection, rng: str = "1D", now: dt.datetime = NOW):
    return load_overview(conn, now=now, rng=rng, stale_after=STALE)  # type: ignore[arg-type]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# fixture DB (scripts/tower_fixture_db.py; E8.7b-d reuse it)
# ---------------------------------------------------------------------------


def test_fixture_has_everything_the_card_lists(conn: sqlite3.Connection) -> None:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert one("SELECT COUNT(*) FROM open_structures WHERE status = 'open'") >= 3
    assert one("SELECT COUNT(*) FROM open_structures WHERE status = 'closed'") >= 1
    days = {json.loads(r[0])["day"] for r in conn.execute("SELECT details_json FROM pnl_snapshots")}
    assert len(days) == 10
    assert one("SELECT COUNT(*) FROM heartbeats WHERE component = 'monitor'") == 30
    assert one("SELECT COUNT(*) FROM halts WHERE cleared_at IS NULL") == 1
    assert one("SELECT COUNT(*) FROM gate_decisions WHERE passed = 0") >= 1
    approvals = {r[0] for r in conn.execute("SELECT status FROM approval_requests")}
    assert {"pending", "approved", "rejected", "expired", "not_actionable"} <= approvals
    execs = {r[0] for r in conn.execute("SELECT status FROM executions")}
    assert {"working", "filled", "cancelled"} <= execs
    assert one("SELECT COUNT(*) FROM fills") >= 3
    no_gate = one(
        "SELECT COUNT(*) FROM proposals p WHERE NOT EXISTS "
        "(SELECT 1 FROM gate_decisions g WHERE g.proposal_hash = p.proposal_hash)"
    )
    assert no_gate >= 1  # "proposed": no gate decision yet
    assert one("SELECT COUNT(*) FROM proposals WHERE kind = 'close'") >= 1


def test_fixture_refuses_to_overwrite(fx_db: Path) -> None:
    before = _sha(fx_db)
    with pytest.raises(FileExistsError):
        fixture.build(fx_db, NOW)
    assert fixture.main([str(fx_db)]) == 2
    assert _sha(fx_db) == before


def test_fixture_cli_default_now(tmp_path: Path) -> None:
    out = tmp_path / "cli.db"
    assert fixture.main([str(out), "--now", "2026-09-28T15:40:00"]) == 0
    c = connect_ro(out)
    try:
        at = c.execute("SELECT MAX(at) FROM heartbeats WHERE component = 'monitor'").fetchone()[0]
    finally:
        c.close()
    assert at == to_db(NOW - dt.timedelta(minutes=2))


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------


def test_equity_intraday_is_one_et_day(conn: sqlite3.Connection) -> None:
    marks = equity_intraday(conn, NOW.date())
    assert len(marks) == 30
    assert [m.at for m in marks] == sorted(m.at for m in marks)
    assert marks[-1].at == NOW - dt.timedelta(minutes=2)
    assert all(m.prev_close is not None for m in marks) and marks[-1].legs
    assert {m.prev_close_source for m in marks} == {"arc_close"}
    assert equity_intraday(conn, NOW.date() - dt.timedelta(days=1)) == []


def test_range_start() -> None:
    today = dt.date(2026, 9, 28)
    assert range_start("1D", today) == today
    assert range_start("1W", today) == dt.date(2026, 9, 21)
    assert range_start("1M", today) == dt.date(2026, 8, 29)
    assert range_start("3M", today) == dt.date(2026, 6, 29)
    assert range_start("YTD", today) == dt.date(2026, 1, 1)
    assert range_start("ALL", today) is None


def test_equity_1d_uses_intraday_marks_against_prev_close(conn: sqlite3.Connection) -> None:
    e = _overview(conn).equity
    assert e.source == "intraday" and e.series_source == "intraday" and len(e.series) == 30
    assert e.value == e.series[-1].v and e.value_at == NOW - dt.timedelta(minutes=2)
    assert e.start_label == "prev close"
    last_daily = conn.execute(
        "SELECT json_extract(details_json, '$.equity') FROM pnl_snapshots "
        "ORDER BY snapshot_at DESC LIMIT 1"
    ).fetchone()[0]
    assert e.start_value == D(last_daily)
    assert e.change == e.value - e.start_value
    assert e.change_pct == pytest.approx(float(e.change / e.start_value))


@pytest.mark.parametrize("rng", ["1W", "1M", "3M", "YTD", "ALL"])
def test_equity_daily_ranges(conn: sqlite3.Connection, rng: str) -> None:
    e = _overview(conn, rng).equity
    assert e.series_source == "daily" and e.range == rng
    # daily closes then today's live mark as the last point
    assert e.series[-1].t == NOW - dt.timedelta(minutes=2)
    assert e.series[0].v == e.start_value
    start = range_start(rng, NOW.date())  # type: ignore[arg-type]
    inside = [p for p in e.series[1:-1] if start is None or p.t.date() >= start]
    assert len(inside) == len(e.series) - 2
    assert e.change == e.value - e.start_value  # type: ignore[operator]


def test_equity_1w_starts_at_last_close_before_the_window(conn: sqlite3.Connection) -> None:
    e = _overview(conn, "1W").equity
    assert e.start_at is not None and e.start_at.date() < range_start("1W", NOW.date())  # type: ignore[operator]
    assert e.start_label == f"{e.start_at:%m-%d}"


def test_equity_falls_back_to_reconciled_without_marks(tmp_path: Path) -> None:
    path = tmp_path / "rec.db"
    c = connect(path)
    migrate(c)
    for day, eq in (("2026-09-24", "100000"), ("2026-09-25", "100250")):
        PnlSnapshotRepo(c).insert(
            realized="0", unrealized="0", total="0",
            details_json=json.dumps({"day": day, "equity": eq, "last_equity": "100000",
                                     "day_pnl": "250", "clean": False}),
            snapshot_at=f"{day}T20:35:00.000000Z",
        )  # fmt: skip
    c.close()
    ro = connect_ro(path)
    try:
        o = _overview(ro)
        e = o.equity
        assert e.source == "reconciled" and e.value == D("100250")
        assert e.series_source == "daily" and [p.v for p in e.series] == [D("100000"), D("100250")]
        assert e.start_label == "prev close" and e.change == D("250")
        d = o.day_pnl
        assert d.source == "reconciled" and d.day_pnl == D("250") and d.prev_equity == D("100000")
        assert d.day_pct == pytest.approx(0.0025)
        assert o.marks_stale and o.marks_at is None and o.positions == []
        # reconciles 3+ days ago are outside the default 24 h window; a week shows them
        assert not any(a.kind == "reconcile" for a in o.activity)
        week = load_overview(ro, now=NOW, stale_after=STALE, activity_hours=168)
        assert any(a.kind == "reconcile" and a.tone == "neg" for a in week.activity)
    finally:
        ro.close()


def test_day_pnl_intraday(conn: sqlite3.Connection) -> None:
    o = _overview(conn)
    d = o.day_pnl
    assert d.source == "intraday" and d.as_of == o.marks_at
    assert d.day_pnl == o.equity.value - d.prev_equity  # type: ignore[operator]
    assert d.realized == D("150") and d.unrealized is not None
    # unrealized = the open structures' legs
    assert d.unrealized == sum((p.unrealized_pl or D(0) for p in o.positions), start=D(0))
    assert d.performance is not None and d.performance_day == dt.date(2026, 9, 25)
    assert d.performance.mtd_pnl is not None and d.performance.ytd_pnl is not None


def test_positions_rows(conn: sqlite3.Connection) -> None:
    rows = {p.ticker: p for p in _overview(conn).positions}
    assert set(rows) == {"SPY", "QQQ", "NVDA"}
    spy, qqq, nvda = rows["SPY"], rows["QQQ"], rows["NVDA"]
    assert spy.kind == "vertical_debit" and len(spy.legs) == 2 and spy.contracts == 3
    assert spy.held is True and spy.held_at is not None and not spy.exit_pending
    # P&L = Σ legs' broker P&L; % of the debit paid
    assert spy.unrealized_pl is not None and spy.mark_net is not None
    assert spy.unrealized_pl == (spy.mark_net - spy.entry_net) * 100 * spy.contracts
    assert spy.unrealized_pct == pytest.approx(float(spy.unrealized_pl / (spy.entry_net * 300)))
    assert spy.dte == 31 and spy.max_loss == D("4.15") * 3 * 100 / 100 * 100
    assert qqq.exit_pending and qqq.exit_reason == "profit_target" and qqq.exit_proposal_hash
    assert qqq.unrealized_pl is not None and qqq.unrealized_pl < 0
    assert nvda.held is False  # reconcile says not held: the UI's red NO
    assert nvda.kind == "long_call" and nvda.day_change is not None
    assert all(p.mark_at == NOW - dt.timedelta(minutes=2) for p in rows.values())


def test_shared_leg_leaves_pnl_blank(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    fixture.build(path, NOW)
    c = connect(path)
    spy = c.execute("SELECT * FROM open_structures WHERE ticker = 'SPY'").fetchone()
    c.execute(
        """INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,
               structure_json, contracts, entry_net, opened_at)
           SELECT 'os-dup', ticker, ?, candidate_id, structure_json, 1, entry_net, opened_at
           FROM open_structures WHERE id = ?""",
        (fixture.phash("p-googl"), spy["id"]),
    )
    c.commit()
    c.close()
    ro = connect_ro(path)
    try:
        spys = [p for p in _overview(ro).positions if p.ticker == "SPY"]
    finally:
        ro.close()
    assert len(spys) == 2 and all(p.unrealized_pl is None and p.mark_net is None for p in spys)


def test_load_positions_status(conn: sqlite3.Connection) -> None:
    open_ = load_positions(conn, now=NOW, status="open", stale_after=STALE)
    assert {p.ticker for p in open_.items} == {"SPY", "QQQ", "NVDA"}
    closed = load_positions(conn, now=NOW, status="closed", stale_after=STALE)
    (amd,) = closed.items
    assert amd.ticker == "AMD" and amd.status == "closed" and amd.closed_at is not None
    assert amd.close_net == D("-5.10") and amd.realized_pl == D("300.00")
    assert amd.dte is None and amd.held is None and amd.unrealized_pl is None
    assert len(load_positions(conn, now=NOW, status="all", stale_after=STALE).items) == 4
    assert open_.stale_after_s == 900 and open_.marks_at == NOW - dt.timedelta(minutes=2)


def test_greeks_vs_caps(conn: sqlite3.Connection) -> None:
    o = load_overview(
        conn,
        now=NOW,
        dollar_delta_cap_pct=0.50,
        vega_cap_pct=0.010,
        max_alloc_pct=0.05,
        stale_after=STALE,
    )
    g = o.greeks
    eq = float(o.equity.value)  # type: ignore[arg-type]
    assert g.greeks.dollar_delta_cap == pytest.approx(0.50 * eq)
    assert g.greeks.vega_cap_usd == pytest.approx(0.010 * eq)
    assert g.per_underlying_cap == pytest.approx(D("0.05") * D(str(eq)))
    assert set(g.max_loss_by_underlying) == {"SPY", "QQQ", "NVDA"}
    assert list(g.max_loss_by_underlying.values()) == sorted(
        g.max_loss_by_underlying.values(), reverse=True
    )
    assert g.greeks.stale_after_s == 900


def test_todays_proposals(conn: sqlite3.Connection) -> None:
    o = _overview(conn)
    by = {(p.ticker, p.kind): p for p in o.proposals}
    assert ("DIA", "open") not in by  # 4 days old: outside the 24 h window
    assert o.proposals_since == NOW - dt.timedelta(hours=24)
    assert all(p.created_at is not None and p.created_at >= o.proposals_since for p in o.proposals)
    aapl = by[("AAPL", "open")]
    assert aapl.gate_passed is False and aapl.violations[0].startswith("per_underlying_limit:")
    assert aapl.net_ev == pytest.approx(-3.2 * 4)  # managed net EV x contracts
    assert by[("GOOGL", "open")].gate_passed is None  # proposed only
    assert by[("MSFT", "open")].approval == "pending"
    assert by[("META", "open")].execution == "working"
    assert by[("IWM", "open")].execution == "cancelled"
    assert by[("TSLA", "open")].approval == "rejected"
    assert by[("AMZN", "open")].approval == "expired"
    assert by[("QQQ", "close")].net_ev is None
    times = [p.created_at for p in o.proposals]
    assert times == sorted(times, reverse=True)  # type: ignore[type-var]


def test_movers(conn: sqlite3.Connection) -> None:
    movers = {m.ticker: m for m in _overview(conn).movers}
    assert set(movers) == {"SPY", "QQQ", "NVDA"}
    assert movers["QQQ"].change_today is not None and movers["QQQ"].change_today < 0
    assert movers["SPY"].change_today is not None and movers["SPY"].change_today > 0
    # every structure was open for all 30 marks (NVDA opened 3 h ago, marks span 2.4 h)
    assert all(len(m.spark) == 30 for m in movers.values())
    assert movers["NVDA"].spark[0] == 0.0  # no move at the first mark


def test_recent_activity(conn: sqlite3.Connection) -> None:
    o = _overview(conn)
    act = o.activity
    assert o.activity_hours == 24 and o.activity_since == NOW - dt.timedelta(hours=24)
    assert all(a.at >= o.activity_since for a in act)
    assert [a.at for a in act] == sorted((a.at for a in act), reverse=True)
    kinds = {a.kind for a in act}
    assert {"fill", "execution", "exit", "halt", "alert"} <= kinds
    halt = next(a for a in act if a.kind == "halt")
    assert halt.tone == "neg" and "arc:reconcile" in halt.text
    assert any(a.kind == "alert" and "resolved" in a.text for a in act)
    assert next(a for a in act if a.kind == "exit").ref == fixture.phash("exit-qqq")
    # the base fixture's reconciles are days old: outside 24 h, inside a week
    assert "reconcile" not in kinds
    week = load_overview(conn, now=NOW, stale_after=STALE, activity_hours=168)
    assert "reconcile" in {a.kind for a in week.activity}


def test_recent_activity_groups_alert_repeats(conn: sqlite3.Connection) -> None:
    """E8.8b: 12 `missed_window` rows in the window collapse into one row with count 12."""
    act = _overview(conn).activity
    missed = [a for a in act if a.group == "missed_window"]
    assert len(missed) == 1
    g = missed[0]
    assert g.count == 12 and len(g.entries) == 12 and g.text == "missed_window ×12"
    assert g.at == max(e.at for e in g.entries) == g.entries[0].at
    assert g.tone == "warn"  # the open scalp alert is the worst tone in the group
    assert [e.at for e in g.entries] == sorted((e.at for e in g.entries), reverse=True)
    assert not any(a.text.startswith("Alert missed_window") for a in act)  # no loose repeats
    # single alerts of other kinds stay plain rows
    single = next(a for a in act if a.text.startswith("Alert coverage"))
    assert single.count == 1 and single.entries == [] and single.group is None
    # the 30 h-old repeat is outside the window, inside a week
    week = load_overview(conn, now=NOW, stale_after=STALE, activity_hours=168)
    assert next(a for a in week.activity if a.group == "missed_window").count == 13


def test_recent_activity_window_boundary(tmp_path: Path) -> None:
    """E8.8b acceptance: a 25 h-old alert is dropped, a 23 h-old one is returned."""
    from arc.monitoring.store import AlertRepo

    path = tmp_path / "act.db"
    c = connect(path)
    migrate(c)
    repo = AlertRepo(c)
    repo.open("old", "tick_stale", "25 h old", at=NOW - dt.timedelta(hours=25), resolved=True)
    repo.open("new", "gateway_down", "23 h old", at=NOW - dt.timedelta(hours=23), resolved=True)
    c.close()
    ro = connect_ro(path)
    try:
        texts = [a.text for a in _overview(ro).activity]
        assert texts == ["Alert gateway_down: 23 h old"]
        both = load_overview(ro, now=NOW, stale_after=STALE, activity_hours=26).activity
        assert len(both) == 2
        with pytest.raises(ValueError, match="activity_hours"):
            load_overview(ro, now=NOW, stale_after=STALE, activity_hours=0)
    finally:
        ro.close()


def test_status_strip(conn: sqlite3.Connection) -> None:
    s = _overview(conn).status
    assert s.halted and s.active_halts == 1 and s.halt is not None
    assert s.halt.actor == "arc:reconcile" and s.halt.at == NOW - dt.timedelta(hours=1, minutes=10)
    assert s.tick_status == "ok" and s.tick_at == NOW - dt.timedelta(minutes=1)
    assert s.health_status == "ok" and s.health_at is not None
    assert [a.kind for a in s.alerts] == ["missed_window"]  # resolved ones are not listed
    assert s.order_budget is not None and (s.order_budget.used, s.order_budget.limit) == (31, 200)


def test_marks_stale_both_ways(conn: sqlite3.Connection) -> None:
    fresh = _overview(conn, now=NOW)
    assert not fresh.marks_stale  # last mark 2 min old, threshold 15 min
    late = _overview(conn, now=NOW + dt.timedelta(minutes=14))
    assert late.marks_stale  # 16 min old
    assert late.stale_after_s == 900


def test_empty_db_overview(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    c = connect(path)
    migrate(c)
    c.close()
    ro = connect_ro(path)
    try:
        o = _overview(ro)
    finally:
        ro.close()
    assert o.equity.value is None and o.equity.series == [] and o.equity.change is None
    assert o.day_pnl.day_pnl is None and o.positions == [] and o.proposals == []
    assert o.movers == [] and o.activity == [] and not o.status.halted and o.marks_stale
    assert o.status.order_budget is None


def test_overview_issues_only_selects(conn: sqlite3.Connection) -> None:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    for rng in RANGES:
        _overview(conn, rng)
    load_positions(conn, now=NOW, status="all", stale_after=STALE)
    conn.set_trace_callback(None)
    assert {s.strip().split()[0].upper() for s in seen if s.strip()} <= {"SELECT", "PRAGMA"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(fx_db: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><div id=root></div>")
    return TestClient(create_app(fx_db, static_dir=static, clock=lambda: NOW))


@pytest.mark.parametrize("rng", RANGES)
def test_api_overview_contract(client: TestClient, rng: str) -> None:
    r = client.get("/api/overview", params={"range": rng})
    assert r.status_code == 200
    o = OverviewResponse.model_validate(r.json())
    assert o.range == rng and o.as_of == NOW and o.stale_after_s == 1800  # 3 x 10-min monitor (D52)
    assert len(o.positions) == 3 and o.status.halted and o.equity.value is not None


def test_api_overview_default_range_and_bad_input(client: TestClient) -> None:
    assert client.get("/api/overview").json()["range"] == "1D"
    bad = client.get("/api/overview", params={"range": "1Y"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"
    assert client.get("/api/positions", params={"status": "x"}).status_code == 422


def test_api_overview_activity_hours(client: TestClient, fx_db: Path, tmp_path: Path) -> None:
    """E8.8b: `activity_hours` (1–168) overrides the config default; the default is
    `tower.overview.activity_hours` from routines.yaml, D26 overrides included."""
    body = client.get("/api/overview").json()
    assert body["activity_hours"] == 24
    wide = client.get("/api/overview", params={"activity_hours": 168}).json()
    assert wide["activity_hours"] == 168 and len(wide["activity"]) > len(body["activity"])
    for bad in (0, 169, "x"):
        r = client.get("/api/overview", params={"activity_hours": bad})
        assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    routines = tmp_path / "routines.yaml"
    raw = (REPO / "config" / "routines.yaml").read_text()
    routines.write_text(raw.replace("activity_hours: 24", "activity_hours: 6"))
    app = create_app(fx_db, static_dir=tmp_path, routines_path=routines, clock=lambda: NOW)
    six = TestClient(app).get("/api/overview").json()
    assert six["activity_hours"] == 6
    since = dt.datetime.fromisoformat(six["activity_since"])
    assert all(dt.datetime.fromisoformat(a["at"]) >= since for a in six["activity"])


def test_activity_hours_is_a_registered_tunable() -> None:
    from arc.control.effective import raw_yaml
    from arc.control.registry import REGISTRY, Target, parse_value, read_raw

    t = REGISTRY["tower.overview.activity_hours"]
    assert t.target is Target.ROUTINES and (t.min, t.max) == (1, 168)
    assert read_raw(t, raw_yaml(t.target)) == 24
    assert parse_value(t, "48h") == 48
    with pytest.raises(ValueError, match="activity_hours"):
        parse_value(t, "200")


def test_api_positions(client: TestClient) -> None:
    for status, n in (("open", 3), ("closed", 1), ("all", 4)):
        r = client.get("/api/positions", params={"status": status})
        assert r.status_code == 200
        body = PositionsResponse.model_validate(r.json())
        assert body.status == status and len(body.items) == n
    assert len(client.get("/api/positions").json()["items"]) == 3


def test_api_overview_uses_effective_caps(fx_db: Path, tmp_path: Path) -> None:
    """The caps come from the effective settings (D26 overrides reach the tower)."""
    from arc.config import ArcSettings

    base = ArcSettings(_env_file=None, max_alloc_pct=0.04, portfolio_dollar_delta_cap_pct=0.2)  # type: ignore[call-arg]
    c = TestClient(create_app(fx_db, base, static_dir=tmp_path, clock=lambda: NOW))
    g = c.get("/api/overview").json()["greeks"]
    assert g["max_alloc_pct"] == pytest.approx(0.04)
    assert g["greeks"]["dollar_delta_cap"] == pytest.approx(0.2 * g["greeks"]["equity"])


def test_api_overview_is_read_only(fx_db: Path, tmp_path: Path) -> None:
    import arc.tower.data as data

    seen: list[str] = []
    real = data.connect_ro

    def traced(path: object) -> object:
        c = real(path)  # type: ignore[arg-type]
        c.set_trace_callback(seen.append)
        return c

    before = _sha(fx_db)
    with mock.patch("arc.tower.routes.deps.connect_ro", side_effect=traced):
        c = TestClient(create_app(fx_db, static_dir=tmp_path, clock=lambda: NOW))
        for rng in RANGES:
            assert c.get("/api/overview", params={"range": rng}).status_code == 200
        assert c.get("/api/positions", params={"status": "all"}).status_code == 200
        assert c.post("/api/overview").status_code == 405
    assert {s.strip().split()[0].upper() for s in seen if s.strip()} <= {"SELECT", "PRAGMA"}
    assert _sha(fx_db) == before


def test_api_overview_missing_db(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path / "nope.db", static_dir=tmp_path, clock=lambda: NOW))
    r = c.get("/api/overview")
    assert r.status_code == 503 and r.json()["error"] == "db_unavailable"


# ---------------------------------------------------------------------------
# performance: < 300 ms on a ~50 MB store
# ---------------------------------------------------------------------------


@pytest.mark.serial  # wall-clock budget: runs alone, after the parallel pass (Makefile)
def test_overview_under_300ms_on_a_50mb_db(tmp_path: Path) -> None:
    path = tmp_path / "big.db"
    fixture.build(path, NOW)
    c = connect(path)
    detail = json.loads(
        c.execute(
            "SELECT detail FROM heartbeats WHERE component = 'monitor' ORDER BY at DESC LIMIT 1"
        ).fetchone()[0]
    )
    detail["legs"] = detail["legs"] * 4  # ~a 20-leg book
    blob = json.dumps(detail)
    start = NOW - dt.timedelta(days=400)
    rows = [
        (f"hb-big-{i}", "monitor", "ok", to_db(start + dt.timedelta(minutes=5 * i)), "{}", blob)
        for i in range(12_000)
    ]
    rows += [
        (f"hb-tick-{i}", "tick", "ok", to_db(start + dt.timedelta(minutes=5 * i)), "{}", "{}")
        for i in range(40_000)
    ]
    with c:
        c.executemany(
            "INSERT INTO heartbeats (id, component, status, at, correlation, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    c.close()
    size_mb = path.stat().st_size / 1e6
    assert size_mb >= 50, size_mb
    ro = connect_ro(path)
    try:
        _overview(ro)  # warm the page cache, as a long-running server is
        t0 = time.perf_counter()
        for rng in ("1D", "1M", "ALL"):
            _overview(ro, rng)
        per_call = (time.perf_counter() - t0) / 3
    finally:
        ro.close()
    assert per_call < 0.3, f"{per_call * 1000:.0f} ms on {size_mb:.0f} MB"
