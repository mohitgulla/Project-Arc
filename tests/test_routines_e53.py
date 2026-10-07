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
        yt = shipped.sources["youtube.briefs"]
        assert yt.cadence == "at 02:00 ET (trading)"  # D45: one pre-market brief run
        assert shipped.sources["rss"].cadence == "every 15m 06:00-20:00 ET (trading)"  # D31
        assert shipped.sources["edgar"].cadence == "every 15m 06:00-20:00 ET (trading)"
        assert shipped.sources["earnings"].cadence == "at 06:00, 18:00 ET (trading)"

    def test_personas(self, shipped: RoutinesConfig) -> None:
        p = shipped.personas
        # D31: 30-min Scalp in session + the 22:00 overnight run; D52: 10-min trading loop.
        assert p["scalp"].cadence == "every 30m 09:00-16:00 ET (trading)"
        assert p["scalp"].after_sources and p["scalp.overnight"].after_sources
        assert p["scalp.overnight"].cadence == "at 22:00 ET (daily)"
        assert p["research"].cadence == "every 10m 09:40-15:50 ET (trading)"
        assert p["research"].chain == [
            "exits.mandatory", "quant.exit", "risk.exit", "quant.open", "risk.open",
            "quant.revise", "quant.propose", "broker.execute",
        ]  # fmt: skip
        assert p["research"].ttl is not None
        assert p["research"].ttl.duration == dt.timedelta(minutes=5)
        assert p["monitor"].cadence == "every 10m 09:30-16:00 ET (trading)"  # D35, D52
        assert p["monitor"].options["eod_marks_from"] == "15:50"
        assert p["monitor"].llm is False and p["monitor"].halt_exempt
        assert (
            p["broker.reconcile"].cadence == "at 16:30 ET (trading)"
            and p["broker.reconcile"].halt_exempt
        )
        assert p["scorecard"].cadence == "at 16:45 ET (fri)"
        assert p["broker"].trigger == "approval"
        assert shipped.triggers_for("scalp.completed") == []  # D31: the loop polls instead
        assert shipped.loop.job == "research" and shipped.is_loop("research")

    def test_monitor_handler_registered(self, shipped: RoutinesConfig) -> None:
        assert BUILTIN_HANDLERS["monitor"] == "arc.routines.monitor:monitor_step"

    def test_full_trading_day(self, shipped: RoutinesConfig) -> None:
        plan = _day_plan(shipped, et(2026, 9, 28, 0, 0), et(2026, 9, 29, 0, 0))  # Monday
        assert plan["youtube.briefs"] == ["Mon 02:00"]  # D45: 23:00 PT, trading days only
        # D31: Scalp 09:00..16:00 every 30 min = 15 runs, plus the 22:00 overnight run.
        assert len(plan["scalp"]) == 15
        assert (plan["scalp"][0], plan["scalp"][-1]) == ("Mon 09:00", "Mon 16:00")
        assert plan["scalp.overnight"] == ["Mon 22:00"]
        assert plan["earnings"] == ["Mon 06:00", "Mon 18:00"]
        assert len(plan["rss"]) == 57 and plan["rss"][0] == "Mon 06:00"  # 06:00..20:00 / 15m
        assert len(plan["edgar"]) == 57 and plan["edgar"][-1] == "Mon 20:00"
        # D52: the loop, 09:40..15:50 inclusive every 10 min = 38 slots (was 75 at 5m).
        assert len(plan["research"]) == 38 and len(set(plan["research"])) == 38
        assert (plan["research"][0], plan["research"][-1]) == ("Mon 09:40", "Mon 15:50")
        # D52: every 10 min, 09:30..16:00 inclusive = 6.5 h x 6 + 1 = 40 slots
        assert len(plan["monitor"]) == 40
        assert len(set(plan["monitor"])) == 40  # no slot planned twice
        assert (plan["monitor"][0], plan["monitor"][-1]) == ("Mon 09:30", "Mon 16:00")
        assert len(plan["positions.evaluate"]) == 13  # D52: 09:50..15:50 every 30 min (:20/:50)
        assert plan["broker.reconcile"] == ["Mon 16:30"]
        assert "scorecard" not in plan and "broker" not in plan

    def test_friday_scorecard_and_weekend(self, shipped: RoutinesConfig) -> None:
        fri = _day_plan(shipped, et(2026, 10, 2, 0, 0), et(2026, 10, 3, 0, 0))
        assert fri["scorecard"] == ["Fri 16:45"]
        weekend = _day_plan(shipped, et(2026, 10, 3, 0, 0), et(2026, 10, 5, 0, 0))
        # Only the daily overnight Scalp; YouTube runs on trading days only (D45).
        assert set(weekend) == {"scalp.overnight"}
        assert weekend["scalp.overnight"] == ["Sat 22:00", "Sun 22:00"]

    def test_youtube_briefs_trading_days_only(self, shipped: RoutinesConfig) -> None:
        """E4.6: 02:00 ET on a trading day; none on a Saturday or a market holiday."""
        mon = _day_plan(shipped, et(2026, 9, 28, 0, 0), et(2026, 9, 29, 0, 0))
        assert mon["youtube.briefs"] == ["Mon 02:00"]
        sat = _day_plan(shipped, et(2026, 10, 3, 0, 0), et(2026, 10, 4, 0, 0))
        assert "youtube.briefs" not in sat
        thanksgiving = _day_plan(shipped, et(2026, 11, 26, 0, 0), et(2026, 11, 27, 0, 0))
        assert "youtube.briefs" not in thanksgiving
        assert "rss" not in thanksgiving  # same calendar as the other trading-day jobs
        friday_after = _day_plan(shipped, et(2026, 11, 27, 0, 0), et(2026, 11, 28, 0, 0))
        assert friday_after["youtube.briefs"] == ["Fri 02:00"]  # early close is a trading day

    def test_sources_run_before_scalp_in_the_same_tick(self, shipped: RoutinesConfig) -> None:
        d = Dispatcher(connect(":memory:"), shipped, is_halted=lambda: False)
        order = [x.job for x in d.plan(et(2026, 9, 27, 22, 0), since=et(2026, 9, 27, 21, 55))]
        assert order == ["scalp.overnight"]
        # In session: the 15-min sources land before the personas of the same slot.
        # The loop runs before the Scalp (name order), so a 30-min Scalp holding the
        # LLM lock never makes the same tick's loop slot skip; the next slot reads it.
        order = [x.job for x in d.plan(et(2026, 9, 28, 10, 0), since=et(2026, 9, 28, 9, 55))]
        assert order == ["edgar", "options_fast", "rss", "monitor", "research", "scalp"]

    def test_halt_skips_chain_but_not_monitor_auditor(self, shipped: RoutinesConfig) -> None:
        d = Dispatcher(connect(":memory:"), shipped, is_halted=lambda: True)
        at_open = {
            x.job: x.action
            for x in d.plan(et(2026, 9, 28, 9, 40), since=et(2026, 9, 28, 9, 35), halted=True)
        }
        assert at_open["research"] == "skip-halted"
        assert at_open["monitor"] == "run"
        post = d.plan(et(2026, 9, 28, 16, 30), since=et(2026, 9, 28, 16, 25), halted=True)
        assert {x.job: x.action for x in post}["broker.reconcile"] == "run"


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
            # Owner 2026-09-30: the day thread switches at 24:00 ET, not 20:00.
            (et(2026, 9, 28, 22, 0), dt.date(2026, 9, 28)),  # Mon 22:00 Scalp -> Mon
            (et(2026, 9, 28, 23, 59, 59), dt.date(2026, 9, 28)),
            (et(2026, 9, 29, 0, 0), dt.date(2026, 9, 29)),  # midnight -> Tue
            (et(2026, 9, 27, 22, 0), dt.date(2026, 9, 28)),  # Sun 22:00 -> Mon
            (et(2026, 10, 2, 22, 0), dt.date(2026, 10, 2)),  # Fri night -> Fri
            (et(2026, 10, 3, 0, 30), dt.date(2026, 10, 5)),  # Sat early -> Mon
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

    def test_default_and_shipped_rollover_is_midnight(self) -> None:
        from arc.routines.config import load_routines

        assert RoutinesConfig().heartbeat.day_rollover == dt.time.max
        assert load_routines().heartbeat.day_rollover == dt.time.max
        cfg = RoutinesConfig.model_validate({"heartbeat": {"day_rollover": "24:00"}})
        assert thread_day(et(2026, 9, 28, 23, 59), cfg.heartbeat.day_rollover) == dt.date(
            2026, 9, 28
        )


