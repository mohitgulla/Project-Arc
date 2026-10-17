"""E3.9 (D122): an opens-only daily-loss halt never stops the exit jobs.

The dispatcher's default halt check is ``HaltSwitch.is_halted()`` (scope ``all`` only),
so under a daily-loss halt (scope ``opens``) the Trade Loop (``research`` + its exit
steps) and ``positions.evaluate`` run; a manual ``!halt`` (scope ``all``) still skips
them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcSettings
from arc.gate import AccountSnapshot, HaltSwitch
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.utils.calendar import ET
from tests.test_routines_e510 import cfg

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 10, 16, 14, 10, tzinfo=ET)  # a Friday, in session

YAML = """
    personas:
      research: {every: 10m, window: "09:30-16:00", days: trading, ttl: 5m}
      positions.evaluate: {every: 10m, window: "09:30-16:00", days: trading, ttl: 5m}
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _dispatcher(conn: sqlite3.Connection, calls: list[str]) -> Dispatcher:
    def handler(name: str):  # noqa: ANN202
        def run(ctx: JobContext) -> JobResult:
            calls.append(name)
            return JobResult(summary=f"{name} done")

        return run

    return Dispatcher(
        conn,
        cfg(YAML),
        handlers={n: handler(n) for n in ("research", "positions.evaluate")},
        notifier=RecordingNotifier(),
    )


def _daily_loss_halt(conn: sqlite3.Connection) -> None:
    settings = ArcSettings(env="paper")  # type: ignore[call-arg]
    acct = AccountSnapshot(equity=D("23480.75"), last_equity=D("24210.23"), as_of=NOW)
    rec = HaltSwitch(HaltRepo(conn)).check_daily_loss(acct, settings, now=NOW, ticks=2)
    assert rec is not None


def test_research_and_positions_evaluate_run_under_daily_loss_halt(
    conn: sqlite3.Connection,
) -> None:
    _daily_loss_halt(conn)
    switch = HaltSwitch(HaltRepo(conn))
    assert switch.opens_blocked() and not switch.is_halted()
    calls: list[str] = []
    report = _dispatcher(conn, calls).tick(NOW)
    assert sorted(calls) == ["positions.evaluate", "research"]
    reasons = {o.job: (o.status, o.reason) for o in report.outcomes}
    assert all(r != "halted (persona)" for _, r in reasons.values())
    assert not report.halted


def test_manual_halt_still_skips_them(conn: sqlite3.Connection) -> None:
    HaltSwitch(HaltRepo(conn)).halt(actor="U1", reason="manual", now=NOW)
    calls: list[str] = []
    report = _dispatcher(conn, calls).tick(NOW)
    assert calls == []
    assert {o.reason for o in report.outcomes} == {"halted (persona)"}
