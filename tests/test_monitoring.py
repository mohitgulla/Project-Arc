"""E8.2 monitoring: heartbeats, watchdog checks, gateway health, alerts, logs, correlation."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from pydantic import ValidationError

from arc.cli import main
from arc.monitoring import alerts, checks, correlation, logs
from arc.monitoring.config import AlertChannel, GatewayCheck, LogSettings, MonitoringSettings
from arc.monitoring.store import AlertRepo, HeartbeatRepo
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.runs import RoutineRunRepo, RunStatus
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent


def et(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ET)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(autouse=True)
def _reset_structlog() -> Any:
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
    lg = logging.getLogger(logs.FILE_LOGGER)
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()


YAML = """
    sources:
      rss: {every: 30m, window: "06:00-20:00", days: trading}
    personas:
      director: {schedule: ["09:30"], days: trading, chain: [quant], ttl: 2h}
    steps:
      quant: {}
"""


def cfg(text: str = YAML) -> RoutinesConfig:
    return RoutinesConfig.model_validate(yaml.safe_load(textwrap.dedent(text)))


MS = MonitoringSettings()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_shipped_config_has_monitoring_section() -> None:
    ms = load_routines().monitoring
    assert ms.tick_stale_after == dt.timedelta(minutes=15)
    assert ms.stuck_after > dt.timedelta(minutes=55)  # longer than the tick timeout
    assert ms.alert_channel is AlertChannel.PROJECT_ARC
    assert ms.gateway.enabled
    assert ms.log.path == Path("logs/arc.jsonl")


def test_monitoring_config_parses_durations_and_rejects_unknown() -> None:
    ms = MonitoringSettings.model_validate(
        {"tick_stale_after": "20m", "gateway": {"timeout": "5s"}, "miss_lookback": "2d"}
    )
    assert ms.tick_stale_after == dt.timedelta(minutes=20)
    assert ms.gateway.timeout == dt.timedelta(seconds=5)
    assert ms.miss_lookback == dt.timedelta(days=2)
    with pytest.raises(ValidationError):
        MonitoringSettings.model_validate({"nope": 1})
    with pytest.raises(ValidationError):
        LogSettings.model_validate({"max_bytes": 10})


def test_routines_config_defaults_monitoring() -> None:
    assert cfg().monitoring == MonitoringSettings()


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_heartbeats_are_append_only(conn: sqlite3.Connection) -> None:
    repo = HeartbeatRepo(conn)
    hb = repo.record("tick", "ok", at=et(2026, 9, 28, 9, 0), correlation={"tick_id": "tick-1"})
    assert repo.latest("tick") == hb
    assert repo.first("tick") == hb
    assert repo.latest("health") is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE heartbeats SET status = 'failed'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM heartbeats")


def test_heartbeat_latest_recent_find(conn: sqlite3.Connection) -> None:
    repo = HeartbeatRepo(conn)
    repo.record("tick", "ok", at=et(2026, 9, 28, 9, 0), correlation={"tick_id": "tick-a"})
    b = repo.record(
        "tick", "failed", at=et(2026, 9, 28, 9, 5), detail={"outcomes": [{"run_id": "run-x"}]}
    )
    repo.record("health", "ok", at=et(2026, 9, 28, 9, 6))
    assert repo.latest("tick") == b
    assert [h.component for h in repo.recent()] == ["health", "tick", "tick"]
    assert len(repo.recent(component="tick", limit=1)) == 1
    assert [h.id for h in repo.find("run-x")] == [b.id]
    assert repo.find("tick-a")[0].correlation == {"tick_id": "tick-a"}


def test_alert_repo_one_open_per_key(conn: sqlite3.Connection) -> None:
    repo = AlertRepo(conn)
    now = et(2026, 9, 28, 9, 0)
    a = repo.open("gateway", "gateway_down", "down", at=now)
    assert repo.seen("gateway")
    assert repo.open_for("gateway") == a
    with pytest.raises(sqlite3.IntegrityError):
        repo.open("gateway", "gateway_down", "again", at=now)
    repo.set_posted([a.id], "123.456")
    assert repo.find("123.456")[0].posted_ts == "123.456"
    assert repo.resolve("gateway", at=now) is not None
    assert repo.resolve("gateway", at=now) is None
    assert repo.open_alerts() == []
    # A recurrence after resolution opens a new row.
    b = repo.open("gateway", "gateway_down", "down again", at=now)
    assert b.id != a.id
    one_off = repo.open("missed:x", "missed_window", "m", at=now, resolved=True)
    assert one_off.resolved_at == now
    assert [x.key for x in repo.open_alerts()] == ["gateway"]


# ---------------------------------------------------------------------------
# checks: tick staleness / stuck runs
# ---------------------------------------------------------------------------


def test_tick_staleness(conn: sqlite3.Connection) -> None:
    now = et(2026, 9, 28, 10, 0)
    r = checks.tick_staleness(conn, MS, now)
    assert r.severity == "failed"
    assert r.findings[0].key == "tick_stale"
    HeartbeatRepo(conn).record("tick", "ok", at=now - dt.timedelta(minutes=5),
                               correlation={"tick_id": "tick-z"})  # fmt: skip
    r = checks.tick_staleness(conn, MS, now)
    assert r.severity == "ok"
    assert "tick-z" in r.summary
    r = checks.tick_staleness(conn, MS, now + dt.timedelta(minutes=20))
    assert r.severity == "failed"
    assert "25 min ago" in r.findings[0].message


def test_stuck_runs(conn: sqlite3.Connection) -> None:
    repo = RoutineRunRepo(conn)
    t0 = et(2026, 9, 28, 9, 0)
    stuck = repo.claim(job="director", scheduled_for=t0, reason="schedule", now=t0)
    fresh = repo.claim(
        job="rss", scheduled_for=t0, reason="schedule", now=t0 + dt.timedelta(hours=1)
    )
    assert stuck is not None and fresh is not None
    r = checks.stuck_runs(conn, MS, t0 + dt.timedelta(minutes=90))
    assert r.severity == "failed"
    assert [f.key for f in r.findings] == [f"stuck:{stuck.run_id}"]
    repo.finish(stuck.run_id, status=RunStatus.OK)
    assert checks.stuck_runs(conn, MS, t0 + dt.timedelta(minutes=90)).severity == "ok"


# ---------------------------------------------------------------------------
# checks: missed routine windows
# ---------------------------------------------------------------------------


class Handlers:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, name: str) -> Any:
        def h(ctx: JobContext) -> JobResult:
            self.calls.append(name)
            return JobResult(summary=f"{name} ok")

        return h


def _dispatcher(conn: sqlite3.Connection, routines: RoutinesConfig) -> Dispatcher:
    rec = Handlers()
    return Dispatcher(
        conn,
        routines,
        handlers={n: rec(n) for n in ("rss", "director", "quant")},
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
    )


def test_missed_windows_not_judged_before_first_tick(conn: sqlite3.Connection) -> None:
    r = checks.missed_windows(conn, cfg(), MS, et(2026, 9, 28, 15, 0))
    assert r.severity == "ok"
    assert "not judged" in r.summary


def test_missed_window_when_ticks_stop(conn: sqlite3.Connection) -> None:
    routines = cfg()
    d = _dispatcher(conn, routines)
    # Ticks every 5 min 06:00-09:00, then the cron dies.
    t = et(2026, 9, 28, 6, 0)
    while t <= et(2026, 9, 28, 9, 0):
        d.tick(t)
        HeartbeatRepo(conn).record("tick", "ok", at=t)
        t += dt.timedelta(minutes=5)
    # 11:00: director 09:30 is still within its 2h window (+10m grace) -> not judged.
    r = checks.missed_windows(conn, routines, MS, et(2026, 9, 28, 11, 0))
    assert "director" not in json.dumps([f.detail for f in r.findings])
    # 11:45: director 09:30 window closed 11:30, +10m grace passed -> missed.
    r = checks.missed_windows(conn, routines, MS, et(2026, 9, 28, 11, 45))
    missed = {f.detail["job"] for f in r.findings}
    assert "director" in missed
    assert "rss" in missed  # 09:30.. rss slots too
    assert all(f.mode == checks.ONE_OFF for f in r.findings)
    assert r.severity == "failed"


def test_collapsed_and_recorded_missed_slots(conn: sqlite3.Connection) -> None:
    routines = cfg()
    d = _dispatcher(conn, routines)
    HeartbeatRepo(conn).record("tick", "ok", at=et(2026, 9, 28, 6, 0))
    d.tick(et(2026, 9, 28, 6, 0))
    # Gap 06:00 -> 07:10: rss 06:30 and 07:00 collapse into one run of 07:00 (in window).
    d.tick(et(2026, 9, 28, 7, 10))
    HeartbeatRepo(conn).record("tick", "ok", at=et(2026, 9, 28, 7, 10))
    r = checks.missed_windows(conn, routines, MS, et(2026, 9, 28, 7, 50))
    assert not [f for f in r.findings if f.detail["job"] == "rss"]
    # Director 09:30 missed and recorded as skip-missed by a late tick at 12:00.
    d.tick(et(2026, 9, 28, 12, 0))
    r = checks.missed_windows(conn, routines, MS, et(2026, 9, 28, 12, 1))
    director = [f for f in r.findings if f.detail["job"] == "director"]
    assert director and "recorded as missed" in director[0].message


def test_missed_window_ignores_halt_skips(conn: sqlite3.Connection) -> None:
    routines = cfg()
    d = Dispatcher(
        conn, routines, handlers={}, notifier=RecordingNotifier(), is_halted=lambda: True
    )
    HeartbeatRepo(conn).record("tick", "ok", at=et(2026, 9, 28, 9, 0))
    d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
    d.tick(et(2026, 9, 28, 9, 35))
    r = checks.missed_windows(conn, routines, MS, et(2026, 9, 28, 12, 0))
    assert not [f for f in r.findings if f.detail["job"] == "director"]


# ---------------------------------------------------------------------------
# checks: gateway
# ---------------------------------------------------------------------------

GW_OK = "✓ Gateway is running (PID 42)\n  Service: launchd\n"
GW_WARN = "✓ Gateway is running (PID 42)\n⚠ Service definition is stale relative to install\n"
GW_DOWN = "✗ Gateway is not running\n  Start with: hermes gateway start\n"
CRON_OK = "✓ Gateway is running — cron jobs will fire automatically\n\n  3 active job(s)\n"
CRON_DOWN = "✗ Gateway is not running — cron jobs will NOT fire\n"


def fake(outputs: dict[str, tuple[int, str]]) -> Any:
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        calls.append(argv)
        return outputs[argv[1]]

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def gw(tmp_path: Path) -> GatewayCheck:
    exe = tmp_path / "hermes"
    exe.write_text("#!/bin/sh\n")
    return GatewayCheck(hermes_bin=str(exe))


def test_parse_status_markers() -> None:
    assert checks.parse_status(GW_OK) == ("ok", [], [])
    sev, errs, warns = checks.parse_status(GW_WARN)
    assert sev == "degraded" and warns == ["Service definition is stale relative to install"]
    sev, errs, _ = checks.parse_status(GW_DOWN)
    assert sev == "failed" and errs == ["Gateway is not running"]


def test_gateway_ok(gw: GatewayCheck) -> None:
    run = fake({"gateway": (0, GW_OK), "cron": (0, CRON_OK)})
    r = checks.gateway_health(gw, run)
    assert r.severity == "ok" and not r.findings
    assert "Gateway is running" in r.summary
    assert [c[1:] for c in run.calls] == [["gateway", "status"], ["cron", "status"]]


def test_gateway_down(gw: GatewayCheck) -> None:
    r = checks.gateway_health(gw, fake({"gateway": (0, GW_DOWN), "cron": (0, CRON_DOWN)}))
    assert r.severity == "failed"
    (f,) = r.findings
    assert f.key == "gateway" and f.alert
    assert "cron jobs will NOT fire" in f.message


def test_gateway_nonzero_exit_or_timeout(gw: GatewayCheck) -> None:
    r = checks.gateway_health(gw, fake({"gateway": (124, "timed out"), "cron": (0, CRON_OK)}))
    assert r.severity == "failed"
    assert "exited 124" in r.findings[0].message


def test_gateway_degraded_not_alerted_by_default(gw: GatewayCheck) -> None:
    r = checks.gateway_health(gw, fake({"gateway": (0, GW_WARN), "cron": (0, CRON_OK)}))
    assert r.severity == "degraded"
    (f,) = r.findings
    assert f.key == "gateway_degraded" and not f.alert
    loud = gw.model_copy(update={"alert_on_degraded": True})
    r = checks.gateway_health(loud, fake({"gateway": (0, GW_WARN), "cron": (0, CRON_OK)}))
    assert r.findings[0].alert


def test_gateway_hermes_missing(tmp_path: Path) -> None:
    r = checks.gateway_health(GatewayCheck(hermes_bin=str(tmp_path / "nope")))
    assert r.severity == "failed"
    assert "not found" in r.findings[0].message


def test_resolve_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert checks.resolve_hermes("hermes-xyz") is None
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "hermes-xyz").write_text("")
    assert checks.resolve_hermes("hermes-xyz") == str(local / "hermes-xyz")


def test_run_command_never_raises(tmp_path: Path) -> None:
    code, out = checks.run_command([str(tmp_path / "missing")], 1)
    assert code == 127
    code, out = checks.run_command([sys.executable, "-c", "print('hi')"], 10)
    assert (code, out.strip()) == (0, "hi")
    code, out = checks.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)
    assert code == 124


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def _cond(key: str = "tick_stale", name: str = "tick") -> checks.CheckResult:
    f = checks.Finding(key=key, kind=key, severity="failed", message=f"{key} broke")
    return checks.CheckResult(name, "failed", "x", (f,))


def test_condition_alert_posts_once_then_resolves(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    now = et(2026, 9, 28, 10, 0)
    corr = {"tick_id": "tick-1", "check_id": "health-1"}
    out = alerts.apply(conn, [_cond()], now=now, correlation=corr, notifier=n)
    assert [a.key for a in out.opened] == ["tick_stale"]
    assert len(n.posts) == 1 and "tick_stale broke" in n.posts[0]
    assert "tick_id=tick-1" in n.posts[0]
    assert AlertRepo(conn).open_for("tick_stale").posted_ts == "ts-1"  # type: ignore[union-attr]
    # Still failing: no repeat post.
    out = alerts.apply(conn, [_cond()], now=now, correlation=corr, notifier=n)
    assert not out.opened and len(out.still_open) == 1 and len(n.posts) == 1
    # Passing again: resolved + one ✅ post.
    ok = checks.CheckResult("tick", "ok", "fine")
    out = alerts.apply(conn, [ok], now=now, correlation=corr, notifier=n)
    assert [a.key for a in out.resolved] == ["tick_stale"]
    assert "resolved" in n.posts[1]
    # Nothing to say: no post.
    alerts.apply(conn, [ok], now=now, correlation=corr, notifier=n)
    assert len(n.posts) == 2


def test_one_off_alert_posted_once(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    f = checks.Finding(
        key="missed:director:x", kind="missed_window", severity="failed", message="missed",
        mode=checks.ONE_OFF,
    )  # fmt: skip
    res = [checks.CheckResult("routine_windows", "failed", "", (f,))]
    now = et(2026, 9, 28, 12, 0)
    alerts.apply(conn, res, now=now, correlation={}, notifier=n)
    alerts.apply(conn, res, now=now, correlation={}, notifier=n)
    assert len(n.posts) == 1
    assert AlertRepo(conn).open_alerts() == []


def test_unchecked_conditions_are_not_resolved(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    now = et(2026, 9, 28, 12, 0)
    alerts.apply(
        conn,
        [_cond("gateway", "gateway"), _cond("stuck:run-1", "stuck_runs"), _cond()],
        now=now,
        correlation={},
        notifier=n,
    )
    # A run without the gateway / tick / stuck checks leaves those alerts open.
    alerts.apply(conn, [], now=now, correlation={}, notifier=n)
    assert {a.key for a in AlertRepo(conn).open_alerts()} == {
        "gateway",
        "stuck:run-1",
        "tick_stale",
    }
    out = alerts.apply(
        conn, [checks.CheckResult("stuck_runs", "ok", "")], now=now, correlation={}, notifier=n
    )
    assert [a.key for a in out.resolved] == ["stuck:run-1"]


def test_non_alerting_findings_are_silent(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    f = checks.Finding(key="gateway_degraded", kind="gateway_degraded", severity="degraded",
                       message="warn", alert=False)  # fmt: skip
    alerts.apply(conn, [checks.CheckResult("gateway", "degraded", "", (f,))],
                 now=et(2026, 9, 28), correlation={}, notifier=n)  # fmt: skip
    assert n.posts == []


# ---------------------------------------------------------------------------
# alerts: outages fold missed windows into the incident (review round 1)
# ---------------------------------------------------------------------------


def _health(
    conn: sqlite3.Connection, routines: RoutinesConfig, now: dt.datetime, n: Any
) -> alerts.AlertOutcome:
    results = [
        checks.tick_staleness(conn, MS, now),
        checks.missed_windows(conn, routines, MS, now),
        checks.stuck_runs(conn, MS, now),
    ]
    return alerts.apply(conn, results, now=now, correlation={"check_id": "h"}, notifier=n)


def _missed(job: str, slot: dt.datetime, deadline: dt.datetime | None = None) -> checks.Finding:
    detail = {"job": job, "slot": slot.isoformat()}
    if deadline is not None:
        detail["deadline"] = deadline.isoformat()
    return checks.Finding(
        key=f"missed:{job}:{slot.isoformat()}", kind="missed_window", severity="failed",
        message=f"{job} {slot:%H:%M} missed its window", mode=checks.ONE_OFF, detail=detail,
    )  # fmt: skip


def test_outage_posts_are_bounded(conn: sqlite3.Connection) -> None:
    routines = cfg()
    d = _dispatcher(conn, routines)
    n = alerts.RecordingOpsNotifier()
    hb = HeartbeatRepo(conn)
    # Healthy ticks + checks every 5 min 06:00-08:00, then the tick cron dies until 14:00.
    t = et(2026, 9, 28, 6, 0)
    while t <= et(2026, 9, 28, 15, 0):
        if not (et(2026, 9, 28, 8, 0) < t < et(2026, 9, 28, 14, 0)):
            d.tick(t)
            hb.record("tick", "ok", at=t, correlation={"tick_id": f"tick-{t:%H%M}"})
        _health(conn, routines, t, n)
        t += dt.timedelta(minutes=5)
    # One open post, one resolve summary: nothing else for the whole outage.
    assert len(n.posts) == 2, n.posts
    assert n.replies == []
    opened, resolved = n.posts
    assert "last routines tick" in opened and "missed its window" not in opened
    assert "resolved: routines tick heartbeat is fresh again" in resolved
    assert "routine slot(s) missed: rss ×" in resolved and "director ×1" in resolved
    # Every missed slot is still on record, folded into the incident and traceable.
    repo = AlertRepo(conn)
    incident = repo.find("tick_stale")[0]
    folded = repo.folded_into(incident.id)
    assert len(folded) >= 10
    assert {alerts.missed_job(a) for a in folded} == {"rss", "director"}
    assert all(a.posted_ts == "ts-2" for a in folded)


def test_first_check_after_long_outage_posts_once(conn: sqlite3.Connection) -> None:
    # Reviewer repro: one tick 08:00, the first check only at 11:00 (health agent was down too).
    routines = cfg()
    _dispatcher(conn, routines).tick(et(2026, 9, 29, 8, 0))
    HeartbeatRepo(conn).record("tick", "ok", at=et(2026, 9, 29, 8, 0))
    n = alerts.RecordingOpsNotifier()
    out = _health(conn, routines, et(2026, 9, 29, 11, 0), n)
    assert [a.key for a in out.opened] == ["tick_stale"]
    assert len(out.folded) >= 4 and len(n.posts) == 1
    assert "missed its window" not in n.posts[0]
    # A day later: still nothing new posted.
    for h in range(12, 24):
        _health(conn, routines, et(2026, 9, 29, h, 0), n)
    assert len(n.posts) == 1


def test_single_miss_with_healthy_ticks_alerts_once(conn: sqlite3.Connection) -> None:
    # Ticks are healthy but director never runs (e.g. dropped from the tick's config).
    rss_only = cfg(
        """
        sources:
          rss: {every: 30m, window: "06:00-20:00", days: trading}
        """
    )
    d = _dispatcher(conn, rss_only)
    n = alerts.RecordingOpsNotifier()
    t = et(2026, 9, 28, 6, 0)
    while t <= et(2026, 9, 28, 13, 0):
        d.tick(t)
        HeartbeatRepo(conn).record("tick", "ok", at=t)
        _health(conn, cfg(), t, n)
        t += dt.timedelta(minutes=5)
    assert len(n.posts) == 1
    assert "director" in n.posts[0] and "missed its window" in n.posts[0]
    assert AlertRepo(conn).open_alerts() == []


def test_missed_slots_collapse_to_one_line_per_job(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    slots = [et(2026, 9, 28, h, 0) for h in (9, 10, 11)]
    fs = (*[_missed("rss", s) for s in slots], _missed("director", slots[0]))
    alerts.apply(conn, [checks.CheckResult("routine_windows", "failed", "", fs)],
                 now=et(2026, 9, 28, 12, 0), correlation={}, notifier=n)  # fmt: skip
    (post,) = n.posts
    lines = [x for x in post.splitlines() if x.startswith(":rotating_light:")]
    assert len(lines) == 2
    rss = next(x for x in lines if "rss:" in x)
    assert "3 slots missed" in rss and "09:00" in rss and "11:00" in rss
    assert any("director 09:00 missed its window" in x for x in lines)


def test_miss_judged_after_incident_resolved_replies_in_thread(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    t0 = et(2026, 9, 28, 10, 0)
    alerts.apply(conn, [_cond()], now=t0, correlation={}, notifier=n)
    alerts.apply(conn, [checks.CheckResult("tick", "ok", "ok")], now=t0 + dt.timedelta(hours=1),
                 correlation={}, notifier=n)  # fmt: skip
    assert len(n.posts) == 2
    # Window closed during the outage, grace ran out after it resolved: reply, not a root post.
    late = _missed("director", et(2026, 9, 28, 9, 30), deadline=t0 + dt.timedelta(minutes=55))
    later = t0 + dt.timedelta(hours=1, minutes=10)
    out = alerts.apply(conn, [checks.CheckResult("routine_windows", "failed", "", (late,))],
                       now=later, correlation={}, notifier=n)  # fmt: skip
    assert len(n.posts) == 2
    assert n.replies == [("ts-1", out.replies[0][1])]
    assert "1 routine slot(s) missed: director ×1" in n.replies[0][1]
    assert AlertRepo(conn).find(late.key)[0].posted_ts == "ts-1"
    # A miss whose window closed well after the incident is a normal alert again.
    fresh = _missed("director", et(2026, 9, 29, 9, 30), deadline=et(2026, 9, 29, 11, 30))
    alerts.apply(conn, [checks.CheckResult("routine_windows", "failed", "", (fresh,))],
                 now=et(2026, 9, 29, 11, 45), correlation={}, notifier=n)  # fmt: skip
    assert len(n.posts) == 3 and "director" in n.posts[2]


def test_miss_after_unposted_incident_is_posted(conn: sqlite3.Connection) -> None:
    quiet = alerts.LogOpsNotifier()  # Slack down: incident never gets a ts
    t0 = et(2026, 9, 28, 10, 0)
    alerts.apply(conn, [_cond()], now=t0, correlation={}, notifier=quiet)
    alerts.apply(conn, [checks.CheckResult("tick", "ok", "ok")], now=t0 + dt.timedelta(hours=1),
                 correlation={}, notifier=quiet)  # fmt: skip
    n = alerts.RecordingOpsNotifier()
    late = _missed("rss", t0, deadline=t0 + dt.timedelta(minutes=30))
    out = alerts.apply(conn, [checks.CheckResult("routine_windows", "failed", "", (late,))],
                       now=t0 + dt.timedelta(hours=2), correlation={}, notifier=n)  # fmt: skip
    assert not out.folded and len(n.posts) == 1 and "rss" in n.posts[0]


def test_gateway_down_absorbs_misses(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    t0 = et(2026, 9, 28, 10, 0)
    miss = _missed("rss", t0, deadline=t0 + dt.timedelta(minutes=30))
    res = [_cond("gateway", "gateway"),
           checks.CheckResult("routine_windows", "failed", "", (miss,))]  # fmt: skip
    out = alerts.apply(conn, res, now=t0 + dt.timedelta(minutes=45), correlation={}, notifier=n)
    assert [a.key for a in out.opened] == ["gateway"] and len(out.folded) == 1
    assert "rss" not in n.posts[0]
    gw_ok = checks.CheckResult("gateway", "ok", "Gateway is running (PID 7)")
    alerts.apply(conn, [gw_ok], now=t0 + dt.timedelta(hours=1), correlation={}, notifier=n)
    assert "resolved: Hermes gateway healthy again (Gateway is running (PID 7))" in n.posts[1]
    assert "1 routine slot(s) missed: rss ×1" in n.posts[1]


def test_resolve_text_reflects_current_state(conn: sqlite3.Connection) -> None:
    n = alerts.RecordingOpsNotifier()
    now = et(2026, 9, 28, 9, 0)
    _health(conn, cfg(), now, n)  # no tick heartbeat yet
    assert "no `arc routines tick` heartbeat recorded yet" in n.posts[0]
    HeartbeatRepo(conn).record("tick", "ok", at=now, correlation={"tick_id": "tick-new"})
    _health(conn, cfg(), now + dt.timedelta(minutes=5), n)
    assert "recorded yet" not in n.posts[1]
    assert "routines tick heartbeat is fresh again" in n.posts[1] and "tick-new" in n.posts[1]
    stuck = checks.Finding(key="stuck:run-9", kind="stuck_run", severity="failed", message="m")
    alerts.apply(conn, [checks.CheckResult("stuck_runs", "failed", "", (stuck,))], now=now,
                 correlation={}, notifier=n)  # fmt: skip
    alerts.apply(conn, [checks.CheckResult("stuck_runs", "ok", "")], now=now, correlation={},
                 notifier=n)  # fmt: skip
    assert "resolved: run run-9 is no longer stuck" in n.posts[-1]
    other = checks.Finding(key="gateway_degraded", kind="gateway_degraded", severity="degraded",
                           message="warned")  # fmt: skip
    alerts.apply(conn, [checks.CheckResult("gateway", "degraded", "", (other,))], now=now,
                 correlation={}, notifier=n)  # fmt: skip
    alerts.apply(conn, [checks.CheckResult("gateway", "ok", "")], now=now, correlation={},
                 notifier=n)  # fmt: skip
    assert "resolved: warned" in n.posts[-1]


class FakeWeb:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def chat_postMessage(self, **kw: Any) -> dict[str, Any]:  # noqa: N802 - Slack SDK name
        if self.fail:
            raise RuntimeError("slack down")
        self.calls.append(kw)
        return {"ts": "111.222"}


def test_slack_ops_notifier_channels_and_failures() -> None:
    from arc.slack.client import CHANNEL_ARC_INVESTOR, CHANNEL_PROJECT_ARC, ArcSlackClient

    web = FakeWeb()
    n = alerts.SlackOpsNotifier(AlertChannel.PROJECT_ARC, ArcSlackClient(web))  # type: ignore[arg-type]
    assert n.post("hello") == "111.222"
    assert web.calls[0]["channel"] == CHANNEL_PROJECT_ARC
    assert n.post("in thread", thread_ts="111.222") == "111.222"
    assert web.calls[1]["thread_ts"] == "111.222" and web.calls[1]["text"] == "in thread"
    assert alerts.LogOpsNotifier().post("x", thread_ts="1.1") is None
    web2 = FakeWeb()
    alerts.SlackOpsNotifier(AlertChannel.ARC_INVESTOR, ArcSlackClient(web2)).post("x")  # type: ignore[arg-type]
    assert web2.calls[0]["channel"] == CHANNEL_ARC_INVESTOR
    bad = alerts.SlackOpsNotifier(AlertChannel.PROJECT_ARC, ArcSlackClient(FakeWeb(fail=True)))  # type: ignore[arg-type]
    assert bad.post("x") is None
    assert alerts.LogOpsNotifier().post("x") is None


# ---------------------------------------------------------------------------
# correlation + logs
# ---------------------------------------------------------------------------


def test_correlation_from_env() -> None:
    env = {"ARC_TICK_ID": "tick-9", "HERMES_KANBAN_TASK": "t_1", "ARC_CRON_JOB": "", "X": "y"}
    assert correlation.from_env(env) == {"tick_id": "tick-9", "kanban_task": "t_1"}
    minted = correlation.tick_correlation({})
    assert minted["tick_id"].startswith("tick-")
    assert correlation.tick_correlation(env)["tick_id"] == "tick-9"


def test_json_log_carries_bound_ids_and_rotates(tmp_path: Path) -> None:
    settings = LogSettings(path=Path("logs/arc.jsonl"), max_bytes=10_000, backups=2)
    path = logs.configure(settings, base_dir=tmp_path)
    assert path == tmp_path / "logs" / "arc.jsonl"
    assert path is not None
    log = structlog.get_logger("t")
    with correlation.bind(tick_id="tick-7", run_id="run-1", chain_run_id=None):
        log.info("hello", n=1)
        log.debug("quiet")
    log.info("after")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines[0]["event"] == "hello"
    assert lines[0]["tick_id"] == "tick-7" and lines[0]["run_id"] == "run-1"
    assert "chain_run_id" not in lines[0]
    assert lines[0]["ts"].endswith(("-04:00", "-05:00"))  # ET
    assert "tick_id" not in lines[1] and len(lines) == 2  # debug not written
    for i in range(200):
        log.info("fill", i=i, pad="x" * 100)
    rotated = sorted(p.name for p in path.parent.iterdir())
    assert rotated == ["arc.jsonl", "arc.jsonl.1", "arc.jsonl.2"]


def test_log_configure_without_file_and_unwritable(tmp_path: Path) -> None:
    assert logs.configure(None, base_dir=None) is None
    blocker = tmp_path / "file"
    blocker.write_text("")
    bad = LogSettings(path=blocker / "sub" / "arc.jsonl")
    assert logs.configure(bad, base_dir=tmp_path) is None
    structlog.get_logger("t").info("still works")


def test_dispatcher_binds_run_ids(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = logs.configure(LogSettings(), base_dir=tmp_path)
    assert path is not None
    seen: list[dict[str, Any]] = []

    def h(ctx: JobContext) -> JobResult:
        seen.append(structlog.contextvars.get_contextvars())
        return JobResult(summary="ok")

    d = Dispatcher(conn, cfg(), handlers={"rss": h, "director": h, "quant": h},
                   notifier=RecordingNotifier(), is_halted=lambda: False)  # fmt: skip
    d.run_manual("director", now=et(2026, 9, 28, 9, 30), chain=True)
    assert seen[0]["job"] == "director" and seen[0]["run_id"].startswith("run-")
    assert seen[0]["chain_run_id"].startswith("chain-") and seen[0]["step_index"] == 0
    assert seen[1]["job"] == "quant" and seen[1]["step_index"] == 1
    assert structlog.contextvars.get_contextvars() == {}


def test_failure_alert_carries_run_id(conn: sqlite3.Connection) -> None:
    def boom(ctx: JobContext) -> JobResult:
        raise RuntimeError("boom")

    n = RecordingNotifier()
    d = Dispatcher(conn, cfg(), handlers={"rss": boom}, notifier=n, is_halted=lambda: False)
    (o,) = d.run_manual("rss", now=et(2026, 9, 28, 9, 30))
    assert o.status == "failed"
    assert any(f"`{o.run_id}`" in text for _, text in n.posts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = tmp_path / "routines.yaml"
    data = yaml.safe_load(textwrap.dedent(YAML))
    data["monitoring"] = {"gateway": {"enabled": False}}
    config.write_text(yaml.safe_dump(data))
    for var in correlation.ENV_KEYS:
        monkeypatch.delenv(var, raising=False)
    return {"db": str(tmp_path / "arc.db"), "config": str(config), "dir": tmp_path}


def test_cli_tick_records_heartbeat_and_health_cycle(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db, config = cli_env["db"], cli_env["config"]
    common = ["--db", db, "--config", config]
    # Before any tick: failing health (tick_stale), exit 1.
    rc = main(["health", "check", *common, "--no-slack", "--now", "2026-09-28T09:00", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1 and report["status"] == "failed"
    assert [a["key"] for a in report["opened"]] == ["tick_stale"]

    monkeypatch.setenv("ARC_TICK_ID", "tick-cli1")
    monkeypatch.setenv("ARC_CRON_JOB", "arc-routines-tick")
    rc = main(["routines", "tick", *common, "--no-slack", "--lock-dir",
               str(cli_env["dir"] / "locks"), "--now", "2026-09-28T09:00", "--json"])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert out["correlation"] == {"tick_id": "tick-cli1", "cron_job": "arc-routines-tick"}
    c = connect(db)
    hb = HeartbeatRepo(c).latest("tick")
    assert hb is not None and hb.correlation["tick_id"] == "tick-cli1"
    assert hb.detail["counts"] == {"ok": 1} or "ok" in hb.detail["counts"]
    run_ids = [o["run_id"] for o in hb.detail["outcomes"]]
    assert run_ids

    # JSON log written next to the DB with the tick id on the run's lines.
    log_lines = [json.loads(x) for x in (cli_env["dir"] / "logs" / "arc.jsonl").read_text()
                 .splitlines()]  # fmt: skip
    assert any(x.get("tick_id") == "tick-cli1" and x.get("run_id") == run_ids[0]
               for x in log_lines)  # fmt: skip

    # Health now resolves the alert.
    monkeypatch.delenv("ARC_TICK_ID")
    rc = main(["health", "check", *common, "--no-slack", "--now", "2026-09-28T09:05"])
    text = capsys.readouterr().out
    assert rc == 0
    assert "resolved" in text

    main(["health", "status", *common])
    status = capsys.readouterr().out
    assert "tick-cli1" in status and "open alerts: 0" in status
    main(["health", "status", *common, "--json"])
    assert json.loads(capsys.readouterr().out)["open_alerts"] == []

    rc = main(["health", "trace", "tick-cli1", *common])
    trace = capsys.readouterr().out
    assert rc == 0
    assert run_ids[0] in trace and "heartbeat" in trace and "log" in trace
    rc = main(["health", "trace", "tick-cli1", *common, "--json"])
    assert json.loads(capsys.readouterr().out)["runs"]
    assert main(["health", "trace", "nothing-here", *common]) == 1
    assert "nothing recorded" in capsys.readouterr().out
    assert main(["health", "trace", "nothing-here", *common, "--json"]) == 1


def test_cli_tick_crash_records_failed_heartbeat(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("dispatcher exploded")

    monkeypatch.setattr(Dispatcher, "tick", boom)
    with pytest.raises(RuntimeError):
        main(["routines", "tick", "--db", cli_env["db"], "--config", cli_env["config"],
              "--no-slack", "--lock-dir", str(cli_env["dir"] / "l")])  # fmt: skip
    hb = HeartbeatRepo(connect(cli_env["db"])).latest("tick")
    assert hb is not None and hb.status == "failed"
    assert "dispatcher exploded" in hb.detail["error"]


def test_cli_dry_run_tick_records_no_heartbeat(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["routines", "tick", "--db", cli_env["db"], "--config", cli_env["config"], "--dry-run",
          "--now", "2026-09-28T09:00"])  # fmt: skip
    capsys.readouterr()
    assert HeartbeatRepo(connect(cli_env["db"])).latest("tick") is None
    assert not (cli_env["dir"] / "logs").exists()


def test_cli_health_check_with_gateway_and_slack(
    cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Path(cli_env["config"])
    data = yaml.safe_load(config.read_text())
    data["monitoring"]["gateway"] = {"enabled": True, "hermes_bin": sys.executable}
    config.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(checks, "run_command", lambda argv, t: (0, GW_DOWN))
    posted: list[str] = []
    monkeypatch.setattr(alerts.SlackOpsNotifier, "__init__", lambda self, ch: None)
    monkeypatch.setattr(
        alerts.SlackOpsNotifier, "post", lambda self, text: posted.append(text) or "9.9"
    )
    # run_checks resolves checks.gateway_health's default runner at def time; patch it.
    orig = checks.gateway_health
    monkeypatch.setattr(checks, "gateway_health", lambda gw: orig(gw, checks.run_command))
    rc = main(["health", "check", "--db", cli_env["db"], "--config", str(config),
               "--now", "2026-09-28T09:00"])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 1
    assert "gateway" in out and "Gateway is not running" in out
    assert len(posted) == 1 and "Gateway is not running" in posted[0]
    health = HeartbeatRepo(connect(cli_env["db"])).latest("health")
    assert health is not None and health.detail["posted_ts"] == "9.9"
    assert health.detail["checks"]["gateway"]["severity"] == "failed"
    # --no-gateway leaves the gateway alert open (not re-checked).
    main(["health", "check", "--db", cli_env["db"], "--config", str(config), "--no-slack",
          "--no-gateway", "--now", "2026-09-28T09:01"])  # fmt: skip
    capsys.readouterr()
    assert AlertRepo(connect(cli_env["db"])).open_for("gateway") is not None


# ---------------------------------------------------------------------------
# Hermes scripts
# ---------------------------------------------------------------------------


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tick_script_passes_tick_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_script(REPO / "hermes" / "routines" / "arc_routines_tick.py", "arc_tick_e82")
    monkeypatch.setattr(mod, "HERMES_ENV", tmp_path / "missing.env")
    env = mod._env("tick-abc")
    assert env["ARC_TICK_ID"] == "tick-abc"
    assert env["ARC_CRON_JOB"] == "arc-routines-tick"


def test_health_script_runs_arc_health_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_script(REPO / "hermes" / "monitoring" / "arc_health_check.py", "arc_health_e82")
    (tmp_path / ".env").write_text(
        "SLACK_BOT_TOKEN='xoxb-test'\nARC_FOO=1\nOTHER_SECRET=nope\n# c\n"
    )
    monkeypatch.setattr(mod, "HERMES_ENV", tmp_path / ".env")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OTHER_SECRET", raising=False)
    env = mod._env()
    assert env["SLACK_BOT_TOKEN"] == "xoxb-test" and env["ARC_FOO"] == "1"
    assert "OTHER_SECRET" not in env
    assert env["ARC_CRON_JOB"] == "arc-health-check"

    monkeypatch.setattr(mod, "LOG", tmp_path / "logs" / "health-check.log")
    monkeypatch.setattr(mod, "ARC", tmp_path / "no-arc")
    assert mod.main() == 2
    fake_arc = tmp_path / "arc"
    fake_arc.write_text('#!/bin/sh\necho "health: $1 $2"\nexit 1\n')
    fake_arc.chmod(0o755)
    monkeypatch.setattr(mod, "ARC", fake_arc)
    monkeypatch.setattr(mod, "REPO", tmp_path)
    assert mod.main() == 1
    assert "health: health check" in (tmp_path / "logs" / "health-check.log").read_text()
    # Rotation.
    monkeypatch.setattr(mod, "LOG_MAX_BYTES", 10)
    mod._log("more")
    mod._log("more")
    assert (tmp_path / "logs" / "health-check.log.1").exists()


def test_install_script_prints_plist() -> None:
    import subprocess

    out = subprocess.run(  # noqa: S603
        ["bash", str(REPO / "hermes" / "monitoring" / "install.sh"), "--print", str(REPO)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "com.projectarc.health-check" in out
    assert "<integer>300</integer>" in out
    assert "arc_health_check.py" in out
