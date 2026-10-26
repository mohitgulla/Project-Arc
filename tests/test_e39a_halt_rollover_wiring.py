"""E3.9a (D137): the routines tick, ``monitor`` and the Tower see a daily-loss halt end
at the next session. Every test injects ``now``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcSettings
from arc.gate import AccountSnapshot, HaltSwitch
from arc.gate.halt import SESSION_ROLLOVER_ACTOR
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.monitor import monitor
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.tower.data import _halts
from arc.tower.data_ops import load_halts
from arc.utils.calendar import ET
from tests.test_routines_e53 import _ctx, _env, _settings
from tests.test_routines_e510 import cfg

if TYPE_CHECKING:
    import sqlite3

FRI_1330 = dt.datetime(2026, 10, 23, 13, 30, tzinfo=ET)
MON_0920 = dt.datetime(2026, 10, 26, 9, 20, tzinfo=ET)
MON_0940 = dt.datetime(2026, 10, 26, 9, 40, tzinfo=ET)

YAML = """
    personas:
      research: {every: 10m, window: "09:20-16:00", days: trading, ttl: 5m}
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _friday_halt(conn: sqlite3.Connection) -> str:
    settings = ArcSettings(env="paper")  # type: ignore[call-arg]
    acct = AccountSnapshot(equity=D("19400"), last_equity=D("20040"), as_of=FRI_1330)
    rec = HaltSwitch(HaltRepo(conn)).check_daily_loss(acct, settings, now=FRI_1330, ticks=2)
    assert rec is not None
    return rec.id


def _dispatcher(
    conn: sqlite3.Connection, calls: list[tuple[str, bool]], notifier: RecordingNotifier
) -> Dispatcher:
    def research(ctx: JobContext) -> JobResult:
        # what the open path reads at run time (E3.9: research skips under opens-only)
        calls.append(("research", HaltSwitch(HaltRepo(ctx.conn)).opens_blocked()))
        return JobResult(summary="research done")

    return Dispatcher(conn, cfg(YAML), handlers={"research": research}, notifier=notifier)


def test_tick_clears_stale_daily_loss_before_the_first_slot(conn: sqlite3.Connection) -> None:
    halt_id = _friday_halt(conn)
    calls: list[tuple[str, bool]] = []
    notifier = RecordingNotifier()
    _dispatcher(conn, calls, notifier).tick(MON_0920)
    assert calls == [("research", False)]  # the pre-open slot already sees opens allowed
    row = conn.execute("SELECT cleared_by, cleared_at FROM halts WHERE id = ?", (halt_id,))
    cleared_by, cleared_at = row.fetchone()
    assert cleared_by == SESSION_ROLLOVER_ACTOR and cleared_at is not None
    lines = [t for _, t in notifier.posts if "expired at session open" in t]
    assert lines == ["[Ops] daily-loss halt from Fri Oct 23 expired at session open; opens allowed"]
    assert notifier.posts[0][0] == MON_0920.date()  # today's day thread
    # posted once: the next tick clears nothing and posts nothing more
    _dispatcher(conn, calls, notifier).tick(MON_0940)
    assert len([t for _, t in notifier.posts if "expired at session open" in t]) == 1


def test_dry_run_tick_does_not_clear(conn: sqlite3.Connection) -> None:
    halt_id = _friday_halt(conn)
    _dispatcher(conn, [], RecordingNotifier()).tick(MON_0920, dry_run=True)
    row = conn.execute("SELECT cleared_at FROM halts WHERE id = ?", (halt_id,)).fetchone()
    assert row[0] is None


def test_tick_survives_a_failed_clear(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _friday_halt(conn)
    disp = _dispatcher(conn, [], RecordingNotifier())

    def boom(self: HaltSwitch, now: dt.datetime) -> list[object]:
        raise RuntimeError("store locked")

    monkeypatch.setattr(HaltSwitch, "expire_stale_session_halts", boom)
    disp._expire_session_halts(MON_0920)  # logged, no raise


def test_tick_survives_a_failed_post(conn: sqlite3.Connection) -> None:
    halt_id = _friday_halt(conn)

    class Broken(RecordingNotifier):
        def post(self, day: dt.date, text: str, blocks: object = None) -> str | None:
            raise RuntimeError("slack down")

    disp = Dispatcher(conn, cfg(YAML), handlers={}, notifier=Broken())
    disp._expire_session_halts(MON_0920)
    row = conn.execute("SELECT cleared_by FROM halts WHERE id = ?", (halt_id,)).fetchone()
    assert row[0] == SESSION_ROLLOVER_ACTOR


def test_manual_monitor_run_clears_and_notices(conn: sqlite3.Connection) -> None:
    _friday_halt(conn)
    ctx = _ctx(conn, "monitor", {"every": "10m"}, MON_0940, _settings())
    r = monitor(ctx, _env([], equity="20100", last="20040"))
    assert "daily-loss halt from Fri Oct 23 expired at session open" in r.notice
    assert r.metrics["opens_halted"] is False and r.metrics["halted"] is False


def test_tower_reads_agree_before_the_clear(conn: sqlite3.Connection) -> None:
    _friday_halt(conn)
    HaltSwitch(HaltRepo(conn)).halt(actor="U1", reason="hold", now=FRI_1330)
    sat = dt.datetime(2026, 10, 24, 10, 0, tzinfo=ET)
    views = {h.kind: h.active for h in _halts(conn, 50, sat)}
    assert views == {"daily_loss": False, "manual": True}
    assert {h.kind: h.active for h in _halts(conn, 50)}["daily_loss"] is True  # no clock
    resp = load_halts(conn, now=sat)
    assert resp.active == 1
    assert {h.kind: h.active for h in resp.halts} == {"daily_loss": False, "manual": True}
    fri = load_halts(conn, now=FRI_1330 + dt.timedelta(hours=1))
    assert fri.active == 2
