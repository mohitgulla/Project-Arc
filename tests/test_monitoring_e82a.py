"""E8.2a: slot-coverage + slow-tick conditions instead of one alert per missed slot."""

from __future__ import annotations

import argparse
import datetime as dt
import textwrap
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from arc.config import ArcSettings
from arc.context.ttl import to_db
from arc.control.effective import effective_routines
from arc.control.registry import REGISTRY, Target
from arc.control.service import ControlService
from arc.monitoring import alerts, checks
from arc.monitoring.cli import run_checks
from arc.monitoring.config import MonitoringSettings
from arc.monitoring.store import AlertRepo, HeartbeatRepo
from arc.routines.config import RoutinesConfig, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

OWNER = "U0OWNER001"
DAY = (2026, 9, 28)  # a Monday session

YAML = """
    sources:
      edgar: {every: 15m, window: "06:00-09:00", days: trading}
    personas:
      research: {every: 5m, window: "09:40-15:50", days: trading, ttl: 5m}
      broker.reconcile: {schedule: ["16:30"], days: trading, ttl: 6h}
    monitoring:
      gateway: {enabled: false}
"""


def et(h: int, m: int = 0, s: int = 0, day: tuple[int, int, int] = DAY) -> dt.datetime:
    return dt.datetime(*day, h, m, s, tzinfo=ET)


def cfg(**monitoring: Any) -> RoutinesConfig:
    data = yaml.safe_load(textwrap.dedent(YAML))
    data["monitoring"].update(monitoring)
    return RoutinesConfig.model_validate(data)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def run(conn: sqlite3.Connection, job: str, slot: dt.datetime, status: str = "ok",
        summary: str = "") -> None:  # fmt: skip
    conn.execute(
        """INSERT INTO routine_runs (run_id, job, reason, scheduled_for, status, summary)
           VALUES (?, ?, 'schedule', ?, ?, ?)""",
        (f"run-{uuid.uuid4().hex[:12]}", job, to_db(slot), status, summary),
    )
    conn.commit()


def ticks(conn: sqlite3.Connection, start: dt.datetime, end: dt.datetime, *,
          step: int = 5, ms: int = 30_000,
          slowest: list[dict[str, Any]] | None = None) -> None:  # fmt: skip
    hb = HeartbeatRepo(conn)
    t = start
    while t <= end:
        hb.record("tick", "ok", at=t, detail={"tick_duration_ms": ms,
                                              "slowest_jobs": slowest or []})  # fmt: skip
        t += dt.timedelta(minutes=step)


