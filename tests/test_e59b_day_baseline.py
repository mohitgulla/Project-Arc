"""E5.9b (D43): one start-of-day equity (Arc's prior close) for every day-P&L surface.

The observed failure (Fri 2026-10-02): the loop roots computed day P&L against
Alpaca's ``last_equity`` (official closing prices, 102,239.29) while Arc's own
Thursday close (live-quote marks) was 101,241.15, so the 09:40 root showed
-$1,004 with nothing traded. These tests replay those numbers.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from arc.broker.base import AccountInfo
from arc.config import ArcSettings
from arc.context import ContextStore
from arc.gate.halt import HaltSwitch
from arc.gate.rules import check_daily_loss
from arc.monitoring.store import HeartbeatRepo
from arc.pipeline.env import PipelineEnv
from arc.pipeline.market import account_baseline, account_snapshot
from arc.pipeline.portfolio_context import build_portfolio_context
from arc.reconcile.baseline import Baseline, day_pnl, start_of_day_equity
from arc.reconcile.engine import reconcile
from arc.routines.config import RoutinesConfig
from arc.routines.handlers import JobContext
from arc.routines.loop import latest_account_facts, loop_root_from_db
from arc.routines.monitor import monitor
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo, PnlSnapshotRepo
from arc.tower.api import create_app
from arc.tower.data import connect_ro, load_snapshot
from arc.tower.data_overview import load_overview
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

THU, FRI, MON = dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 5)
THU_CLOSE, THU_BROKER = D("101241.15"), D("102239.29")
FRI_CLOSE, FRI_BROKER = D("99970.24"), D("101045.13")
FRI_0940 = dt.datetime(2026, 10, 2, 9, 40, tzinfo=ET)
MON_0940 = dt.datetime(2026, 10, 5, 9, 40, tzinfo=ET)


def _settings() -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", account_profile="margin")  # type: ignore[call-arg]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = connect(path)
    migrate(c)
    return c


@pytest.fixture
def conn() -> sqlite3.Connection:
    return _db()


def _close(conn: sqlite3.Connection, day: dt.date, equity: D, at: str, **extra: str) -> None:
    PnlSnapshotRepo(conn).insert(
        realized="0", unrealized="0", total="0",
        details_json=json.dumps({"day": day.isoformat(), "equity": str(equity), **extra}),
        snapshot_at=at,
    )  # fmt: skip


def _info(equity: D, last: D | None) -> AccountInfo:
    return AccountInfo(
        account_id="PAPER", equity=equity, buying_power=D("50000"), cash=D("50000"),
        last_equity=last,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# start_of_day_equity
# ---------------------------------------------------------------------------


def test_fri_oct_2_replay_uses_arc_close_not_broker(conn: sqlite3.Connection) -> None:
    _close(conn, THU, THU_CLOSE, "2026-10-01T20:32:00.492479Z", last_equity="100000")
    base = start_of_day_equity(conn, FRI, broker_last_equity=THU_BROKER)
    assert base is not None
    assert base.value == THU_CLOSE and base.source == "arc_close"
    assert base.prev_session == THU and base.day == FRI
    assert base.as_of == dt.datetime(2026, 10, 1, 20, 32, 0, 492479, tzinfo=dt.UTC)
    assert day_pnl(D("101235.29"), base) == D("-5.86")  # not -1,004.00


def test_monday_case_rolls_back_over_the_weekend(conn: sqlite3.Connection) -> None:
    _close(conn, THU, THU_CLOSE, "2026-10-01T20:32:00Z")
    _close(conn, FRI, FRI_CLOSE, "2026-10-02T20:35:01.059434Z")
    base = start_of_day_equity(conn, MON, broker_last_equity=FRI_BROKER)
    assert base is not None and base.value == FRI_CLOSE and base.source == "arc_close"
    assert base.prev_session == FRI
    # Saturday / Sunday (not sessions) also resolve to Friday's close
    for d in (dt.date(2026, 10, 3), dt.date(2026, 10, 4)):
        b = start_of_day_equity(conn, d, broker_last_equity=FRI_BROKER)
        assert b is not None and b.prev_session == FRI and b.value == FRI_CLOSE


def test_previous_session_skips_market_holidays(conn: sqlite3.Connection) -> None:
    # Thanksgiving Thu 2026-11-26: Friday's baseline is Wednesday's close.
    _close(conn, dt.date(2026, 11, 25), D("100100"), "2026-11-25T21:30:00Z")
    _close(conn, dt.date(2026, 11, 26), D("1"), "2026-11-26T21:30:00Z")  # stray holiday row
    b = start_of_day_equity(conn, dt.date(2026, 11, 27), broker_last_equity=D("99000"))
    assert b is not None and b.prev_session == dt.date(2026, 11, 25) and b.value == D("100100")
    # Labor Day Mon 2026-09-07 + weekend: Tuesday's baseline is Friday 09-04's close.
    _close(conn, dt.date(2026, 9, 4), D("100200"), "2026-09-04T20:30:00Z")
    b = start_of_day_equity(conn, dt.date(2026, 9, 8), broker_last_equity=None)
    assert b is not None and b.prev_session == dt.date(2026, 9, 4) and b.value == D("100200")


def test_falls_back_to_broker_without_an_arc_close(conn: sqlite3.Connection) -> None:
    b = start_of_day_equity(conn, FRI, broker_last_equity=THU_BROKER)
    assert b == Baseline(value=THU_BROKER, source="broker_last_equity", as_of=None, day=FRI,
                         prev_session=THU)  # fmt: skip
    # a gap of more than one session: Wednesday's close is not Thursday's
    _close(conn, dt.date(2026, 9, 30), D("100500"), "2026-09-30T20:30:00Z")
    b = start_of_day_equity(conn, FRI, broker_last_equity=THU_BROKER)
    assert b is not None and b.source == "broker_last_equity"
    # neither: unknown
    assert start_of_day_equity(conn, FRI, broker_last_equity=None) is None
    assert start_of_day_equity(conn, FRI, broker_last_equity=D(0)) is None
    assert day_pnl(D(1), None) is None and day_pnl(None, b) is None


def test_newest_valid_row_of_the_session_wins(conn: sqlite3.Connection) -> None:
    _close(conn, THU, D("101000"), "2026-10-01T20:30:00Z")
    _close(conn, THU, THU_CLOSE, "2026-10-01T20:32:00Z")  # reconcile re-run supersedes
    PnlSnapshotRepo(conn).insert(  # newest, but no usable equity: skipped
        realized="0",
        unrealized="0",
        total="0",
        details_json=json.dumps({"day": THU.isoformat(), "equity": None}),
        snapshot_at="2026-10-01T20:40:00Z",
    )
    PnlSnapshotRepo(conn).insert(
        realized="0",
        unrealized="0",
        total="0",
        details_json=json.dumps({"day": THU.isoformat(), "equity": "nan"}),
        snapshot_at="2026-10-01T20:41:00Z",
    )
    b = start_of_day_equity(conn, FRI, broker_last_equity=THU_BROKER)
    assert b is not None and b.value == THU_CLOSE


def test_no_pnl_table_is_no_arc_close() -> None:
    import sqlite3 as _sqlite3

    bare = _sqlite3.connect(":memory:")
    b = start_of_day_equity(bare, FRI, broker_last_equity=THU_BROKER)
    assert b is not None and b.source == "broker_last_equity"


# ---------------------------------------------------------------------------
# Gate + halt get the Arc-close baseline (override through account_snapshot)
# ---------------------------------------------------------------------------


def test_gate_daily_loss_and_halt_use_the_arc_close(conn: sqlite3.Connection) -> None:
    """Mon: equity 98,000. vs broker 101,045.13 it is -3.01% (halt); vs Arc 99,970.24 -1.97%."""
    _close(conn, FRI, FRI_CLOSE, "2026-10-02T20:35:01Z")
    cfg = _settings()
    info = _info(D("98000"), FRI_BROKER)
    base = account_baseline(conn, info, MON_0940)
    snap = account_snapshot(info, MON_0940, baseline=base)
    assert snap.last_equity == FRI_CLOSE  # the gate's start-of-day basis
    assert check_daily_loss(snap, cfg) == []
    switch = HaltSwitch(HaltRepo(conn))
    assert switch.check_daily_loss(snap, cfg, now=MON_0940) is None
    # control: the raw broker value would have halted the account on a phantom loss
    raw = snap.model_copy(update={"last_equity": FRI_BROKER})
    assert check_daily_loss(raw, cfg) != []
    # a real 3% loss against the Arc close still halts
    real = account_snapshot(_info(D("96970"), FRI_BROKER), MON_0940, baseline=base)
    assert check_daily_loss(real, cfg) != []
    assert switch.check_daily_loss(real, cfg, now=MON_0940) is not None


def test_gate_fails_closed_without_any_baseline() -> None:
    snap = account_snapshot(_info(D("100000"), None), FRI_0940, baseline=None)
    assert snap.last_equity == 0 and check_daily_loss(snap, _settings()) != []


# ---------------------------------------------------------------------------
# Monitor, portfolio_context, reconcile
# ---------------------------------------------------------------------------


def _ctx(conn: sqlite3.Connection, now: dt.datetime) -> JobContext:
    routines = RoutinesConfig.model_validate({"personas": {"monitor": {"every": "5m"}}})
    kind, step = routines.step("monitor")
    return JobContext(
        job="monitor", kind=kind, spec=step, run_id="run-mon", chain_run_id=None,
        scheduled_for=now, now=now, conn=conn, snapshot=ContextStore(conn).snapshot(now),
        routines=routines, settings_factory=_settings,
    )  # fmt: skip


def _env(info: AccountInfo) -> PipelineEnv:
    env = PipelineEnv.fixtures()
    env.account = lambda: info
    env.positions = lambda: []
    return env


def test_monitor_heartbeat_carries_the_baseline(conn: sqlite3.Connection) -> None:
    _close(conn, FRI, FRI_CLOSE, "2026-10-02T20:35:01Z")
    r = monitor(_ctx(conn, MON_0940), _env(_info(D("98000"), FRI_BROKER)))
    assert r.metrics["halt_raised"] is False  # vs broker this would be a -3.01% halt
    assert "day P&L $-1,970.24" in r.summary
    hb = HeartbeatRepo(conn).latest("monitor")
    assert hb is not None
    assert hb.detail["prev_close"] == float(FRI_CLOSE)
    assert hb.detail["prev_close_source"] == "arc_close"
    assert hb.detail["day_pnl"] == pytest.approx(-1970.24)
    assert hb.detail["last_equity"] == float(FRI_BROKER)  # raw broker value kept for audit


def test_portfolio_context_day_pnl_and_digest_bucket(conn: sqlite3.Connection) -> None:
    from arc.routines.loop import pnl_bucket

    _close(conn, THU, THU_CLOSE, "2026-10-01T20:32:00Z")
    info = _info(D("101235.29"), THU_BROKER)
    pc = build_portfolio_context(
        conn, _env(info), _settings(), info=info, now=FRI_0940, halted=False,
        budget_tier="normal",
    )  # fmt: skip
    assert pc.account.day_pnl == pytest.approx(-5.86)
    assert pc.account.prev_close == float(THU_CLOSE)
    assert pc.account.prev_close_source == "arc_close"
    # Research digest bucket sees ~0, not a -1% move (bucket 0.5% of equity)
    assert pnl_bucket(pc.account.day_pnl, pc.account.equity, 0.5) == -1
    assert pnl_bucket(-1004.0, pc.account.equity, 0.5) == -2


def test_reconcile_writes_prev_close_and_day_pnl_from_it(conn: sqlite3.Connection) -> None:
    from tests.test_reconcile import FakeBroker

    _close(conn, THU, THU_CLOSE, "2026-10-01T20:32:00Z")
    now = dt.datetime(2026, 10, 2, 16, 35, tzinfo=ET)
    broker = FakeBroker(equity=str(FRI_CLOSE), last_equity=str(THU_BROKER))
    rep = reconcile(conn, broker, settings=_settings(), now=now, run_id="r")  # type: ignore[arg-type]
    assert rep.day_pnl == FRI_CLOSE - THU_CLOSE  # -1,270.91, not -2,269.05
    assert rep.baseline is not None and rep.baseline.source == "arc_close"
    row = conn.execute(
        "SELECT details_json FROM pnl_snapshots WHERE id = ?", (rep.pnl_snapshot_id,)
    ).fetchone()
    d = json.loads(row[0])
    assert d["prev_close"] == str(THU_CLOSE) and d["prev_close_source"] == "arc_close"
    assert d["last_equity"] == str(THU_BROKER) and d["day_pnl"] == str(FRI_CLOSE - THU_CLOSE)


# ---------------------------------------------------------------------------
# Tower parity: loop root == /api/overview == /api/snapshot == 1D chart start
# ---------------------------------------------------------------------------


@pytest.fixture
def parity_db(tmp_path: Path) -> Path:
    path = tmp_path / "arc.db"
    c = _db(path)
    _close(c, THU, THU_CLOSE, "2026-10-01T20:32:00Z", last_equity="100000")
    info = _info(D("101235.29"), THU_BROKER)
    monitor(_ctx(c, FRI_0940), _env(info))
    pc = build_portfolio_context(
        c, _env(info), _settings(), info=info, now=FRI_0940, halted=False, budget_tier="normal"
    )
    ContextStore(c).write(
        kind="portfolio_context", subject="session", payload=pc, produced_by="research",
        chain_run_id="chain-1", now=FRI_0940,
    )  # fmt: skip
    c.close()
    return path


def test_tower_parity_with_the_loop_root(parity_db: Path, tmp_path: Path) -> None:
    c = connect(parity_db)
    try:
        root = loop_root_from_db(c, "chain-1", slot=FRI_0940)
        facts = latest_account_facts(c, FRI_0940)
    finally:
        c.close()
    assert root.day_pnl == pytest.approx(-5.86) and facts.day_pnl == root.day_pnl
    assert root.equity is not None and root.day_pnl is not None
    assert root.equity - root.day_pnl == pytest.approx(float(THU_CLOSE))

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><div id=root></div>")
    client = TestClient(create_app(parity_db, static_dir=static, clock=lambda: FRI_0940))
    ov = client.get("/api/overview", params={"range": "1D"})
    snap = client.get("/api/snapshot")
    assert ov.status_code == 200 and snap.status_code == 200
    day, eq = ov.json()["day_pnl"], ov.json()["equity"]
    pnl = snap.json()["pnl"]

    assert D(day["day_pnl"]) == D(pnl["intraday_day_pnl"]) == D("-5.86")
    assert D(day["prev_equity"]) == D(pnl["intraday_prev_close"]) == THU_CLOSE
    assert day["prev_close_source"] == pnl["intraday_prev_close_source"] == "arc_close"
    assert eq["start_label"] == "prev close" and eq["start_source"] == "arc_close"
    assert D(eq["start_value"]) == THU_CLOSE
    assert D(eq["value"]) - D(eq["start_value"]) == D(day["day_pnl"])
    assert float(D(day["day_pnl"])) == pytest.approx(root.day_pnl)


def test_tower_recomputes_legacy_rows_and_flags_broker_fallback(tmp_path: Path) -> None:
    """Pre-E5.9b heartbeats carry only last_equity: the tower derives the same baseline."""
    path = tmp_path / "legacy.db"
    c = _db(path)
    _close(c, THU, THU_CLOSE, "2026-10-01T20:32:00Z", last_equity="100000", day_pnl="1241.15")
    HeartbeatRepo(c).record(
        "monitor", "ok", at=FRI_0940,
        detail={"valued": True, "equity": 101235.29, "last_equity": float(THU_BROKER)},
    )  # fmt: skip
    c.close()
    ro = connect_ro(path)
    try:
        o = load_overview(ro, now=FRI_0940, stale_after=dt.timedelta(minutes=15))
        s = load_snapshot(ro, now=FRI_0940)
    finally:
        ro.close()
    assert o.day_pnl.day_pnl == D("-5.86") and o.day_pnl.prev_close_source == "arc_close"
    assert s.pnl.intraday_day_pnl == D("-5.86")

    # no Arc close at all: broker fallback, surfaced as such
    path2 = tmp_path / "fresh.db"
    c = _db(path2)
    HeartbeatRepo(c).record(
        "monitor", "ok", at=FRI_0940,
        detail={"valued": True, "equity": 101235.29, "last_equity": float(THU_BROKER)},
    )  # fmt: skip
    c.close()
    ro = connect_ro(path2)
    try:
        o = load_overview(ro, now=FRI_0940, stale_after=dt.timedelta(minutes=15))
    finally:
        ro.close()
    assert o.day_pnl.prev_close_source == "broker_last_equity"
    assert o.day_pnl.prev_equity == THU_BROKER and o.equity.start_source == "broker_last_equity"


def test_tower_reconciled_day_pnl_uses_the_prior_arc_close(tmp_path: Path) -> None:
    """After the EOD reconcile (newer than the last mark) the card shows close-to-close."""
    path = tmp_path / "rec.db"
    c = _db(path)
    _close(c, THU, THU_CLOSE, "2026-10-01T20:32:00Z")
    # a legacy Friday row whose stored day_pnl was against the broker (-2,269.05)
    _close(c, FRI, FRI_CLOSE, "2026-10-02T20:35:01Z", last_equity=str(THU_BROKER),
           day_pnl="-2269.05")  # fmt: skip
    c.close()
    ro = connect_ro(path)
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=ET)
    try:
        o = load_overview(ro, now=now, stale_after=dt.timedelta(minutes=15))
        s = load_snapshot(ro, now=now)
    finally:
        ro.close()
    d = o.day_pnl
    assert d.source == "reconciled" and d.prev_equity == THU_CLOSE
    assert d.day_pnl == FRI_CLOSE - THU_CLOSE == s.pnl.day_pnl
    assert d.performance is not None and d.performance.day_pnl == pytest.approx(
        float(FRI_CLOSE - THU_CLOSE)
    )