class TestHeartbeatPolicy:
    def test_repeated_source_runs_fold_into_one_entry(self, conn: sqlite3.Connection) -> None:
        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        for n in (3, 0, 2):
            hb.queue_source("edgar", f"{n} new docs", new_docs=n)
        hb.queue_source("rss", "1 new doc", new_docs=1)
        hb.summary(et(2026, 9, 28, 12, 0), "scalp", "done")
        (day, text) = notes.posts[0]
        assert day == dt.date(2026, 9, 28)
        assert "edgar ×3, 5 new docs total (last: 2 new docs)" in text
        assert "rss: 1 new doc" in text
        hb.summary(et(2026, 9, 28, 12, 30), "research", "x")
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
        hb.alert(et(2026, 9, 29, 0, 30), "scalp", "boom")  # after midnight -> Tue
        # A one-line [Routines] notice is inline code on the emoji's line (owner 2026-09-30).
        assert notes.posts[0] == (
            dt.date(2026, 9, 28),
            ":warning: `[Routines] monitor: halt`",
        )
        assert notes.posts[1] == (
            dt.date(2026, 9, 29),
            ":rotating_light: ⚡ [Scalp] scalp FAILED: boom",
        )

    def test_routines_lines_are_code_blocks_and_persona_lines_are_not(
        self, conn: sqlite3.Connection
    ) -> None:
        """E5.5b: every ``[Routines]`` heartbeat is wrapped in ``` fences."""
        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        hb.queue_source("rss", "3 new docs", new_docs=3)
        hb.summary(et(2026, 9, 28, 10, 0), "propose", "1 proposal")
        text = notes.posts[0][1]
        assert text.startswith("```\n[Routines] propose ✓ 1 proposal")
        assert text.endswith("\n```")
        assert "> sources since last update: rss: 3 new docs\n```" in text  # folded inside
        hb.alert(et(2026, 9, 28, 10, 5), "monitor", "boom", run_id="r-1")
        assert notes.posts[1][1] == (":rotating_light: `[Routines] monitor FAILED: boom` `r-1`")
        hb.summary(et(2026, 9, 28, 12, 0), "research", "ranked 2")
        assert notes.posts[2][1] == "🧠 [Research] research ✓ ranked 2"
        hb.alert(et(2026, 9, 28, 12, 5), "quant", "boom")
        assert notes.posts[3][1] == ":rotating_light: 📐 [Quant] quant FAILED: boom"

    def test_routines_code_block_escapes_inner_fences(self, conn: sqlite3.Connection) -> None:
        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        hb.summary(et(2026, 9, 28, 10, 0), "propose", "saw ``` in output")
        text = notes.posts[0][1]
        assert text.count("```") == 2  # only the outer fence survives
        assert text.startswith("```\n") and text.endswith("\n```")
        assert "saw `\u200b`\u200b` in output" in text

    def test_card_folds_sources_into_scalp_session_notes_before_footer(
        self, conn: sqlite3.Connection
    ) -> None:
        """E5.5b: folded sources → ``⚡ [Scalp] Session notes`` section, footer stays last."""
        from arc.slack import blocks as B
        from arc.slack.personas import Persona

        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        hb.queue_source("rss", "3 new <docs>", new_docs=3)
        card = [B.header("🧠 [Research] Ranked"), B.divider(), B.footer(run="r-1", chain="c-1")]
        hb.summary(et(2026, 9, 28, 12, 0), "research", "ranked", blocks=card)
        posted = notes.blocks[0]
        assert posted is not None
        assert [b["type"] for b in posted] == ["header", "divider", "section", "context"]
        assert posted[-1] == B.footer(run="r-1", chain="c-1")
        # Attributed to the Scalp even on a Research card; persona text is escaped.
        assert posted[2]["text"]["text"] == (
            "*⚡ [Scalp] Session notes*\nsources since last update: rss: 3 new &lt;docs&gt;"
        )
        assert card[-1]["type"] == "context"  # the caller's list is not mutated
        assert "> sources since last update: rss: 3 new <docs>" in notes.posts[0][1]

        # A Scalp card that already has session notes gets the line appended.
        hb.queue_source("edgar", "1 new doc", new_docs=1)
        card2 = [
            B.header("⚡ [Scalp] Scan"),
            B.persona_section(Persona.SCALP, "Session notes", "Quiet tape."),
            B.footer(run="r-2"),
        ]
        hb.summary(et(2026, 9, 28, 12, 30), "scalp", "scan", blocks=card2)
        posted2 = notes.blocks[1]
        assert posted2 is not None
        assert [b["type"] for b in posted2] == ["header", "section", "context"]
        assert posted2[1]["text"]["text"] == (
            "*⚡ [Scalp] Session notes*\nQuiet tape.\nsources since last update: edgar: 1 new doc"
        )
        assert posted2[-1] == B.footer(run="r-2")

    def test_card_fold_keeps_fifty_block_cap(self, conn: sqlite3.Connection) -> None:
        from arc.slack import blocks as B

        notes = RecordingNotifier()
        hb = Heartbeats(conn, notes)
        hb.queue_source("rss", "1 new doc")
        card = [
            B.header("🧠 [Research] Ranked"),
            *[B.divider() for _ in range(48)],
            B.footer(run="r"),
        ]
        assert len(card) == B.MAX_BLOCKS
        hb.summary(et(2026, 9, 28, 12, 0), "research", "x", blocks=card)
        posted = notes.blocks[0]
        assert posted is not None
        assert len(posted) == B.MAX_BLOCKS
        assert posted[-1] == B.footer(run="r")
        assert posted[-2]["text"]["text"].startswith("*⚡ [Scalp] Session notes*")

    def test_dispatcher_posts_quiet_job_notice_and_rolls_over(
        self, conn: sqlite3.Connection
    ) -> None:
        cfg = RoutinesConfig.model_validate(
            {
                "personas": {
                    "monitor": {"every": "30m", "notify": "quiet", "llm": False},
                    "scalp": {"schedule": ["22:00"]},
                }
            }
        )
        notes = RecordingNotifier()
        handlers = {
            "monitor": lambda ctx: JobResult(summary="ok", notice="HALT"),
            "scalp": lambda ctx: JobResult(summary="3 candidates"),
        }
        d = Dispatcher(conn, cfg, handlers=handlers, notifier=notes, is_halted=lambda: False)
        d.tick(et(2026, 9, 28, 22, 0), since=et(2026, 9, 28, 21, 55))
        # 24:00 rollover (owner 2026-09-30): Monday's 22:00 posts stay in Monday's thread.
        assert notes.posts[0] == (
            dt.date(2026, 9, 28),
            ":warning: `[Routines] monitor: HALT`",
        )
        assert notes.posts[1][0] == dt.date(2026, 9, 28)
        assert notes.posts[1][1].startswith("⚡ [Scalp] scalp ✓ 3 candidates")
        assert "monitor: ok" in notes.posts[1][1]