def research_slots(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    out, t = [], start
    while t <= end:
        out.append(t)
        t += dt.timedelta(minutes=5)
    return out


def fill_research(conn: sqlite3.Connection, start: dt.datetime, end: dt.datetime,
                  skip: set[dt.datetime] = frozenset()) -> None:  # type: ignore[assignment]  # fmt: skip
    for s in research_slots(start, end):
        if s not in skip:
            run(conn, "research", s)


def health(conn: sqlite3.Connection, routines: RoutinesConfig, now: dt.datetime,
           n: alerts.RecordingOpsNotifier) -> alerts.AlertOutcome:  # fmt: skip
    ms = routines.monitoring
    results = [
        checks.tick_staleness(conn, ms, now),
        checks.tick_slow(conn, routines, ms, now),
        checks.missed_windows(conn, routines, ms, now),
        checks.slot_coverage(conn, routines, ms, now),
    ]
    return alerts.apply(conn, results, now=now, correlation={"check_id": "h"}, notifier=n)


# 11:00 judges research slots 09:50..10:45 (judge time = slot + 5m ttl + 10m grace).
NOW = et(11, 0)
# Two 15-min tick gaps. Judging is collapse aware (as `missed_windows`): the last
# slot of each gap counts as run because the next slot ran inside its window, so
# 6 slots without a row are 4 misses: 8/12.
MISSED = {et(10, 0), et(10, 5), et(10, 10), et(10, 30), et(10, 35), et(10, 40)}


def _setup_four_of_twelve_missed(conn: sqlite3.Connection, *, ms: int = 30_000,
                                 slowest: list[dict[str, Any]] | None = None) -> None:  # fmt: skip
    ticks(conn, et(9, 0), NOW, ms=ms, slowest=slowest)
    fill_research(conn, et(9, 40), et(10, 55), skip=MISSED)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_defaults_and_shipped_config() -> None:
    ms = MonitoringSettings()
    assert ms.per_slot_min_interval == dt.timedelta(minutes=60)
    assert ms.coverage_window == dt.timedelta(minutes=60)
    assert ms.coverage_min == 0.8
    assert ms.tick_slow_count == 2 and ms.tick_slow_after == dt.timedelta(minutes=4)
    assert load_routines().monitoring.model_dump() == {
        **load_routines().monitoring.model_dump(),
        "per_slot_min_interval": dt.timedelta(minutes=60),
        "coverage_window": dt.timedelta(minutes=60),
        "coverage_min": 0.8,
        "tick_slow_count": 2,
        "tick_slow_after": dt.timedelta(minutes=4),
    }
    with pytest.raises(ValueError, match="coverage_min"):
        MonitoringSettings.model_validate({"coverage_min": 1.5})


def test_slot_interval_splits_by_cadence() -> None:
    r, ms = cfg(), MonitoringSettings()
    assert checks.slot_interval(r.personas["research"]) == dt.timedelta(minutes=5)
    assert checks.slot_interval(r.personas["broker.reconcile"]) == dt.timedelta(days=1)
    two = RoutinesConfig.model_validate(
        {"sources": {"e": {"schedule": ["06:00", "18:00"]}}}
    ).sources["e"]
    assert checks.slot_interval(two) == dt.timedelta(hours=12)
    assert not checks.per_slot(r.personas["research"], ms)
    assert not checks.per_slot(r.sources["edgar"], ms)
    assert checks.per_slot(r.personas["broker.reconcile"], ms) and checks.per_slot(two, ms)


# ---------------------------------------------------------------------------
# coverage condition
# ---------------------------------------------------------------------------


def test_five_min_job_missing_4_of_12_is_one_coverage_alert(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn)
    n = alerts.RecordingOpsNotifier()
    out = health(conn, cfg(), NOW, n)
    assert [a.key for a in out.opened] == ["coverage:research"]
    assert not [a for a in out.opened if a.kind == "missed_window"]
    (post,) = n.posts
    assert "research ran 8/12 slots in the last 60 min (67%)" in post
    assert "likely cause: ticks on time" in post
    assert "missed its window" not in post
    # Still failing five minutes later: no repeat post, the alert stays open.
    fill_research(conn, et(11, 0), et(11, 0))
    health(conn, cfg(), NOW + dt.timedelta(minutes=5), n)
    assert len(n.posts) == 1
    assert AlertRepo(conn).open_for("coverage:research") is not None
    # The per-slot path is untouched for research: nothing recorded per slot.
    assert not AlertRepo(conn).find("missed:research")


def test_recovery_posts_one_resolve_with_ratio(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn)
    n = alerts.RecordingOpsNotifier()
    health(conn, cfg(), NOW, n)
    # The next hour runs every slot: 12:15 judges 11:05..12:00 only.
    ticks(conn, NOW + dt.timedelta(minutes=5), et(12, 15))
    fill_research(conn, et(11, 0), et(12, 10))
    out = health(conn, cfg(), et(12, 15), n)
    assert [a.key for a in out.resolved] == ["coverage:research"]
    assert len(n.posts) == 2
    assert (
        "resolved: research slot coverage recovered: ran 12/12 slots in the last 60 min (100%)"
        in n.posts[1]
    )
    health(conn, cfg(), et(12, 20), n)
    assert len(n.posts) == 2


def test_resolve_without_judged_slots(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn)
    n = alerts.RecordingOpsNotifier()
    health(conn, cfg(), NOW, n)
    # After the session: no research slot judged in the window -> passes, generic text.
    ticks(conn, et(16, 0), et(17, 30))
    health(conn, cfg(), et(17, 30), n)
    assert "research slot coverage recovered (no slots judged in the window)" in n.posts[-1]


def test_daily_job_miss_is_still_one_per_slot_alert(conn: sqlite3.Connection) -> None:
    ticks(conn, et(16, 0), et(22, 45))
    n = alerts.RecordingOpsNotifier()
    out = health(conn, cfg(), et(22, 45), n)  # broker.reconcile 16:30 + 6h ttl + 10m grace passed
    missed = [a for a in out.opened if a.kind == "missed_window"]
    assert [alerts.missed_job(a) for a in missed] == ["broker.reconcile"]
    assert (
        len(n.posts) == 1 and "broker.reconcile Mon 09-28 16:30 EDT missed its window" in n.posts[0]
    )
    health(conn, cfg(), et(22, 50), n)
    assert len(n.posts) == 1


def test_halted_slots_are_excluded(conn: sqlite3.Connection) -> None:
    ticks(conn, et(9, 0), NOW)
    for s in research_slots(et(9, 40), et(10, 55)):
        if s < et(10, 20):
            run(conn, "research", s, "skipped", "halted (persona)")
        else:
            run(conn, "research", s)
    n = alerts.RecordingOpsNotifier()
    r = checks.slot_coverage(conn, cfg(), MonitoringSettings(), NOW)
    assert r.severity == "ok"
    assert r.detail["jobs"]["research"] == {"ran": 6, "judged": 6, "halted": 6}
    # Per-slot judging agrees: halted is never a miss (shrink the threshold to judge it).
    per = checks.missed_windows(
        conn, cfg(), MonitoringSettings(per_slot_min_interval=dt.timedelta(minutes=5)), NOW
    )
    assert not [f for f in per.findings if f.detail["job"] == "research"]
    # A collapsed slot whose only later row is a halt skip is halted too.
    conn.execute("DELETE FROM routine_runs WHERE scheduled_for = ?", (to_db(et(9, 50)),))
    assert checks._slot_attempted(conn, "research", et(9, 50), et(9, 55)) == checks.HALTED
    assert health(conn, cfg(), NOW, n).opened == []


def test_coverage_not_judged_before_first_tick(conn: sqlite3.Connection) -> None:
    r = checks.slot_coverage(conn, cfg(), MonitoringSettings(), NOW)
    assert r.severity == "ok" and "not judged" in r.summary


def test_coverage_cause_falls_back_to_tick_gaps(conn: sqlite3.Connection) -> None:
    # Old heartbeats (no tick_duration_ms): the cause is read from the spacing.
    hb = HeartbeatRepo(conn)
    for t in (et(9, 0), et(10, 0), et(10, 3), et(10, 40), et(10, 58)):
        hb.record("tick", "ok", at=t)
    fill_research(conn, et(9, 40), et(10, 55), skip=MISSED)
    r = checks.slot_coverage(conn, cfg(), MonitoringSettings(), NOW)
    (f,) = r.findings
    assert f.message.endswith("likely cause: tick gaps up to 37 min")
    stats = checks.tick_stats(hb.between("tick", et(10, 0), NOW))
    assert stats.ticks == 3 and stats.max_duration is None
    assert checks.likely_cause(checks.tick_stats([]), cfg(), MonitoringSettings()) == (
        "no tick ran in the window"
    )
    # Old heartbeats on schedule: no blame on the tick.
    c2 = connect(":memory:")
    migrate(c2)
    for t in (et(10, 0), et(10, 6), et(10, 11)):
        HeartbeatRepo(c2).record("tick", "ok", at=t)
    on_time = checks.tick_stats(HeartbeatRepo(c2).between("tick", et(9, 0), NOW))
    assert checks.likely_cause(on_time, cfg(), MonitoringSettings()).startswith(
        "ticks on time (gaps up to 6 min)"
    )
    one = checks.tick_stats(HeartbeatRepo(c2).between("tick", et(10, 5), NOW))
    assert "ticks on time (gaps up to 5 min)" in checks.likely_cause(
        one, cfg(), MonitoringSettings()
    )
    single = checks.tick_stats(HeartbeatRepo(c2).between("tick", et(10, 10), NOW))
    assert "ticks on time (1 tick)" in checks.likely_cause(single, cfg(), MonitoringSettings())


# ---------------------------------------------------------------------------
# tick_slow + folding
# ---------------------------------------------------------------------------

SLOW = [{"job": "scalp", "ms": 340_000}, {"job": "edgar", "ms": 20_000}]


def test_tick_slow_check() -> None:
    c = connect(":memory:")
    migrate(c)
    ms, r = MonitoringSettings(), cfg()
    ticks(c, et(10, 0), et(10, 30))
    ok = checks.tick_slow(c, r, ms, et(10, 30))
    assert ok.severity == "ok" and ok.summary.startswith("7 tick(s) in the last 60 min")
    HeartbeatRepo(c).record("tick", "ok", at=et(10, 35),
                            detail={"tick_duration_ms": 483_000, "slowest_jobs": SLOW})  # fmt: skip
    assert checks.tick_slow(c, r, ms, et(10, 36)).severity == "ok"  # one slow tick only
    HeartbeatRepo(c).record("tick", "ok", at=et(10, 40),
                            detail={"tick_duration_ms": 250_000, "slowest_jobs": SLOW})  # fmt: skip
    res = checks.tick_slow(c, r, ms, et(10, 41))
    (f,) = res.findings
    assert "spacing" not in f.message.split("(")[0]  # durations alone tripped it
    assert f.key == "tick_slow"
    assert "2 of 9 ticks in the last 60 min took > 4m00s" in f.message
    assert "max 8m03s" in f.message and "top job scalp 5m40s" in f.message
    # Spacing alone: p90 of the gaps > 1.5 x the 5-min interval.
    c2 = connect(":memory:")
    migrate(c2)
    ticks(c2, et(10, 0), et(11, 0), step=10)
    res = checks.tick_slow(c2, r, ms, et(11, 0))
    assert res.severity == "failed" and "tick spacing p90 10m00s > 7m30s" in res.findings[0].message
    assert checks.fmt_duration(dt.timedelta(seconds=-3)) == "0m00s"


def test_tick_slow_open_folds_coverage(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn, ms=330_000, slowest=SLOW)
    n = alerts.RecordingOpsNotifier()
    out = health(conn, cfg(), NOW, n)
    assert [a.key for a in out.opened] == ["tick_slow"]
    assert [a.key for a in out.folded] == ["coverage:research"]
    (post,) = n.posts
    assert "routines ticks are slow" in post and "coverage" not in post
    folded = AlertRepo(conn).open_for("coverage:research")
    assert folded is not None and folded.correlation[alerts.FOLDED_INTO] == out.opened[0].id
    assert "likely cause: slow ticks (max 5m30s, scalp 5m40s)" in folded.message
    # Both still failing: nothing new.
    health(conn, cfg(), NOW + dt.timedelta(minutes=1), n)
    assert len(n.posts) == 1
    # The hour after: fast ticks, every slot ran -> both clear, ONE post naming the fold.
    ticks(conn, NOW + dt.timedelta(minutes=5), et(12, 15))
    fill_research(conn, et(11, 0), et(12, 10))
    out = health(conn, cfg(), et(12, 15), n)
    assert [a.key for a in out.resolved] == ["tick_slow"]
    assert len(n.posts) == 2
    assert "resolved: routines ticks back within limits" in n.posts[1]
    assert "low slot coverage: research" in n.posts[1]
    assert AlertRepo(conn).open_for("coverage:research") is None


def test_coverage_outliving_its_incident_is_posted(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn, ms=330_000, slowest=SLOW)
    n = alerts.RecordingOpsNotifier()
    health(conn, cfg(), NOW, n)
    # Ticks fast again but research keeps missing: tick_slow resolves, coverage posts.
    ticks(conn, NOW + dt.timedelta(minutes=5), et(12, 15))
    out = health(conn, cfg(), et(12, 15), n)
    assert [a.key for a in out.resolved] == ["tick_slow"]
    assert [a.key for a in out.opened] == ["coverage:research"]
    assert len(n.posts) == 2
    assert "research ran 0/12 slots" in n.posts[1] and "ticks on time" in n.posts[1]
    rec = AlertRepo(conn).open_for("coverage:research")
    assert rec is not None and alerts.FOLDED_INTO not in rec.correlation
    assert rec.correlation["unfolded_from"]
    # Its later recovery is a normal resolve line.
    fill_research(conn, et(12, 15), et(13, 15))
    ticks(conn, et(12, 20), et(13, 30))
    health(conn, cfg(), et(13, 30), n)
    assert "research slot coverage recovered" in n.posts[-1]


def test_tick_stale_also_folds_coverage(conn: sqlite3.Connection) -> None:
    ticks(conn, et(9, 0), et(10, 10))
    fill_research(conn, et(9, 40), et(10, 5))
    n = alerts.RecordingOpsNotifier()
    out = health(conn, cfg(), NOW, n)  # last tick 50 min ago
    assert [a.key for a in out.opened] == ["tick_stale"]
    assert [a.key for a in out.folded] == ["coverage:research"]
    assert len(n.posts) == 1


def test_unchecked_coverage_and_tick_slow_stay_open(conn: sqlite3.Connection) -> None:
    _setup_four_of_twelve_missed(conn, ms=330_000, slowest=SLOW)
    n = alerts.RecordingOpsNotifier()
    health(conn, cfg(), NOW, n)
    alerts.apply(conn, [], now=et(12, 0), correlation={}, notifier=n)
    assert {a.key for a in AlertRepo(conn).open_alerts()} == {"tick_slow", "coverage:research"}


def test_trace_reads_both_alert_kinds(conn: sqlite3.Connection, tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from arc.cli import main

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    repo = AlertRepo(c)
    old = repo.open("missed:research:2026-09-28T14:00:00.000000Z", "missed_window", "old miss",
                    at=et(10, 15), resolved=True)  # fmt: skip
    new = repo.open("coverage:research", "coverage", "research ran 7/12", at=et(11, 0))
    c.close()
    for alert in (old, new):
        assert main(["health", "trace", alert.id, "--db", str(db)]) == 0
        assert alert.id in capsys.readouterr().out


# ---------------------------------------------------------------------------
# rollup line (Auditor journal card)
# ---------------------------------------------------------------------------


def test_slot_rollup_line(conn: sqlite3.Connection) -> None:
    # Three-slot gaps: the last slot of each gap is collapsed into the next run.
    fill_research(conn, et(9, 40), et(15, 50), skip={et(10, 0), et(10, 5), et(10, 10)})
    gap = {et(7, 0), et(7, 15), et(7, 30)}
    for s in [et(6, 0) + dt.timedelta(minutes=15 * i) for i in range(13)]:
        if s not in gap:
            run(conn, "edgar", s)
    covs = checks.slot_rollup(conn, cfg(), et(16, 30))  # broker.reconcile 16:30 not closed yet
    by = {c.job: c for c in covs}
    assert set(by) == {"research", "edgar"}
    assert by["research"].text == "73/75" and by["edgar"].text == "11/13"
    line = checks.rollup_line(covs, cfg())
    assert line == "Slots: research 73/75, edgar 11/13 · missed 4 (list in tower Ops)"
    assert checks.rollup_line([], cfg()) is None
    clean = [checks.Coverage("research", 75, 75)]
    assert checks.rollup_line(clean, cfg()) == "Slots: research 75/75 · missed 0"
    only_src = [checks.Coverage("edgar", 4, 4)]
    assert checks.rollup_line(only_src, cfg()) == "Slots: missed 0"


def test_auditor_card_carries_the_slots_line(conn: sqlite3.Connection) -> None:
    from arc.broker import reconcile_job as aud

    fill_research(conn, et(9, 40), et(15, 50), skip={et(10, 0)})

    class Ctx:
        def __init__(self) -> None:
            self.conn, self.routines, self.now = conn, cfg(), et(16, 30)

    line = aud._slots_line(Ctx())  # type: ignore[arg-type]
    assert line is not None and line.startswith("Slots: research 75/75")

    class Broken(Ctx):
        @property
        def routines(self) -> RoutinesConfig:  # type: ignore[override]
            raise RuntimeError("no config")

        @routines.setter
        def routines(self, _v: object) -> None:
            pass

    assert aud._slots_line(Broken()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# D26: every knob is tunable and changes the check result
# ---------------------------------------------------------------------------

KNOBS = (
    "monitoring.per_slot_min_interval",
    "monitoring.coverage_window",
    "monitoring.coverage_min",
    "monitoring.tick_slow_count",
    "monitoring.tick_slow_after",
    "monitoring.earnings_stale_after",
)


def test_every_new_knob_is_registered() -> None:
    fields = {"per_slot_min_interval", "coverage_window", "coverage_min", "tick_slow_count",
              "tick_slow_after", "earnings_stale_after"}  # fmt: skip
    assert {k.removeprefix("monitoring.") for k in KNOBS} == fields
    for k in KNOBS:
        t = REGISTRY[k]
        assert t.target is Target.ROUTINES and t.path == ("monitoring", k.split(".")[1])


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "routines.yaml"
    p.write_text(textwrap.dedent(YAML))
    return p


def _svc(conn: sqlite3.Connection) -> ControlService:
    base = ArcSettings(_env_file=None, approver_slack_user_ids=[OWNER])  # type: ignore[call-arg]
    return ControlService(conn, base=base, now=lambda: NOW, optionable=lambda s: True,
                          is_halted=lambda: False)  # fmt: skip


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    svc = _svc(conn)
    r = svc.set(key, value, actor=OWNER, source="slack")
    if r.pending is not None:
        r = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert r.outcome == "applied", r


def _results(conn: sqlite3.Connection, config: Path, now: dt.datetime) -> dict[str, Any]:
    args = argparse.Namespace(config=str(config), no_gateway=True, no_remote=True)
    return {r.name: r for r in run_checks(conn, args, now)}


@pytest.mark.parametrize(
    ("key", "value", "check", "before", "after"),
    [
        ("monitoring.coverage_min", "60%", "slot_coverage", "failed", "ok"),
        ("monitoring.coverage_window", "30", "slot_coverage", "failed", "ok"),
        ("monitoring.per_slot_min_interval", "5", "routine_windows", "ok", "failed"),
        ("monitoring.per_slot_min_interval", "5", "slot_coverage", "failed", "ok"),
        ("monitoring.tick_slow_count", "3", "tick_slow", "failed", "ok"),
        ("monitoring.tick_slow_after", "6", "tick_slow", "failed", "ok"),
    ],
)
def test_control_override_changes_the_check(
    conn: sqlite3.Connection, config_file: Path,
    key: str, value: str, check: str, before: str, after: str,
) -> None:  # fmt: skip
    # Misses early in the hour (a 30-min window no longer sees them); 2 slow ticks.
    ticks(conn, et(9, 0), et(10, 50))
    hb = HeartbeatRepo(conn)
    for t in (et(10, 52), et(10, 56)):
        hb.record("tick", "ok", at=t, detail={"tick_duration_ms": 330_000, "slowest_jobs": SLOW})
    early = {et(9, 50), et(9, 55), et(10, 0), et(10, 5)}
    fill_research(conn, et(9, 40), et(10, 55), skip=early)
    assert _results(conn, config_file, NOW)[check].severity == before
    _set(conn, key, value)
    assert effective_routines(conn, config_file).monitoring != load_routines(config_file).monitoring
    assert _results(conn, config_file, NOW)[check].severity == after
    assert _svc(conn).view(key).value is not None


# ---------------------------------------------------------------------------
# E4.1d: coverage:earnings (stale earnings calendar)
# ---------------------------------------------------------------------------

EARNINGS_YAML = """
    sources:
      earnings:
        schedule: ["06:00", "18:00"]
        days: trading
        category: company
        writes: [raw_doc_ref]
    monitoring:
      gateway: {enabled: false}
"""
STOCKS = ["SPY", "QQQ", "AAPL", "MSFT"]


def _earnings_cfg() -> RoutinesConfig:
    return RoutinesConfig.model_validate(yaml.safe_load(textwrap.dedent(EARNINGS_YAML)))


def _earnings_doc(conn: sqlite3.Connection, ingested: dt.datetime, sym: str = "AAPL") -> None:
    conn.execute(
        """INSERT INTO raw_docs (id, source, url, published_at, text, tickers_hint,
                                 content_hash, ingested_at)
           VALUES (?, 'earnings', ?, '2026-10-28T00:00:00+00:00', 't', ?, ?, ?)""",
        (uuid.uuid4().hex[:16], f"https://finnhub.io/calendar/earnings/{sym}/2026-10-28",
         f'["{sym}"]', uuid.uuid4().hex, to_db(ingested)),
    )  # fmt: skip
    conn.commit()


def test_stale_earnings_calendar_alerts(conn: sqlite3.Connection) -> None:
    """E4.1d: no earnings doc newer than 7 d while the universe has stocks -> one
    `coverage:earnings` condition (posted once, resolved when a fresh doc lands)."""
    r, ms = _earnings_cfg(), MonitoringSettings()
    run(conn, "earnings", et(6, 0), "skipped", "no_api_key: ARC_FINNHUB_API_KEY is not set")

    res = checks.earnings_coverage(conn, r, ms, NOW, STOCKS)
    assert res.severity == "failed"
    (f,) = res.findings
    assert f.key == "coverage:earnings"
    assert "none ever stored" in f.message and "no_api_key" in f.message
    assert "AAPL" in f.message and "SPY" not in f.message

    n = alerts.RecordingOpsNotifier()
    out = alerts.apply(conn, [res], now=NOW, correlation={}, notifier=n)
    assert [a.key for a in out.opened] == ["coverage:earnings"]
    assert len(n.posts) == 1 and "earnings calendar stale" in n.posts[0]
    # Still stale an hour later: no repeat post.
    later = NOW + dt.timedelta(hours=1)
    alerts.apply(conn, [checks.earnings_coverage(conn, r, ms, later, STOCKS)], now=later,
                 correlation={}, notifier=n)  # fmt: skip
    assert len(n.posts) == 1
    # A slot-coverage run alone does not resolve it (it is not that check's alert).
    alerts.apply(conn, [checks.slot_coverage(conn, r, ms, later)], now=later, correlation={},
                 notifier=n)  # fmt: skip
    assert AlertRepo(conn).open_for("coverage:earnings") is not None

    # An old doc (8 d) is still stale; a fresh one resolves the alert.
    _earnings_doc(conn, NOW - dt.timedelta(days=8))
    assert checks.earnings_coverage(conn, r, ms, later, STOCKS).severity == "failed"
    _earnings_doc(conn, later - dt.timedelta(hours=2), "MSFT")
    fresh = checks.earnings_coverage(conn, r, ms, later, STOCKS)
    assert fresh.severity == "ok" and "2 stored" in fresh.summary
    out = alerts.apply(conn, [fresh], now=later, correlation={}, notifier=n)
    assert [a.key for a in out.resolved] == ["coverage:earnings"]
    assert "earnings calendar fresh again" in n.posts[-1]


def test_earnings_coverage_not_judged(conn: sqlite3.Connection) -> None:
    r, ms = _earnings_cfg(), MonitoringSettings()
    # ETF-only universe: ETFs have no earnings, nothing to alert on.
    assert checks.earnings_coverage(conn, r, ms, NOW, ["SPY", "QQQ"]).severity == "ok"
    # No earnings job enabled (e.g. a test config): not judged.
    assert checks.earnings_coverage(conn, cfg(), ms, NOW, STOCKS).severity == "ok"
    # Threshold is config.
    _earnings_doc(conn, NOW - dt.timedelta(days=3))
    short = MonitoringSettings(earnings_stale_after=dt.timedelta(days=2))
    assert checks.earnings_coverage(conn, r, short, NOW, STOCKS).severity == "failed"
    assert checks.earnings_coverage(conn, r, ms, NOW, STOCKS).severity == "ok"
    # The earnings job never gets a slot-ratio coverage alert of its own.
    assert "earnings" not in checks.slot_coverage(conn, r, ms, NOW).detail.get("jobs", {})


def test_earnings_coverage_in_run_checks_and_override(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    p = tmp_path / "routines.yaml"
    p.write_text(textwrap.dedent(EARNINGS_YAML))
    _earnings_doc(conn, NOW - dt.timedelta(days=3))
    assert _results(conn, p, NOW)["earnings_coverage"].severity == "ok"
    _set(conn, "monitoring.earnings_stale_after", "2")
    assert effective_routines(conn, p).monitoring.earnings_stale_after == dt.timedelta(days=2)
    assert _results(conn, p, NOW)["earnings_coverage"].severity == "failed"


def test_stale_earnings_is_not_folded_into_a_tick_incident(conn: sqlite3.Connection) -> None:
    """A tick outage does not explain a calendar that was never stored: post it."""
    r, ms = _earnings_cfg(), MonitoringSettings()
    n = alerts.RecordingOpsNotifier()
    results = [
        checks.tick_staleness(conn, ms, NOW),
        checks.earnings_coverage(conn, r, ms, NOW, STOCKS),
    ]
    out = alerts.apply(conn, results, now=NOW, correlation={}, notifier=n)
    assert {a.key for a in out.opened} == {"tick_stale", "coverage:earnings"}
    assert not out.folded
    assert "earnings calendar stale" in n.posts[0]
