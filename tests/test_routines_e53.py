"""E5.3: shipped routine defaults, weekly days, day-thread heartbeats, monitor, cron script."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
import yaml
from pydantic import ValidationError

from arc.broker.base import AccountInfo, BrokerPosition
from arc.cli import main
from arc.config import ArcSettings
from arc.context import ContextStore
from arc.routines.config import DEFAULT_ROUTINES_PATH, Days, RoutinesConfig, Weekday, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import BUILTIN_HANDLERS, JobContext, JobResult, youtube_source
from arc.routines.heartbeat import Heartbeats, RecordingNotifier, thread_day
from arc.routines.monitor import monitor
from arc.routines.schedule import day_matches, slots_between
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.ingest.youtube import YoutubeRunStats
    from arc.pipeline.env import PipelineEnv

REPO = Path(__file__).resolve().parent.parent


def et(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ET)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(scope="module")
def shipped() -> RoutinesConfig:
    return load_routines(DEFAULT_ROUTINES_PATH)


def _day_plan(cfg: RoutinesConfig, start: dt.datetime, end: dt.datetime) -> dict[str, list[str]]:
    """Job -> 'HH:MM' slots over (start, end], simulated tick by tick (every 5 min)."""
    d = Dispatcher(connect(":memory:"), cfg, is_halted=lambda: False)
    migrate(d.conn)
    out: dict[str, list[str]] = {}
    prev, cur = start, start + dt.timedelta(minutes=5)
    while cur <= end:
        for due in d.plan(cur, since=prev, halted=False):
            assert due.action == "run", due
            out.setdefault(due.job, []).append(f"{due.slot:%a %H:%M}")
        prev, cur = cur, cur + dt.timedelta(minutes=5)
    return out


# ---------------------------------------------------------------------------
# Shipped config/routines.yaml (the card's acceptance cadences)
# ---------------------------------------------------------------------------


class TestShippedDefaults:
    def test_sources(self, shipped: RoutinesConfig) -> None:
        yt = shipped.sources["youtube.stockedup"]
        assert yt.cadence == "at 12:00, 22:00 ET (daily)"
        assert shipped.sources["rss"].cadence == "every 30m 06:00-20:00 ET (trading)"
        assert shipped.sources["edgar"].cadence == "every 15m 06:00-20:00 ET (trading)"
        assert shipped.sources["earnings"].cadence == "at 06:00, 18:00 ET (trading)"

    def test_personas(self, shipped: RoutinesConfig) -> None:
        p = shipped.personas
        assert p["scout"].cadence == "at 12:00, 22:00 ET (daily)" and p["scout"].after_sources
        assert p["director"].cadence == "at 09:30 ET (trading)"
        assert p["director"].chain == ["quant", "risk", "propose"]
        assert p["monitor"].cadence == "every 30m 09:30-16:00 ET (trading)"
        assert p["monitor"].llm is False and p["monitor"].halt_exempt
        assert p["auditor"].cadence == "at 16:30 ET (trading)" and p["auditor"].halt_exempt
        assert p["scorecard"].cadence == "at 16:45 ET (fri)"
        assert p["investor"].trigger == "approval"
        (rule,) = shipped.triggers_for("scout.completed")
        assert rule.run == "director"
        assert rule.condition == "new_candidates > 0 and session == 'open'"

    def test_monitor_handler_registered(self, shipped: RoutinesConfig) -> None:
        assert BUILTIN_HANDLERS["monitor"] == "arc.routines.monitor:monitor_step"

    def test_full_trading_day(self, shipped: RoutinesConfig) -> None:
        plan = _day_plan(shipped, et(2026, 9, 28, 0, 0), et(2026, 9, 29, 0, 0))  # Monday
        assert plan["youtube.stockedup"] == ["Mon 12:00", "Mon 22:00"]
        assert plan["scout"] == ["Mon 12:00", "Mon 22:00"]
        assert plan["earnings"] == ["Mon 06:00", "Mon 18:00"]
        assert len(plan["rss"]) == 29 and plan["rss"][0] == "Mon 06:00"  # 06:00..20:00 / 30m
        assert len(plan["edgar"]) == 57 and plan["edgar"][-1] == "Mon 20:00"
        assert plan["director"] == ["Mon 09:30"]
        assert len(plan["monitor"]) == 14
        assert (plan["monitor"][0], plan["monitor"][-1]) == ("Mon 09:30", "Mon 16:00")
        assert plan["auditor"] == ["Mon 16:30"]
        assert "scorecard" not in plan and "investor" not in plan

    def test_friday_scorecard_and_weekend(self, shipped: RoutinesConfig) -> None:
        fri = _day_plan(shipped, et(2026, 10, 2, 0, 0), et(2026, 10, 3, 0, 0))
        assert fri["scorecard"] == ["Fri 16:45"]
        weekend = _day_plan(shipped, et(2026, 10, 3, 0, 0), et(2026, 10, 5, 0, 0))
        # Only the daily jobs; Sunday 22:00 matters (StockedUp posts Sunday for Monday).
        assert set(weekend) == {"youtube.stockedup", "scout"}
        assert weekend["scout"] == ["Sat 12:00", "Sat 22:00", "Sun 12:00", "Sun 22:00"]

    def test_sources_run_before_scout_in_the_same_tick(self, shipped: RoutinesConfig) -> None:
        d = Dispatcher(connect(":memory:"), shipped, is_halted=lambda: False)
        order = [x.job for x in d.plan(et(2026, 9, 27, 22, 0), since=et(2026, 9, 27, 21, 55))]
        assert order == ["youtube.stockedup", "scout"]

    def test_halt_skips_chain_but_not_monitor_auditor(self, shipped: RoutinesConfig) -> None:
        d = Dispatcher(connect(":memory:"), shipped, is_halted=lambda: True)
        at_open = {
            x.job: x.action
            for x in d.plan(et(2026, 9, 28, 9, 30), since=et(2026, 9, 28, 9, 25), halted=True)
        }
        assert at_open["director"] == "skip-halted"
        assert at_open["monitor"] == "run"
        post = d.plan(et(2026, 9, 28, 16, 30), since=et(2026, 9, 28, 16, 25), halted=True)
        assert {x.job: x.action for x in post}["auditor"] == "run"


# ---------------------------------------------------------------------------
# Weekly days
# ---------------------------------------------------------------------------


class TestWeekdays:
    def test_parse_and_match(self) -> None:
        cfg = RoutinesConfig.model_validate(
            {"personas": {"w": {"schedule": "16:45", "days": ["Fri", "monday"]}}}
        )
        spec = cfg.personas["w"]
        assert spec.days == [Weekday.FRI, Weekday.MON]
        assert spec.cadence == "at 16:45 ET (fri,mon)"
        assert day_matches(spec.days, dt.date(2026, 10, 2))  # Fri
        assert day_matches(spec.days, dt.date(2026, 10, 5))  # Mon
        assert not day_matches(spec.days, dt.date(2026, 10, 3))
        slots = slots_between(spec, et(2026, 9, 28, 0, 0), et(2026, 10, 12, 0, 0))
        assert [f"{s:%a %d}" for s in slots] == ["Mon 28", "Fri 02", "Mon 05", "Fri 09"]

    @pytest.mark.parametrize("days", [[], ["fri", "friday"], ["xyz"]])
    def test_invalid(self, days: list[str]) -> None:
        with pytest.raises(ValidationError):
            RoutinesConfig.model_validate({"personas": {"w": {"schedule": "16:45", "days": days}}})

    def test_named_days_still_work(self) -> None:
        cfg = RoutinesConfig.model_validate({"sources": {"s": {"every": "1h", "days": "weekdays"}}})
        assert cfg.sources["s"].days is Days.WEEKDAYS

    def test_weekday_index(self) -> None:
        assert [w.weekday_index for w in Weekday] == list(range(7))


# ---------------------------------------------------------------------------
# Heartbeats: day thread, folding, notices
# ---------------------------------------------------------------------------


class TestDayThread:
    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (et(2026, 9, 28, 12, 0), dt.date(2026, 9, 28)),  # Mon midday -> Mon
            (et(2026, 9, 28, 19, 59), dt.date(2026, 9, 28)),
            (et(2026, 9, 28, 22, 0), dt.date(2026, 9, 29)),  # Mon 22:00 Scout -> Tue
            (et(2026, 9, 27, 22, 0), dt.date(2026, 9, 28)),  # Sun 22:00 -> Mon
            (et(2026, 10, 2, 22, 0), dt.date(2026, 10, 5)),  # Fri night -> Mon
            (et(2026, 10, 3, 12, 0), dt.date(2026, 10, 5)),  # Sat -> Mon
            (et(2026, 11, 26, 12, 0), dt.date(2026, 11, 27)),  # Thanksgiving -> Fri
        ],
    )
    def test_thread_day(self, now: dt.datetime, expected: dt.date) -> None:
        assert thread_day(now) == expected

    def test_rollover_is_configurable(self) -> None:
        cfg = RoutinesConfig.model_validate({"heartbeat": {"day_rollover": "23:00"}})
        assert cfg.heartbeat.day_rollover == dt.time(23, 0)
        assert thread_day(et(2026, 9, 28, 22, 0), cfg.heartbeat.day_rollover) == dt.date(
            2026, 9, 28
        )
        with pytest.raises(ValidationError):
            RoutinesConfig.model_validate({"heartbeat": {"day_rollover": "25:00"}})


class TestHeartbeatPolicy:
    def test_repeated_source_runs_fold_into_one_entry(self, conn: sqlite3.Connection) -> None:
        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        for n in (3, 0, 2):
            hb.queue_source("edgar", f"{n} new docs", new_docs=n)
        hb.queue_source("rss", "1 new doc", new_docs=1)
        hb.summary(et(2026, 9, 28, 12, 0), "scout", "done")
        (day, text) = notes.posts[0]
        assert day == dt.date(2026, 9, 28)
        assert "edgar ×3, 5 new docs total (last: 2 new docs)" in text
        assert "rss: 1 new doc" in text
        hb.summary(et(2026, 9, 28, 12, 30), "director", "x")
        assert "sources since" not in notes.posts[1][1]

    def test_legacy_string_rows_are_read(self, conn: sqlite3.Connection) -> None:
        from arc.routines.runs import RoutineStateRepo

        RoutineStateRepo(conn).set("heartbeat:pending_sources", json.dumps(["rss: 2 new docs"]))
        hb = Heartbeats(conn, RecordingNotifier())
        hb.queue_source("rss", "1 new doc")
        assert hb._pending() == ["rss ×2 (last: 1 new doc)"]

    def test_notice_and_alert_prefixes(self, conn: sqlite3.Connection) -> None:
        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        hb.notice(et(2026, 9, 28, 10, 0), "monitor", "halt")
        hb.alert(et(2026, 9, 28, 22, 0), "scout", "boom")
        assert notes.posts[0] == (dt.date(2026, 9, 28), ":warning: [Routines] monitor: halt")
        assert notes.posts[1] == (
            dt.date(2026, 9, 29),
            ":rotating_light: [Scout] scout FAILED: boom",
        )

    def test_dispatcher_posts_quiet_job_notice_and_rolls_over(
        self, conn: sqlite3.Connection
    ) -> None:
        cfg = RoutinesConfig.model_validate(
            {
                "personas": {
                    "monitor": {"every": "30m", "notify": "quiet", "llm": False},
                    "scout": {"schedule": ["22:00"]},
                }
            }
        )
        notes = RecordingNotifier()
        handlers = {
            "monitor": lambda ctx: JobResult(summary="ok", notice="HALT"),
            "scout": lambda ctx: JobResult(summary="3 candidates"),
        }
        d = Dispatcher(conn, cfg, handlers=handlers, notifier=notes, is_halted=lambda: False)
        d.tick(et(2026, 9, 28, 22, 0), since=et(2026, 9, 28, 21, 55))
        assert notes.posts[0] == (dt.date(2026, 9, 29), ":warning: [Routines] monitor: HALT")
        assert notes.posts[1][0] == dt.date(2026, 9, 29)
        assert notes.posts[1][1].startswith("[Scout] scout ✓ 3 candidates")
        assert "monitor: ok" in notes.posts[1][1]


# ---------------------------------------------------------------------------
# YouTube run summary (owner note: every Scout run shows the caption outcome)
# ---------------------------------------------------------------------------


def _ctx(
    conn: sqlite3.Connection,
    job: str,
    spec: dict[str, object],
    now: dt.datetime,
    settings: ArcSettings | None = None,
) -> JobContext:
    key = "sources" if job.startswith("youtube") else "personas"
    routines = RoutinesConfig.model_validate({key: {job: spec}})
    kind, step = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=step,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=lambda: settings or ArcSettings(),
    )


def test_youtube_source_summary_includes_caption_outcome(conn: sqlite3.Connection) -> None:
    def fake_fetch(_conn: object, _settings: object, *, stats: YoutubeRunStats) -> list[object]:
        stats.captions = {"rate_limited": 1}
        stats.captions_skipped, stats.skip_reason = 2, "breaker"
        stats.audio, stats.audio_wall_s = 2, 158.4
        stats.cooldown_until = et(2026, 9, 28, 22, 34)
        stats.consecutive_rate_limits = 1
        return []

    ctx = _ctx(conn, "youtube.stockedup", {"schedule": ["22:00"], "channel": "UCx"},
               et(2026, 9, 28, 22, 0))  # fmt: skip
    with mock.patch("arc.ingest.youtube.fetch_youtube", side_effect=fake_fetch):
        result = youtube_source(ctx)
    assert result.summary == (
        "0 new docs · captions: ok 0, rate_limited 1, empty 0, error 0, skipped 2 (breaker)"
        " · audio 2 (158s wall) · captions cooldown until Mon 22:34 ET (streak 1)"
    )
    assert result.metrics["captions_rate_limited"] == 1
    assert result.metrics["audio_fallbacks"] == 2
    assert result.metrics["captions_cooldown_active"] is True


# ---------------------------------------------------------------------------
# Intraday monitor
# ---------------------------------------------------------------------------


FIXTURE_NOW = et(2026, 9, 25, 16, 0)


def _env(
    positions: list[BrokerPosition], *, equity: str = "100000", last: str = "100000"
) -> PipelineEnv:
    from arc.pipeline.env import PipelineEnv

    env = PipelineEnv.fixtures()
    info = AccountInfo(
        account_id="PAPER",
        equity=Decimal(equity),
        buying_power=Decimal("1"),
        cash=Decimal("1"),
        last_equity=Decimal(last),
    )
    env.account = lambda: info
    env.positions = lambda: positions
    return env


def _spread() -> list[BrokerPosition]:
    return [
        BrokerPosition(
            symbol="SPY261030P00711000", qty=Decimal(-1), side="short",
            avg_entry_price=Decimal("5.10"),
        ),
        BrokerPosition(
            symbol="SPY261030P00710000", qty=Decimal(1), side="long",
            avg_entry_price=Decimal("4.80"),
        ),
    ]  # fmt: skip


def _settings() -> ArcSettings:
    return ArcSettings(env="paper")  # type: ignore[call-arg]


class TestMonitor:
    def test_no_positions(self, conn: sqlite3.Connection) -> None:
        ctx = _ctx(conn, "monitor", {"every": "30m"}, FIXTURE_NOW, _settings())
        r = monitor(ctx, _env([]))
        assert r.summary == "equity $100,000.00, day P&L $+0.00; 0 position(s)"
        assert r.metrics["positions"] == 0 and r.metrics["valued"] is True
        assert r.notice == ""

    def test_values_open_spread(self, conn: sqlite3.Connection) -> None:
        ctx = _ctx(conn, "monitor", {"every": "30m"}, FIXTURE_NOW, _settings())
        r = monitor(ctx, _env(_spread()))
        assert r.metrics["positions"] == 1
        assert r.metrics["max_loss"] == pytest.approx(70.0)  # $1 wide - $0.30 credit
        assert "[SPY], max loss $70" in r.summary and "Δ" in r.summary
        assert r.notice == ""

    def test_records_monitor_heartbeat_for_tower(self, conn: sqlite3.Connection) -> None:
        """E8.3: each run persists its Greeks and broker legs (the tower's only source)."""
        from arc.monitoring.store import HeartbeatRepo

        ctx = _ctx(conn, "monitor", {"every": "30m"}, FIXTURE_NOW, _settings())
        r = monitor(ctx, _env(_spread(), equity="100250"))
        hb = HeartbeatRepo(conn).latest("monitor")
        assert hb is not None and hb.status == "ok" and hb.at == FIXTURE_NOW
        assert hb.correlation == {"run_id": "run-1"}
        assert hb.detail["delta"] == pytest.approx(r.metrics["delta"])
        assert hb.detail["equity"] == 100250.0 and hb.detail["last_equity"] == 100000.0
        assert [leg["symbol"] for leg in hb.detail["legs"]] == [
            "SPY261030P00711000",
            "SPY261030P00710000",
        ]
        assert hb.detail["legs"][0]["qty"] == "-1"

    def test_daily_loss_raises_halt_notice_once(self, conn: sqlite3.Connection) -> None:
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        env = _env([], equity="96000", last="100000")
        ctx = _ctx(conn, "monitor", {"every": "30m"}, FIXTURE_NOW, _settings())
        r = monitor(ctx, env)
        assert r.metrics["halt_raised"] is True and r.metrics["halted"] is True
        assert r.notice.startswith("daily-loss halt raised:")
        assert r.summary.endswith("; HALTED")
        assert HaltSwitch(HaltRepo(conn)).is_halted()
        later = _ctx(conn, "monitor", {"every": "30m"},
                     FIXTURE_NOW + dt.timedelta(minutes=30), _settings())  # fmt: skip
        r2 = monitor(later, env)
        assert r2.metrics["halt_raised"] is False and r2.notice == ""

    def test_unvaluable_position_and_expiry_warn(self, conn: sqlite3.Connection) -> None:
        naked = [
            BrokerPosition(symbol="SPY260928P00700000", qty=Decimal(-1), side="short",
                           avg_entry_price=Decimal("1")),
        ]  # fmt: skip
        ctx = _ctx(conn, "monitor", {"every": "30m", "expiry_warn_days": 3}, FIXTURE_NOW,
                   _settings())  # fmt: skip
        r = monitor(ctx, _env(naked))
        assert r.metrics["valued"] is False and r.metrics["positions"] is None
        assert "positions NOT valued" in r.summary
        assert "cannot value open positions" in r.notice
        from arc.monitoring.store import HeartbeatRepo

        hb = HeartbeatRepo(conn).latest("monitor")
        assert hb is not None and hb.status == "degraded" and hb.detail["valued"] is False
        assert "expiring within 3 day(s): SPY 09-28" in r.notice
        # same notice again the same day is not re-posted
        assert monitor(ctx, _env(naked)).notice == ""

    def test_monitor_never_submits(self) -> None:
        """E6.2: the monitor may *propose* exits (gate + token + card) but never sends one."""
        src = (REPO / "arc" / "routines" / "monitor.py").read_text()
        src += (REPO / "arc" / "execution" / "exits.py").read_text()
        for forbidden in ("submit(", "submit_mleg", "arc.execution.ladder", "import execute"):
            assert forbidden not in src, forbidden


# ---------------------------------------------------------------------------
# CLI: full simulated day
# ---------------------------------------------------------------------------


class TestSimulateCli:
    def test_step_simulates_ticks(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            ["routines", "tick", "--dry-run", "--step", "5m",
             "--since", "2026-09-27T21:55", "--now", "2026-09-28T09:30"]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0
        assert "tick Sun 2026-09-27 22:00 EDT" in out
        assert "tick Mon 2026-09-28 09:30 EDT" in out
        assert "↳ Mon 09:30 propose" in out
        assert out.rstrip().endswith("had work")

    def test_step_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            ["routines", "tick", "--dry-run", "--json", "--step", "30m",
             "--since", "2026-10-03T00:00", "--now", "2026-10-04T00:00"]
        )  # fmt: skip
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        jobs = [o["job"] for t in data["ticks"] for o in t["outcomes"]]
        assert jobs.count("scout") == 2 and "rss" not in jobs

    @pytest.mark.parametrize("extra", [[], ["--dry-run", "--step", "0m"]])
    def test_step_errors(self, extra: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        args = ["routines", "tick", "--step", "5m", *extra]
        if extra:
            args = ["routines", "tick", *extra]
        assert main(args) == 2
        assert "error:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Hermes cron script (hermes/routines/)
# ---------------------------------------------------------------------------


def _load_tick_script(cwd: Path, monkeypatch: pytest.MonkeyPatch, home: Path):  # noqa: ANN202
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    path = REPO / "hermes" / "routines" / "arc_routines_tick.py"
    spec = importlib.util.spec_from_file_location("arc_routines_tick_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_arc(repo: Path, body: str) -> None:
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    arc = bin_dir / "arc"
    arc.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
    arc.chmod(arc.stat().st_mode | stat.S_IEXEC)


class TestTickScript:
    def test_success_is_silent_and_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        home = tmp_path / "home"
        (home / ".hermes").mkdir(parents=True)
        (home / ".hermes" / ".env").write_text(
            "SLACK_BOT_TOKEN='xoxb-test'\nARC_FOO=bar\nOTHER_SECRET=nope\n# c\n"
        )
        repo = tmp_path / "repo"
        _fake_arc(
            repo,
            "print(sys.argv[1:], os.environ.get('SLACK_BOT_TOKEN'), os.environ.get('ARC_FOO'),"
            " os.environ.get('OTHER_SECRET'), os.environ.get('ARC_ENV'))",
        )
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ARC_ENV", raising=False)
        mod = _load_tick_script(repo, monkeypatch, home)
        assert mod.main() == 0
        assert capsys.readouterr().out == ""
        log = (repo / "data" / "logs" / "routines-tick.log").read_text()
        assert "exit=0" in log
        assert "['routines', 'tick'] xoxb-test bar None paper" in log

    @pytest.mark.parametrize(("code", "rc", "loud"), [(1, 0, False), (2, 2, True)])
    def test_exit_codes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: int,
        rc: int,
        loud: bool,
    ) -> None:
        repo = tmp_path / "repo"
        _fake_arc(repo, f"sys.stderr.write('Traceback: kaboom\\n'); sys.exit({code})")
        mod = _load_tick_script(repo, monkeypatch, tmp_path)
        assert mod.main() == rc
        out = capsys.readouterr().out
        assert ("kaboom" in out) is loud

    def test_missing_venv_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mod = _load_tick_script(tmp_path, monkeypatch, tmp_path)
        assert mod.main() == 2
        assert "not found" in capsys.readouterr().out

    def test_timeout_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        _fake_arc(repo, "import time; time.sleep(5)")
        monkeypatch.setenv("ARC_TICK_TIMEOUT_SECONDS", "1")
        mod = _load_tick_script(repo, monkeypatch, tmp_path)
        assert mod.main() == 3
        assert "timed out" in capsys.readouterr().out

    def test_log_rotation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _load_tick_script(tmp_path, monkeypatch, tmp_path)
        mod.LOG_MAX_BYTES = 10
        for i in range(5):
            mod._log(f"entry {i} " + "x" * 20)
        logs = sorted(p.name for p in mod.LOG.parent.iterdir())
        assert logs == ["routines-tick.log", "routines-tick.log.1", "routines-tick.log.2",
                        "routines-tick.log.3"]  # fmt: skip
        assert "entry 4" in mod.LOG.read_text()

    def test_install_script_is_executable_and_single_job(self) -> None:
        sh = REPO / "hermes" / "routines" / "install.sh"
        assert os.access(sh, os.X_OK)
        text = sh.read_text()
        assert text.count("hermes cron create") == 1
        assert '"every 5m"' in text and "--no-agent" in text and "--workdir" in text


def test_yaml_comment_overview_matches_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["tick"]["interval"] == "5m"
    assert set(raw["personas"]) == {
        "scout", "director", "monitor", "auditor", "scorecard", "investor",
        "positions.evaluate",
    }  # fmt: skip