# ---------------------------------------------------------------------------
# YouTube run summary (owner note: every Scalp run shows the caption outcome)
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
        # D57: the Tower's dollar delta (Σ Δ × spot) rides on the heartbeat
        assert hb.detail["dollar_delta"] == pytest.approx(r.metrics["dollar_delta"])
        assert hb.detail["dollar_delta"] != 0.0
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

    def test_heartbeat_carries_account_fields_and_leg_marks(self, conn: sqlite3.Connection) -> None:
        """E5.3a (D35): cash / buying power / per-leg marks for the tower, no migration."""
        from arc.monitoring.store import HeartbeatRepo

        legs = [
            p.model_copy(update={"current_price": Decimal("5.25"),
                                 "lastday_price": Decimal("5.00"),
                                 "change_today": Decimal("0.05")})
            for p in _spread()
        ]  # fmt: skip
        env = _env(legs, equity="100250")
        info = AccountInfo(
            account_id="PAPER", equity=Decimal("100250"), last_equity=Decimal("100000"),
            cash=Decimal("98000.5"), buying_power=Decimal("196001"),
            options_buying_power=Decimal("98000.5"), non_marginable_buying_power=Decimal("97000"),
        )  # fmt: skip
        env.account = lambda: info
        ctx = _ctx(conn, "monitor", {"every": "5m"}, FIXTURE_NOW, _settings())
        monitor(ctx, env)
        hb = HeartbeatRepo(conn).latest("monitor")
        assert hb is not None
        d = hb.detail
        assert d["equity"] == 100250.0 and d["last_equity"] == 100000.0
        assert d["cash"] == 98000.5 and d["buying_power"] == 196001.0
        assert d["options_buying_power"] == 98000.5 and d["non_marginable_bp"] == 97000.0
        leg = d["legs"][0]
        assert (leg["current_price"], leg["lastday_price"], leg["change_today"]) == (
            "5.25", "5.00", "0.05",
        )  # fmt: skip
        assert d["broker_requests"] == 6  # account + positions + 4 for the one SPY root

    def test_missing_optional_account_fields_are_null(self, conn: sqlite3.Connection) -> None:
        from arc.monitoring.store import HeartbeatRepo

        env = _env([])
        env.account = lambda: AccountInfo(
            account_id="PAPER", equity=Decimal(1), buying_power=Decimal(2), cash=Decimal(3)
        )
        monitor(_ctx(conn, "monitor", {"every": "5m"}, FIXTURE_NOW, _settings()), env)
        hb = HeartbeatRepo(conn).latest("monitor")
        assert hb is not None
        assert hb.detail["options_buying_power"] is None and hb.detail["last_equity"] is None
        assert hb.detail["non_marginable_bp"] is None and hb.detail["cash"] == 3.0

    def test_notices_post_once_per_day_when_the_mix_changes(self, conn: sqlite3.Connection) -> None:
        """At 5 min the notice mix changes run to run; each distinct text posts once a day."""
        naked = [
            BrokerPosition(symbol="SPY260928P00700000", qty=Decimal(-1), side="short",
                           avg_entry_price=Decimal("1")),
        ]  # fmt: skip
        spec = {"every": "5m", "expiry_warn_days": 3}
        t0 = et(2026, 9, 25, 10, 0)
        r1 = monitor(_ctx(conn, "monitor", spec, t0, _settings()),
                     _env(naked, equity="96000", last="100000"))  # fmt: skip
        assert "daily-loss halt raised" in r1.notice and "expiring within 3" in r1.notice
        assert "cannot value open positions" in r1.notice
        seen: list[str] = []
        for i in range(1, 12):  # the rest of the hour, every 5 min: nothing new
            ctx = _ctx(conn, "monitor", spec, t0 + dt.timedelta(minutes=5 * i), _settings())
            seen.append(monitor(ctx, _env(naked, equity="96000", last="100000")).notice)
        assert seen == [""] * 11
        # a different notice the same day posts, alone
        spec2 = {"every": "5m", "expiry_warn_days": 5}
        r2 = monitor(_ctx(conn, "monitor", spec2, t0 + dt.timedelta(hours=1), _settings()),
                     _env(naked))  # fmt: skip
        assert r2.notice == "expiring within 5 day(s): SPY 09-28"
        # next ET day: posted again
        r3 = monitor(_ctx(conn, "monitor", spec, et(2026, 9, 26, 10, 0), _settings()),
                     _env(naked))  # fmt: skip
        assert "expiring within 3 day(s): SPY 09-28" in r3.notice

    @pytest.mark.parametrize("legacy", ["0123456789abcdef", "1234567890123456"])
    def test_legacy_notice_state_is_replaced(self, conn: sqlite3.Connection, legacy: str) -> None:
        """A pre-E5.3a bare digest in routine_state is not a JSON object: nothing seen."""
        from arc.routines.monitor import _NOTICE_KEY
        from arc.routines.runs import RoutineStateRepo

        RoutineStateRepo(conn).set(_NOTICE_KEY, legacy, now=FIXTURE_NOW)
        env = _env([], equity="96000", last="100000")
        r = monitor(_ctx(conn, "monitor", {"every": "5m"}, FIXTURE_NOW, _settings()), env)
        assert r.notice.startswith("daily-loss halt raised:")

    def test_two_runs_in_one_session_propose_at_most_one_exit(
        self, conn: sqlite3.Connection
    ) -> None:
        """E5.3a: at a 5-min cadence the exit dedupe (exit pending / exit_day) still holds."""
        from tests.test_execution_exits import NOW, bull_put, open_structure
        from tests.test_execution_exits import settings as exit_settings

        open_structure(conn)  # 2 x SPY 711/710 bull put, take profit fires on the fixture
        held = [
            BrokerPosition(symbol=leg.occ_symbol, qty=Decimal(2 if leg.side == "long" else -2),
                           side=str(leg.side), avg_entry_price=Decimal("1"))
            for leg in bull_put().legs
        ]  # fmt: skip
        spec = {"every": "5m", "exits": True, "eod_marks_from": "15:50", "writes": ["proposal"]}
        proposed = []
        for i in range(3):
            ctx = _ctx(conn, "monitor", spec, NOW + dt.timedelta(minutes=5 * i), exit_settings())
            proposed.append(monitor(ctx, _env(held)).metrics["exits_proposed"])
        assert proposed == [1, 0, 0]
        n = conn.execute("SELECT COUNT(*) FROM proposals WHERE kind = 'close'").fetchone()[0]
        assert n == 1

    @pytest.mark.parametrize("roots", [0, 1, 8, 15])
    def test_broker_requests_stay_under_alpaca_basic_limit(self, roots: int) -> None:
        """E5.3a: one run per 5 min; even 3 overlapping jobs in a minute stay under 200/min."""
        from arc.routines.monitor import ALPACA_BASIC_REQ_PER_MIN, broker_requests

        assert broker_requests(roots) == 2 + 4 * roots
        assert 3 * broker_requests(roots) < ALPACA_BASIC_REQ_PER_MIN
        assert 3 * broker_requests(_settings().max_open_positions) < ALPACA_BASIC_REQ_PER_MIN

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
             "--since", "2026-09-27T21:55", "--now", "2026-09-28T09:40"]
        )  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0
        assert "tick Sun 2026-09-27 22:00 EDT" in out
        assert "tick Mon 2026-09-28 09:40 EDT" in out
        assert "↳ Mon 09:40 quant.propose" in out  # D31: first loop slot
        assert "Mon 09:30 research" not in out
        assert out.rstrip().endswith("had work")

    def test_step_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            ["routines", "tick", "--dry-run", "--json", "--step", "30m",
             "--since", "2026-10-03T00:00", "--now", "2026-10-04T00:00"]
        )  # fmt: skip
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        jobs = [o["job"] for t in data["ticks"] for o in t["outcomes"]]
        # Saturday: only the overnight Scalp + StockedUp; no in-session jobs.
        assert jobs.count("scalp.overnight") == 1 and "scalp" not in jobs
        assert "rss" not in jobs and "research" not in jobs

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
        mod.VENV_WAIT_S = 0
        assert mod.main() == 2
        assert "not found" in capsys.readouterr().out

    def test_arc_appearing_during_wait_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mod = _load_tick_script(repo, monkeypatch, tmp_path)
        monkeypatch.setattr(mod.time, "sleep", lambda _s: _fake_arc(repo, "sys.exit(0)"))
        assert mod.main() == 0
        assert capsys.readouterr().out == ""

    def test_arc_vanishing_before_exec_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        _fake_arc(repo, "sys.exit(0)")
        mod = _load_tick_script(repo, monkeypatch, tmp_path)

        def gone(*_a: object, **_k: object) -> None:
            raise FileNotFoundError(str(mod.ARC))

        monkeypatch.setattr(mod.subprocess, "run", gone)
        assert mod.main() == 2
        assert "vanished" in capsys.readouterr().out

    @pytest.mark.parametrize(("points_home", "rc"), [(True, 0), (False, 4)])
    def test_editable_install_must_point_at_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        points_home: bool,
        rc: int,
    ) -> None:
        repo = tmp_path / "repo"
        _fake_arc(repo, "sys.exit(0)")
        site = repo / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        target = repo if points_home else tmp_path / "scratch-wt"
        (site / "_editable_impl_arc.pth").write_text(f"{target}\n")
        mod = _load_tick_script(repo, monkeypatch, tmp_path)
        assert mod.main() == rc
        out = capsys.readouterr().out
        assert ("scratch-wt" in out) is (not points_home)
        assert (repo / "data" / "logs" / "routines-tick.log").exists() is points_home

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
        # D52: on the clock; an `every 10m` interval re-anchors on each run's finish and drifts
        assert 'SCHEDULE="*/10 * * * *"' in text
        assert "--no-agent" in text and "--workdir" in text


def test_yaml_comment_overview_matches_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["tick"]["interval"] == "10m"  # D52
    assert set(raw["personas"]) - {
        "finnhub_context", "director_diversification", "quant_risk_loop", "scout_feed",
        "scalp_options_tape",
        "research_idea_pool", "research_compact_prompt", "exit_path",
    } == {
        "scout", "scalp", "scalp.overnight", "research", "monitor", "broker.reconcile", "scorecard",
        "broker",
        "positions.evaluate", "experiments.evaluate",
    }  # fmt: skip
    # D31: the loop's cadence and window are config; the loop knobs are one block.
    assert raw["personas"]["research"] == {
        **raw["personas"]["research"],
        "every": "10m", "window": "09:40-15:50", "days": "trading", "ttl": "5m",
    }  # fmt: skip
    assert raw["loop"]["job"] == "research" and raw["loop"]["max_runtime"] == "4m"
    assert raw["monitoring"]["stuck_after_jobs"]["research"] == "20m"  # D52: two 10-min slots
    assert raw["triggers"] == []
