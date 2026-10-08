"""Routine dispatcher (E5.4, D16): config, due-time math, chains, triggers, catch-up, halt."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest
import structlog
import yaml
from freezegun import freeze_time
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.cli import main
from arc.context import ContextStore
from arc.llm_routing import LLMRouting, Persona, TierSpec
from arc.routines.conditions import ConditionError, evaluate_condition, parse_condition
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    Days,
    JobKind,
    JobSpec,
    RoutinesConfig,
    load_routines,
)
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    JobContext,
    JobResult,
    JobSkippedError,
    import_handler,
    not_implemented,
    resolve_handler,
    youtube_url,
)
from arc.routines.heartbeat import Heartbeats, RecordingNotifier, label_for
from arc.routines.locks import LLM_LOCK, LockBusyError, LockManager
from arc.routines.runs import RoutineRun, RoutineRunRepo, RunStatus
from arc.routines.schedule import (
    catchup_deadline,
    day_matches,
    next_slot,
    slots_between,
    wall_clock,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path


def et(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ET)


def job(**kw: object) -> JobSpec:
    return JobSpec.model_validate(kw)


def cfg(text: str) -> RoutinesConfig:
    return RoutinesConfig.model_validate(yaml.safe_load(textwrap.dedent(text)))


BASE_YAML = """
    sources:
      rss: {every: 30m, window: "06:00-20:00", days: trading}
    personas:
      scalp: {schedule: ["22:00", "12:00"], days: daily, after_sources: true, ttl: 3h}
      research: {schedule: ["09:00"], days: trading,
                 chain: [quant.open, risk.open, quant.propose], ttl: 2h,
                 writes: [shortlist]}
      broker.reconcile: {schedule: ["16:30"], days: trading, halt_exempt: true, ttl: 6h}
      broker: {trigger: approval}
    triggers:
      - on: scalp.completed
        if: "new_candidates > 0 and session == 'open'"
        run: research
"""


class Recorder:
    """Handler factory that records call order and each call's snapshot."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.snapshots: dict[str, list[str]] = {}
        self.fail: set[str] = set()
        self.skip: set[str] = set()
        self.metrics: dict[str, dict[str, object]] = {}

    def __call__(self, name: str) -> Callable[[JobContext], JobResult]:
        def handler(ctx: JobContext) -> JobResult:
            self.calls.append(name)
            self.snapshots[name] = [e.kind for e in ctx.snapshot.entries]
            if name in self.fail:
                raise RuntimeError(f"{name} boom")
            if name in self.skip:
                raise JobSkippedError("not today")
            if name == "research":
                ctx.write("shortlist", "market", _shortlist())
            return JobResult(summary=f"{name} done", metrics=self.metrics.get(name, {}))

        return handler

    def handlers(self, names: list[str]) -> dict[str, Callable[[JobContext], JobResult]]:
        return {n: self(n) for n in names}


def _shortlist() -> dict[str, object]:
    return {
        "shortlist": [],
        "market_regime": "risk_on",
        "session_notes": "quiet",
    }


# D39: every persona on an on-device model, so persona jobs take the global LLM lock.
LOCAL_ROUTING = LLMRouting(
    tiers={"local": TierSpec(model="ollama/qwen3", local=True)},
    personas={p: "local" for p in Persona},
)

ALL = [
    "rss",
    "scalp",
    "research",
    "quant.open",
    "risk.open",
    "quant.propose",
    "broker.reconcile",
    "broker",
]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def make(
    conn: sqlite3.Connection,
    text: str = BASE_YAML,
    *,
    halted: bool = False,
    rec: Recorder | None = None,
    notifier: RecordingNotifier | None = None,
) -> tuple[Dispatcher, Recorder, RecordingNotifier]:
    rec = rec or Recorder()
    notifier = notifier or RecordingNotifier()
    state = {"halted": halted}
    d = Dispatcher(
        conn,
        cfg(text),
        handlers=rec.handlers(ALL),
        notifier=notifier,
        is_halted=lambda: state["halted"],
    )
    d._halt_state = state  # type: ignore[attr-defined]
    return d, rec, notifier


def runs(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (r["job"], r["status"], r["scheduled_for"])
        for r in conn.execute("SELECT * FROM routine_runs ORDER BY rowid").fetchall()
    ]


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_config_validates(self) -> None:
        c = load_routines(DEFAULT_ROUTINES_PATH)
        assert c.personas["scalp"].after_sources
        assert c.personas["research"].chain == [
            "exits.mandatory",
            "quant.exit",
            "risk.exit",
            "quant.open",
            "risk.open",
            "quant.revise",
            "quant.propose",
            "broker.execute",
        ]
        # D31: the trading loop replaces the scalp.completed -> research trigger.
        assert c.triggers_for("scalp.completed") == []
        assert c.is_loop("research") and not c.is_loop("scalp")
        assert c.personas["research"].every == dt.timedelta(minutes=10)  # D52
        assert str(c.personas["research"].window) == "09:40-15:50"
        assert c.personas["research"].ttl is not None
        assert c.personas["research"].ttl.duration == dt.timedelta(minutes=5)
        assert c.personas["scalp"].every == dt.timedelta(minutes=30)
        assert c.personas["scalp.overnight"].schedule == [dt.time(22, 0)]
        assert c.sources["youtube.briefs"].schedule == [dt.time(2, 0)]  # D45 / E4.6 (02:00 ET)
        assert c.monitoring.stuck_after_for("research") == dt.timedelta(minutes=20)
        assert c.loop.max_idle == dt.timedelta(minutes=30)
        assert c.loop.max_runtime == dt.timedelta(minutes=7)  # D61
        assert {r.run for r in c.triggers_for("approval")} == {"broker"}
        yt = c.sources["youtube.briefs"]
        assert "youtube.stockedup" not in c.sources
        assert "category" not in yt.options and yt.options["lookback"] == "48h"  # D49, D60
        assert [(ch["slug"], ch["category"]) for ch in yt.options["channels"]] == [
            ("stockedup", "youtube_micro"),
            ("fxevolution", "youtube_macro"),
            ("tradebrigade", "youtube_micro"),
            ("arete", "youtube_micro"),
            ("bravos", "youtube_macro"),
            ("warrior", "youtube_micro"),  # D60
            ("ibd", "youtube_micro"),  # D60
        ]
        brief_ttl = c.context_policy("channel_brief", "youtube.briefs").ttl
        assert brief_ttl is not None and brief_ttl.duration == dt.timedelta(hours=48)  # D60

    def test_card_example_parses(self) -> None:
        c = cfg(
            """
            timezone: America/New_York
            sources:
              youtube.stockedup: {schedule: ["22:00", "12:00"], days: daily}
              rss:               {every: 30m, window: "06:00-20:00", days: trading}
              edgar:             {every: 15m, window: "06:00-20:00", days: trading}
              earnings:          {schedule: ["06:00", "18:00"], days: trading}
            personas:
              scalp:    {schedule: ["22:00", "12:00"], days: daily, after_sources: true}
              research: {schedule: ["09:00"], days: trading,
                         chain: [quant.open, risk.open, quant.propose]}
              broker.reconcile: {schedule: ["16:30"], days: trading}
              broker: {trigger: approval}
            triggers:
              - on: scalp.completed
                if: "new_candidates > 0 and session == 'open'"
                run: research
            """
        )
        assert len(c.jobs()) == 8

    def test_extra_filters_are_kept_as_options(self) -> None:
        c = cfg("sources: {edgar: {every: 5m, only_for: open_positions, tickers: [SPY]}}")
        assert c.sources["edgar"].options == {"only_for": "open_positions", "tickers": ["SPY"]}

    @pytest.mark.parametrize(
        "bad",
        [
            "sources: {rss: {days: daily}}",  # no cadence
            "sources: {rss: {every: 5m, schedule: ['01:00']}}",  # two cadences
            "sources: {rss: {schedule: ['25:00']}}",
            "sources: {rss: {schedule: ['01:00', '01:00']}}",
            "sources: {rss: {every: 0m}}",
            "sources: {rss: {schedule: ['01:00'], window: '01:00-02:00'}}",
            "sources: {rss: {every: 5m, window: '10:00-09:00'}}",
            "sources: {rss: {every: 5m, window: '10:00'}}",
            "sources: {rss: {every: 5m, days: sometimes}}",
            "sources: {rss: {every: 5m, chain: [x]}}",  # chain is persona-only
            "sources: {Rss: {every: 5m}}",  # bad name
            "sources: {rss: {every: 5m}}\npersonas: {rss: {every: 5m}}",  # dup name
            "personas: {d: {schedule: ['09:00'], chain: [d]}}",  # chain repeats itself
            "personas: {d: {schedule: ['09:00'], chain: [q, q]}}",
            "sources: {rss: {every: 5m}}\npersonas: {d: {schedule: ['09:00'], chain: [rss]}}",
            "personas: {d: {schedule: ['09:00'], chain: [Bad]}}",
            "personas: {a: {schedule: ['09:00'], halt_exempt: true, chain: [q]}}",
            "personas: {d: {schedule: ['09:00'], reads: [gossip]}}",
            "personas: {d: {schedule: ['09:00'], handler: 'not a path'}}",
            "context_ttl: {gossip: {ttl: 1h}}",
            "personas: {d: {schedule: ['09:00']}}\ntriggers: [{on: x, run: nobody}]",
            "personas: {d: {schedule: ['09:00']}}\ntriggers: [{on: ghost.completed, run: d}]",
            "personas: {d: {schedule: ['09:00']}}\ntriggers: [{on: x, run: d, if: 'f()'}]",
            "personas: {d: {schedule: ['09:00']}}\ntriggers: [{on: x, run: d, if: 'a >'}]",
            "timezone: UTC",
            "unknown_top_level: 1",
            "steps: {Bad: {}}",
            "tick: {max_trigger_depth: 0}",
        ],
    )
    def test_invalid_configs_rejected(self, bad: str) -> None:
        with pytest.raises((ValidationError, ValueError)):
            cfg(bad)

    def test_writes_parse(self) -> None:
        c = cfg("personas: {d: {schedule: ['09:00'], writes: [shortlist, note]}}")
        assert c.personas["d"].writes == ["shortlist", "note"]
        assert cfg("steps: {quant.propose: {writes: []}}").steps["quant.propose"].writes == []
        assert cfg("personas: {d: {schedule: ['09:00']}}").personas["d"].writes is None

    def test_unknown_write_kind_message(self) -> None:
        with pytest.raises(ValidationError, match="unknown context kind 'bogus' in writes"):
            cfg("personas: {d: {schedule: ['09:00'], writes: [bogus]}}")
        with pytest.raises(ValidationError, match="unknown context kind 'bogus' in reads"):
            cfg("personas: {d: {schedule: ['09:00'], reads: [bogus]}}")
        with pytest.raises(ValidationError, match="writes lists a kind twice"):
            cfg("personas: {d: {schedule: ['09:00'], writes: [note, note]}}")

    def test_chain_step_completion_is_a_valid_trigger(self) -> None:
        c = cfg(
            """
            personas:
              research: {schedule: ["09:00"], chain: [quant.open]}
              broker.reconcile: {schedule: ["16:30"]}
            triggers: [{on: quant.open.completed, run: broker.reconcile}]
            """
        )
        assert c.triggers_for("quant.open.completed")[0].run == "broker.reconcile"

    def test_step_and_context_policy_lookup(self) -> None:
        c = cfg(
            """
            context_ttl: {shortlist: {ttl: 1 session}}
            personas:
              research: {schedule: ["09:00"], chain: [quant.propose]}
            steps:
              quant.propose: {context: {ttl: 20m, supersede: accumulate}, llm: false}
            """
        )
        assert str(c.context_policy("shortlist", "research").ttl) == "1 session"
        assert str(c.context_policy("proposal", "quant.propose").ttl) == "20m"
        assert c.context_policy("regime").ttl is None
        assert c.step("quant.propose")[1].llm is False
        assert c.step("quant.open")[0] is JobKind.PERSONA
        assert c.job("nobody") is None

    def test_disabled_jobs_are_ignored(self) -> None:
        c = cfg("sources: {rss: {every: 5m, enabled: false}}")
        assert c.jobs() == {}

    def test_load_rejects_non_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "r.yaml"
        p.write_text("- 1\n- 2\n")
        with pytest.raises(ValueError, match="mapping"):
            load_routines(p)
        p.write_text("")
        assert load_routines(p).jobs() == {}

    def test_cadence_text(self) -> None:
        assert job(schedule=["12:00", "09:00"]).cadence == "at 09:00, 12:00 ET (daily)"
        assert job(every="2h").cadence == "every 2h ET (daily)"
        assert job(every="15m", window="06:00-20:00", days="trading").cadence == (
            "every 15m 06:00-20:00 ET (trading)"
        )
        assert job(trigger="approval").cadence == "on approval"


# ---------------------------------------------------------------------------
# Due-time math
# ---------------------------------------------------------------------------


class TestSchedule:
    def test_days_filter(self) -> None:
        sat, mon, holiday = dt.date(2026, 9, 26), dt.date(2026, 9, 28), dt.date(2026, 11, 26)
        assert day_matches(Days.DAILY, sat)
        assert not day_matches(Days.WEEKDAYS, sat) and day_matches(Days.WEEKDAYS, holiday)
        assert day_matches(Days.TRADING, mon) and not day_matches(Days.TRADING, holiday)
        assert not day_matches(Days.TRADING, sat)

    def test_trading_day_filter_skips_weekend_and_holiday(self) -> None:
        spec = job(schedule=["09:00"], days="trading")
        # Wed 11-25 .. Mon 11-30 2026: Thanksgiving Thu closed, Fri early close is a session.
        slots = slots_between(spec, et(2026, 11, 25, 0, 0), et(2026, 11, 30, 23, 0))
        assert [s.date() for s in slots] == [
            dt.date(2026, 11, 25),
            dt.date(2026, 11, 27),
            dt.date(2026, 11, 30),
        ]

    def test_every_with_window(self) -> None:
        spec = job(every="30m", window="06:00-20:00", days="trading")
        slots = slots_between(spec, et(2026, 9, 28, 0, 0), et(2026, 9, 28, 23, 59))
        assert slots[0] == et(2026, 9, 28, 6, 0)
        assert slots[-1] == et(2026, 9, 28, 20, 0)  # window end is inclusive
        assert len(slots) == 29
        assert all(
            b - a == dt.timedelta(minutes=30) for a, b in zip(slots, slots[1:], strict=False)
        )

    def test_every_without_window_is_midnight_anchored(self) -> None:
        spec = job(every="6h")
        slots = slots_between(spec, et(2026, 9, 27, 23, 0), et(2026, 9, 28, 23, 0))
        assert [s.hour for s in slots] == [0, 6, 12, 18]

    def test_interval_is_half_open(self) -> None:
        spec = job(schedule=["12:00"])
        assert slots_between(spec, et(2026, 9, 28, 12, 0), et(2026, 9, 28, 13, 0)) == []
        assert slots_between(spec, et(2026, 9, 28, 11, 0), et(2026, 9, 28, 12, 0)) == [
            et(2026, 9, 28, 12, 0)
        ]
        assert slots_between(spec, et(2026, 9, 28, 13, 0), et(2026, 9, 28, 12, 0)) == []

    def test_dst_spring_forward_wall_clock(self) -> None:
        spec = job(schedule=["22:00"])
        # 2026-03-08 is spring-forward. 22:00 stays 22:00 local on both sides.
        slots = slots_between(spec, et(2026, 3, 7, 0, 0), et(2026, 3, 9, 0, 0))
        assert [s.hour for s in slots] == [22, 22]
        assert [s.utcoffset() for s in slots] == [dt.timedelta(hours=-5), dt.timedelta(hours=-4)]

    def test_dst_gap_time_runs_once_shifted_forward(self) -> None:
        spec = job(schedule=["02:30"])
        slots = slots_between(spec, et(2026, 3, 8, 0, 0), et(2026, 3, 8, 12, 0))
        assert len(slots) == 1
        assert (slots[0].hour, slots[0].minute) == (3, 30)
        assert wall_clock(dt.date(2026, 3, 8), dt.time(2, 30)) == slots[0]

    def test_dst_fall_back_repeated_hour_runs_once(self) -> None:
        spec = job(schedule=["01:30"])
        # 2026-11-01 is fall-back: 01:30 happens twice; we run at the first one.
        slots = slots_between(spec, et(2026, 10, 31, 12, 0), et(2026, 11, 1, 12, 0))
        assert len(slots) == 1
        assert slots[0].utcoffset() == dt.timedelta(hours=-4)
        every = job(every="30m")
        day = slots_between(every, et(2026, 10, 31, 23, 59), et(2026, 11, 1, 23, 59))
        assert len(day) == 48  # wall-clock grid: one slot per half hour label
        assert len(set(day)) == 48

    def test_next_slot(self) -> None:
        spec = job(schedule=["09:00"], days="trading")
        assert next_slot(spec, et(2026, 9, 25, 10, 0)) == et(2026, 9, 28, 9, 0)  # Fri -> Mon
        assert next_slot(job(trigger="approval"), et(2026, 9, 25, 10, 0)) is None

    def test_catchup_deadline(self) -> None:
        assert catchup_deadline(job(schedule=["09:00"]), et(2026, 9, 28, 9, 0)) == et(
            2026, 9, 28, 11, 0
        )
        assert catchup_deadline(job(every="15m"), et(2026, 9, 28, 9, 0)) == et(2026, 9, 28, 9, 15)
        assert catchup_deadline(
            job(schedule=["22:00"], ttl="1 session"), et(2026, 9, 27, 22, 0)
        ) == (et(2026, 9, 28, 16, 0))

    @settings(max_examples=50, deadline=None)
    @given(
        start=st.datetimes(
            min_value=dt.datetime(2026, 1, 1),
            max_value=dt.datetime(2027, 6, 1),
            timezones=st.just(ET),
        ),
        hours=st.integers(min_value=1, max_value=24 * 10),
        minutes=st.sampled_from([5, 15, 30, 60]),
    )
    def test_slots_are_sorted_unique_in_range_and_in_window(
        self, start: dt.datetime, hours: int, minutes: int
    ) -> None:
        spec = job(every=f"{minutes}m", window="06:00-20:00", days="trading")
        end = start + dt.timedelta(hours=hours)
        slots = slots_between(spec, start, end)
        assert slots == sorted(set(slots))
        for s in slots:
            assert start < s <= end
            assert dt.time(6, 0) <= s.timetz().replace(tzinfo=None) <= dt.time(20, 0)
            assert day_matches(Days.TRADING, s.date())


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


class TestConditions:
    def test_card_condition(self) -> None:
        cond = "new_candidates > 0 and session == 'open'"
        assert evaluate_condition(cond, {"new_candidates": 2, "session": "open"})
        assert not evaluate_condition(cond, {"new_candidates": 0, "session": "open"})
        assert not evaluate_condition(cond, {"new_candidates": 3, "session": "post"})
        assert not evaluate_condition(cond, {"session": "open"})  # missing metric -> false

    def test_operators(self) -> None:
        env = {"a": 3, "s": "open", "n": None}
        assert evaluate_condition(None, env) and evaluate_condition("", env)
        assert evaluate_condition("not a < 2 or a == 0", env)
        assert evaluate_condition("1 < a <= 3", env)
        assert not evaluate_condition("1 < a < 3", env)
        assert evaluate_condition("s in ('open', 'pre')", env)
        assert evaluate_condition("s not in ['post']", env)
        assert evaluate_condition("-a < 0 and a != 4 and a >= 3", env)
        assert not evaluate_condition("-n < 0", env)

    @pytest.mark.parametrize(
        "expr", ["__import__('os')", "a.b", "a[0]", "lambda: 1", "a + 1", "(a := 1)", "a >"]
    )
    def test_unsafe_rejected(self, expr: str) -> None:
        with pytest.raises(ConditionError):
            parse_condition(expr)


# ---------------------------------------------------------------------------
# Dispatcher behaviour
# ---------------------------------------------------------------------------


class TestTick:
    def test_sources_run_before_after_sources_persona(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        report = d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        assert rec.calls == ["rss", "scalp"]
        assert [o.status for o in report.outcomes] == ["ok", "ok"]

    def test_duplicate_tick_does_not_double_run(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        now = et(2026, 9, 28, 12, 0)
        d.tick(now, since=et(2026, 9, 28, 11, 55))
        report = d.tick(now, since=et(2026, 9, 28, 11, 55))  # e.g. cron fired twice
        assert rec.calls == ["rss", "scalp"]
        assert {o.status for o in report.outcomes} == {"duplicate"}
        # and a second dispatcher on the same DB (overlapping process) agrees
        d2, rec2, _ = make(conn)
        d2.tick(now, since=et(2026, 9, 28, 11, 55))
        assert rec2.calls == []
        assert len(runs(conn)) == 2

    def test_cursor_advances_between_ticks(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 11, 55))  # first ever tick: looks back one interval
        d.tick(et(2026, 9, 28, 12, 0))
        d.tick(et(2026, 9, 28, 12, 5))
        d.tick(et(2026, 9, 28, 12, 30))
        assert rec.calls == ["rss", "scalp", "rss"]

    def test_catchup_runs_missed_job_once(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 8, 0))
        rec.calls.clear()
        # Down from 08:00 to 10:40: 5 rss slots and research 09:00 were missed.
        report = d.tick(et(2026, 9, 28, 10, 40))
        assert rec.calls.count("rss") == 1
        assert rec.calls.count("research") == 1
        rss = next(o for o in report.outcomes if o.job == "rss")
        assert rss.scheduled_for == et(2026, 9, 28, 10, 30)
        assert "collapsed" in rss.summary
        assert [r for r in runs(conn) if r[0] == "rss"][-1][1] == "ok"

    def test_catchup_skips_outside_ttl_window(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 8, 0))
        rec.calls.clear()
        report = d.tick(et(2026, 9, 28, 11, 30))  # research ttl 2h -> window ended 11:00
        assert "research" not in rec.calls
        research = next(o for o in report.outcomes if o.job == "research")
        assert research.status == "skipped" and "missed" in research.reason
        assert ("research", "skipped", "2026-09-28T13:00:00.000000Z") in runs(conn)

    def test_catchup_after_long_downtime_is_bounded(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 1, 8, 0))
        rec.calls.clear()
        d.tick(et(2026, 9, 28, 12, 0))  # 4 weeks down; lookback 7d, each job once at most
        assert sorted(rec.calls) == sorted(set(rec.calls))

    def test_halt_skips_personas_but_not_sources_or_auditor(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn, halted=True)
        d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        d.tick(et(2026, 9, 28, 16, 30), since=et(2026, 9, 28, 16, 25))
        assert rec.calls == ["rss", "rss", "broker.reconcile"]  # rss at 12:00 and 16:30
        assert ("scalp", "skipped", "2026-09-28T16:00:00.000000Z") in runs(conn)

    def test_halt_blocks_triggered_persona(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        rec.metrics["scalp"] = {"new_candidates": 2}
        d._halt_state["halted"] = False  # type: ignore[attr-defined]
        original = d._fire

        def fire_halted(*a: object, **k: object) -> object:
            d._halt_state["halted"] = True  # type: ignore[attr-defined]
            return original(*a, **k)  # type: ignore[arg-type]

        d._fire = fire_halted  # type: ignore[method-assign]
        report = d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        assert "research" not in rec.calls
        assert any(o.job == "research" and o.status == "skipped" for o in report.outcomes)

    def test_failures_are_recorded_and_alerted(self, conn: sqlite3.Connection) -> None:
        d, rec, notes = make(conn)
        rec.fail.add("rss")
        report = d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        rss = next(o for o in report.outcomes if o.job == "rss")
        assert rss.status == "failed" and "boom" in rss.summary
        assert rec.calls == ["rss", "scalp"]  # one failed job doesn't stop the tick
        assert any("FAILED" in text and "rss" in text for _, text in notes.posts)
        row = RoutineRunRepo(conn).get(rss.run_id or "")
        assert row is not None and row.error and "boom" in row.error

    def test_handler_skip_and_bad_return(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        rec.skip.add("scalp")
        d.handlers["rss"] = lambda ctx: "nope"  # type: ignore[assignment,return-value]
        report = d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        by_job = {o.job: o for o in report.outcomes}
        assert by_job["scalp"].status == "skipped"
        assert by_job["rss"].status == "failed" and "JobResult" in by_job["rss"].summary

    def test_unimplemented_persona_is_skipped_not_failed(self, conn: sqlite3.Connection) -> None:
        yaml_text = BASE_YAML.replace(
            "broker: {trigger: approval}",
            'broker: {trigger: approval}\n      lessons: {schedule: ["16:45"], days: [fri]}',
        )
        d = Dispatcher(conn, cfg(yaml_text), notifier=RecordingNotifier(), is_halted=lambda: False)
        outcomes = d.run_job(
            "lessons", et(2026, 10, 2, 16, 45), reason="manual", now=et(2026, 10, 2, 16, 45)
        )
        assert [o.status for o in outcomes] == ["skipped"]  # "lessons" has no handler

    def test_default_halt_reads_halts_table(self, conn: sqlite3.Connection) -> None:
        from arc.store.repos import HaltRepo

        d = Dispatcher(conn, cfg(BASE_YAML), handlers=Recorder().handlers(ALL))
        assert not d._is_halted()
        HaltRepo(conn).halt(reason="test")
        assert d._is_halted()


class TestChains:
    def test_chain_order_and_shared_chain_id(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        assert rec.calls == ["rss", "research", "quant.open", "risk.open", "quant.propose"]
        rows = conn.execute(
            "SELECT job, chain_run_id, step_index FROM routine_runs"
            " WHERE chain_run_id IS NOT NULL ORDER BY step_index"
        ).fetchall()
        assert [r["job"] for r in rows] == ["research", "quant.open", "risk.open", "quant.propose"]
        assert len({r["chain_run_id"] for r in rows}) == 1
        # each step reads the context written by the previous one
        assert rec.snapshots["research"] == []
        assert rec.snapshots["quant.open"] == ["shortlist"]

    def test_failed_step_stops_chain_then_resume(self, conn: sqlite3.Connection) -> None:
        d, rec, notes = make(conn)
        rec.fail.add("risk.open")
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        assert rec.calls == ["rss", "research", "quant.open", "risk.open"]
        assert any("risk.open FAILED" in t for _, t in notes.posts)
        chain_id = RoutineRunRepo(conn).latest_failed_chain("research", dt.date(2026, 9, 28))
        assert chain_id is not None

        rec.calls.clear()
        rec.fail.clear()
        outcomes = d.run_manual("research", now=et(2026, 9, 28, 9, 20), chain=True)
        assert rec.calls == ["risk.open", "quant.propose"]  # research + quant are not re-run
        assert [o.status for o in outcomes] == ["ok", "ok", "ok", "ok"]
        steps = RoutineRunRepo(conn).chain(chain_id)
        assert [s.status for s in steps] == [RunStatus.OK] * 4
        assert steps[2].attempts == 2
        assert RoutineRunRepo(conn).latest_failed_chain("research", dt.date(2026, 9, 28)) is None

    def test_resume_is_idempotent_per_step(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        chain_id = conn.execute(
            "SELECT chain_run_id FROM routine_runs WHERE chain_run_id IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        assert RoutineRunRepo(conn).chain(chain_id)
        rec.calls.clear()
        d.resume_chain(chain_id, now=et(2026, 9, 28, 9, 30))
        assert rec.calls == []

    def test_manual_run_and_errors(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.run_manual("scalp", now=et(2026, 9, 28, 13, 7))
        assert rec.calls == ["rss", "scalp"]  # after_sources
        rec.calls.clear()
        d.run_manual("research", now=et(2026, 9, 28, 13, 8), chain=True, fresh=True)
        assert rec.calls == ["research", "quant.open", "risk.open", "quant.propose"]
        with pytest.raises(KeyError):
            d.run_manual("nobody", now=et(2026, 9, 28, 13, 9))
        with pytest.raises(KeyError):
            d.resume_chain("chain-nope", now=et(2026, 9, 28, 13, 9))
        with pytest.raises(KeyError):
            d.run_job("nobody", et(2026, 9, 28, 13, 9), reason="x", now=et(2026, 9, 28, 13, 9))


class TestTriggers:
    def test_trigger_condition_true_runs_research_chain(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        rec.metrics["scalp"] = {"new_candidates": 2}
        report = d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        assert rec.calls == ["rss", "scalp", "research", "quant.open", "risk.open", "quant.propose"]
        research = next(o for o in report.outcomes if o.job == "research")
        assert research.reason == "event:scalp.completed"

    def test_trigger_condition_false(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        rec.metrics["scalp"] = {"new_candidates": 0}
        d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        assert "research" not in rec.calls
        # candidates but market closed (22:00 run) -> no research
        rec.metrics["scalp"] = {"new_candidates": 5}
        d.tick(et(2026, 9, 28, 22, 0), since=et(2026, 9, 28, 21, 55))
        assert "research" not in rec.calls

    def test_external_event_runs_investor_once(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.events.emit("approval", {"proposal_id": "p1"}, now=et(2026, 9, 28, 10, 1))
        d.tick(et(2026, 9, 28, 10, 5), since=et(2026, 9, 28, 10, 4))
        d.tick(et(2026, 9, 28, 10, 10), since=et(2026, 9, 28, 10, 9))
        assert rec.calls.count("broker") == 1
        row = conn.execute("SELECT consumed_by FROM routine_events").fetchone()
        assert len(json.loads(row["consumed_by"])) == 1

    def test_trigger_depth_is_bounded(self, conn: sqlite3.Connection) -> None:
        text = """
            tick: {max_trigger_depth: 2}
            personas:
              a: {schedule: ["09:00"]}
              b: {trigger: a.completed}
              c: {trigger: b.completed}
              e: {trigger: c.completed}
        """
        rec = Recorder()
        d = Dispatcher(
            conn,
            cfg(text),
            handlers=rec.handlers(["a", "b", "c", "e"]),
            notifier=RecordingNotifier(),
            is_halted=lambda: False,
        )
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        assert rec.calls == ["a", "b", "c"]


class TestContextIntegration:
    def test_snapshot_id_recorded_on_run(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        repo = RoutineRunRepo(conn)
        by_job = {r.job: r for r in repo.history(limit=10)}
        store = ContextStore(conn)
        quant = by_job["quant.open"]
        assert len(quant.inputs_snapshot) == 1
        snap = store.load_snapshot(quant.inputs_snapshot[0])
        assert [e.kind for e in snap.entries] == ["shortlist"]
        assert snap.entries[0].produced_by == "research"
        research = by_job["research"]
        assert research.outputs == [snap.entries[0].id]
        assert snap.entries[0].chain_run_id == research.chain_run_id

    def test_sources_do_not_record_snapshots(self, conn: sqlite3.Connection) -> None:
        d, _, _ = make(conn)
        d.tick(et(2026, 9, 28, 11, 30), since=et(2026, 9, 28, 11, 25))
        rss = RoutineRunRepo(conn).history(job="rss")[0]
        assert rss.inputs_snapshot == []

    def test_persona_reads_only_configured_kinds(self, conn: sqlite3.Connection) -> None:
        text = BASE_YAML.replace(
            "                 writes: [shortlist]}",
            "                 writes: [shortlist]}\n"
            "    steps:\n      quant.open: {reads: [candidate]}",
        )
        c = cfg(text)
        assert c.steps["quant.open"].reads == ["candidate"]
        d, rec, _ = make(conn, text)
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        assert rec.snapshots["quant.open"] == []  # shortlist exists but quant only reads candidates

    def test_entries_get_producer_ttl_from_config(self, conn: sqlite3.Connection) -> None:
        text = BASE_YAML + "\n    context_ttl: {shortlist: {ttl: 1 session}}\n"
        d, _, _ = make(conn, text)
        d.tick(et(2026, 9, 28, 9, 0), since=et(2026, 9, 28, 8, 55))
        entry = ContextStore(conn).query(as_of=et(2026, 9, 28, 9, 1), kinds=["shortlist"])[0]
        assert entry.expires_at == et(2026, 9, 28, 16, 0)
        assert entry.produced_by == "research"


class TestWriteContract:
    """D27: undeclared writes fail the run (fail-closed)."""

    def _run(self, conn: sqlite3.Connection, writes: str | None) -> tuple[RoutineRun, list[Any]]:
        extra = f", writes: {writes}" if writes is not None else ""
        text = f"personas:\n  research: {{schedule: ['09:00']{extra}}}\n"
        d = Dispatcher(
            conn,
            cfg(text),
            handlers={"research": Recorder()("research")},
            notifier=RecordingNotifier(),
            is_halted=lambda: False,
        )
        with structlog.testing.capture_logs() as logs:
            d.run_manual("research", now=et(2026, 9, 28, 9, 0))
        run = RoutineRunRepo(conn).history(job="research")[0]
        return run, logs

    def _shortlists(self, conn: sqlite3.Connection) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM context_entries WHERE kind='shortlist'"
        ).fetchone()[0]

    def test_undeclared_write_fails_job(self, conn: sqlite3.Connection) -> None:
        run, _ = self._run(conn, "[candidate]")
        assert run.status is RunStatus.FAILED
        assert run.error is not None and "writes" in run.error and "shortlist" in run.error
        assert self._shortlists(conn) == 0

    def test_declared_write_ok(self, conn: sqlite3.Connection) -> None:
        run, _ = self._run(conn, "[shortlist]")
        assert run.status is RunStatus.OK
        assert self._shortlists(conn) == 1

    def test_writes_none_fails(self, conn: sqlite3.Connection) -> None:
        from arc.routines.manifest import ManifestRepo

        run, _ = self._run(conn, None)
        assert run.status is RunStatus.FAILED
        assert run.error is not None and run.error.startswith("ContractViolationError")
        manifest = ManifestRepo(conn).latest(run.run_id)
        assert manifest is not None
        assert manifest.error_class == "ContractViolationError"
        assert manifest.declared_writes is None

    def test_empty_writes_declares_nothing(self, conn: sqlite3.Connection) -> None:
        run, _ = self._run(conn, "[]")
        assert run.status is RunStatus.FAILED and self._shortlists(conn) == 0

    def test_violation_logged(self, conn: sqlite3.Connection) -> None:
        _, logs = self._run(conn, "[candidate]")
        rejected = [e for e in logs if e["event"] == "context.write_rejected"]
        assert rejected and rejected[0]["job"] == "research"
        assert rejected[0]["kind"] == "shortlist" and rejected[0]["declared"] == ["candidate"]
        assert rejected[0]["log_level"] == "error"


class TestConfigDriven:
    def test_every_unit_declares_io(self) -> None:
        """D27: every job and chain step in the shipped config declares its writes."""
        c = load_routines(DEFAULT_ROUTINES_PATH)
        units = set(c.jobs()) | {s for p in c.personas.values() for s in p.chain} | set(c.steps)
        missing = sorted(u for u in units if c.step(u)[1].writes is None)
        assert missing == [], f"declare `writes:` for {missing} in config/routines.yaml"

    def test_research_reads_match_config(self) -> None:
        from arc.pipeline.steps import RESEARCH_READS

        assert load_routines(DEFAULT_ROUTINES_PATH).personas["research"].reads == RESEARCH_READS

    def test_new_source_and_persona_need_no_code(self, conn: sqlite3.Connection) -> None:
        """Adding YAML entries is enough: built-in handlers resolve by name prefix."""
        text = BASE_YAML.replace(
            "sources:\n",
            "sources:\n      youtube.macrochannel: {schedule: ['07:00'], channel: UCabc}\n",
        ).replace(
            "personas:\n",
            "personas:\n"
            "      macro_watch: {schedule: ['07:00'], handler: 'tests.test_routines:_ext'}\n",
        )
        c = cfg(text)
        assert resolve_handler("youtube.macrochannel", c.sources["youtube.macrochannel"]) is (
            import_handler(BUILTIN_HANDLERS["youtube"])
        )
        seen: list[str] = []
        d = Dispatcher(
            conn,
            c,
            handlers={"youtube": lambda ctx: seen.append(ctx.options["channel"]) or JobResult()},
            notifier=RecordingNotifier(),
            is_halted=lambda: False,
        )
        d.tick(et(2026, 9, 28, 7, 0), since=et(2026, 9, 28, 6, 55))
        assert seen == ["UCabc"]
        assert _EXT_CALLS == ["macro_watch"]

    def test_handler_resolution(self) -> None:
        spec = JobSpec.model_validate({"every": "5m"})
        assert resolve_handler("lessons", spec) is not_implemented
        assert resolve_handler("scorecard", spec).__name__ == "scorecard_step"
        assert resolve_handler("broker.reconcile", spec).__name__ == "broker_reconcile_step"
        assert resolve_handler("quant.open", spec).__name__ == "quant_open_step"
        assert resolve_handler("quant.propose", spec).__name__ == "quant_propose_step"
        assert resolve_handler("propose", spec) is not_implemented  # E13.15: alias gone
        assert resolve_handler("rss", spec).__name__ == "rss_source"
        assert resolve_handler("edgar.filings", spec).__name__ == "edgar_source"
        with pytest.raises(TypeError):
            import_handler("arc.routines.handlers:BUILTIN_HANDLERS")
        assert youtube_url("UCx") == "https://www.youtube.com/channel/UCx/videos"
        assert youtube_url("https://y/x") == "https://y/x"


_EXT_CALLS: list[str] = []


def _ext(ctx: JobContext) -> JobResult:
    _EXT_CALLS.append(ctx.job)
    return JobResult(summary="ext")


class TestHeartbeats:
    def test_sources_quiet_personas_summarise(self, conn: sqlite3.Connection) -> None:
        d, _, notes = make(conn)
        d.tick(et(2026, 9, 28, 11, 30), since=et(2026, 9, 28, 11, 25))
        assert notes.posts == []  # rss alone: quiet
        d.tick(et(2026, 9, 28, 12, 0), since=et(2026, 9, 28, 11, 55))
        assert len(notes.posts) == 1
        text = notes.posts[0][1]
        assert text.startswith("⚡ [Scalp] scalp ✓")
        assert "rss ×2 (last: rss done)" in text  # E5.3: repeated source runs fold into one
        assert notes.posts[0][0] == dt.date(2026, 9, 28)

    def test_labels(self) -> None:
        assert label_for("research") == "🧠 [Research]"
        assert label_for("youtube.stockedup") == "[Routines]"

    @staticmethod
    def _card_dispatcher(
        conn: sqlite3.Connection, scalp_notify: str | None
    ) -> tuple[Dispatcher, RecordingNotifier]:
        from arc.slack.blocks import CardView, header

        extra = f", notify: {scalp_notify}" if scalp_notify else ""
        text = f"""
            sources:
              rss: {{every: 30m}}
            personas:
              scalp: {{schedule: ["12:00"]{extra}}}
        """
        view = CardView(text="⚡ [Scalp] Scan: 1 doc → 1 candidate", blocks=[header("card")])
        notes = RecordingNotifier()
        d = Dispatcher(
            conn,
            cfg(text),
            handlers={
                "rss": lambda ctx: JobResult(summary="1 new doc"),
                "scalp": lambda ctx: JobResult(summary="1 docs → 1 accepted", card=view),
            },
            notifier=notes,
            is_halted=lambda: False,
        )
        return d, notes

    @pytest.mark.parametrize(
        ("notify", "posts", "has_blocks"),
        [(None, 1, True), ("card", 1, True), ("summary", 1, False), ("quiet", 0, False)],
    )
    def test_notify_knob_is_config_only(
        self, conn: sqlite3.Connection, notify: str | None, posts: int, has_blocks: bool
    ) -> None:
        """``notify:`` in routines.yaml alone switches card / one-liner / quiet (E5.5)."""
        d, notes = self._card_dispatcher(conn, notify)
        d.run_manual("scalp", now=et(2026, 9, 28, 12, 0))
        assert len(notes.posts) == posts
        if posts:
            # The fallback text is the unchanged one-liner in every mode.
            assert notes.posts[0][1] == "⚡ [Scalp] scalp ✓ 1 docs → 1 accepted"
            assert (notes.blocks[0] is not None) is has_blocks

    def test_card_default_without_a_card_falls_back_to_one_liner(
        self, conn: sqlite3.Connection
    ) -> None:
        d, _, notes = make(conn)
        d.run_manual("broker.reconcile", now=et(2026, 9, 28, 16, 30))
        assert notes.posts[0][1] == "🏦 [Broker] broker.reconcile ✓ broker.reconcile done"
        assert notes.blocks == [None]

    def test_card_folds_pending_sources(self, conn: sqlite3.Connection) -> None:
        d, notes = self._card_dispatcher(conn, None)
        Heartbeats(conn, notes).queue_source("rss", "3 new docs")
        d.run_manual("scalp", now=et(2026, 9, 28, 12, 0))
        blocks = notes.blocks[0]
        assert blocks is not None
        # E5.5b: the folded line is a ⚡ [Scalp] Session notes section before the
        # footer; the footer stays last. The card fixture has no footer, so the
        # section is simply the last block here.
        assert blocks[-1]["text"]["text"] == (
            "*⚡ [Scalp] Session notes*\nsources since last update: rss: 3 new docs"
        )
        assert notes.posts[0][1].endswith("\n> sources since last update: rss: 3 new docs")

    def test_failures_keep_one_line_alert(self, conn: sqlite3.Connection) -> None:
        d, rec, notes = make(conn)
        rec.fail.add("broker.reconcile")
        (o,) = d.run_manual("broker.reconcile", now=et(2026, 9, 28, 16, 30))
        assert notes.posts[0][1] == (
            ":rotating_light: 🏦 [Broker] broker.reconcile FAILED: "
            f"RuntimeError: broker.reconcile boom `{o.run_id}`"
        )
        assert notes.blocks == [None]

    def test_notify_card_validates(self) -> None:
        assert job(every="5m", notify="card").notify == "card"
        with pytest.raises(ValidationError):
            job(every="5m", notify="loud")

    def test_shipped_config_cards_for_personas(self) -> None:
        r = load_routines(DEFAULT_ROUTINES_PATH)
        for name in ("scalp", "research", "broker.reconcile", "broker", "quant.open", "risk.open"):
            assert r.step(name)[1].notify == "card", name
        assert r.step("quant.propose")[1].notify == "summary"  # E6.1 owns the proposal card
        assert all(s.notify in (None, "quiet") for s in r.sources.values())

    def test_pending_queue_is_bounded(self, conn: sqlite3.Connection) -> None:
        hb = Heartbeats(conn, RecordingNotifier())
        for i in range(60):
            hb.queue_source(f"src{i}", str(i))
        assert len(hb._pending()) == 50

    def test_slack_notifier_creates_day_thread_once(self, conn: sqlite3.Connection) -> None:
        from unittest import mock

        from arc.routines.heartbeat import SlackDayThreadNotifier
        from arc.slack.client import ArcSlackClient

        web = mock.MagicMock()
        web.chat_postMessage.side_effect = [{"ts": "111.1"}, {"ts": "b"}, {"ts": "2"}, {"ts": "3"}]
        n = SlackDayThreadNotifier(conn, ArcSlackClient(client=web))
        n.post(dt.date(2026, 9, 28), "one")
        n.post(dt.date(2026, 9, 28), "two", [{"type": "divider"}])
        calls = web.chat_postMessage.call_args_list
        assert len(calls) == 4  # root, D34 auto-approve banner, one, two
        assert calls[0].kwargs["text"] == "💡 Mon Sep 28 · Session Notes"
        assert calls[1].kwargs["text"].startswith("Auto-approve: ")
        assert calls[2].kwargs["thread_ts"] == "111.1" == calls[3].kwargs["thread_ts"]
        assert "blocks" not in calls[2].kwargs
        assert calls[3].kwargs["blocks"] == [{"type": "divider"}]
        assert calls[3].kwargs["text"] == "two"
        web.chat_postMessage.side_effect = RuntimeError("slack down")
        n.post(dt.date(2026, 9, 29), "never raises")


class TestLocks:
    def test_lock_busy_and_release(self, tmp_path: Path) -> None:
        a, b = LockManager(tmp_path), LockManager(tmp_path)
        with a.hold("scalp", LLM_LOCK):
            with pytest.raises(LockBusyError), b.hold("research", LLM_LOCK):
                pass
            with b.hold("rss"):
                pass
        with b.hold(LLM_LOCK):
            pass

    def test_busy_llm_lock_defers_without_recording(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        rec = Recorder()
        d = Dispatcher(
            conn,
            cfg(BASE_YAML),
            handlers=rec.handlers(ALL),
            locks=LockManager(tmp_path),
            notifier=RecordingNotifier(),
            is_halted=lambda: False,
            routing=LOCAL_ROUTING,  # D39: only a local model takes the LLM lock
        )
        with LockManager(tmp_path).hold(LLM_LOCK):
            report = d.tick(et(2026, 9, 28, 12, 0))
        assert rec.calls == ["rss"]
        assert next(o for o in report.outcomes if o.job == "scalp").status == "deferred"
        assert [r[0] for r in runs(conn)] == ["rss"]
        d.tick(et(2026, 9, 28, 12, 5))  # next tick, inside scalp's 3h window
        assert rec.calls == ["rss", "scalp"]


class TestDryRunAndCli:
    def test_dry_run_changes_nothing(self, conn: sqlite3.Connection) -> None:
        d, rec, _ = make(conn)
        report = d.tick(et(2026, 9, 28, 12, 0), dry_run=True, since=et(2026, 9, 28, 11, 55))
        assert rec.calls == []
        assert runs(conn) == []
        assert [o.job for o in report.outcomes] == ["rss", "scalp", "research"]
        assert report.outcomes[-1].status == "may-run"
        lines = report.lines()
        assert "dry-run" in lines[0]

    def test_cli_dry_run_matches_card(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["routines", "tick", "--dry-run", "--now", "2026-09-28T12:00-04:00"])
        out = capsys.readouterr().out
        assert rc == 0
        order = [ln.split()[3] for ln in out.splitlines() if ln.strip()[:2].rstrip(".").isdigit()]
        assert order[-1] == "scalp"
        # D31: 12:00 is a loop slot too; the loop runs before the Scalp of the same tick.
        # D45: no 12:00 YouTube slot any more (02:00 ET only).
        assert set(order[:-1]) == {
            "edgar",
            "rss",
            "research",
            "monitor",
            "options_fast",
            "market_movers",  # E14.3 (D60)
            "ticker_news",
        }
        assert order.index("research") < order.index("scalp")
        assert order.index("options_fast") < order.index("scalp")  # E13.6: a Scalp source
        assert order.index("market_movers") < order.index("scalp")  # E14.3: a Scalp source
        assert "quant.open" in out and "may-run" not in out  # no trigger any more

    def test_cli_validate_list_history_context(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["routines", "validate"]) == 0
        bad = tmp_path / "bad.yaml"
        bad.write_text("sources: {rss: {}}\n")
        assert main(["routines", "validate", "--config", str(bad)]) == 1
        assert main(["routines", "list", "--now", "2026-09-28T11:00-04:00"]) == 0
        out = capsys.readouterr().out
        assert (
            "INVALID" in out
            and "research" in out
            and "quant.open → risk.open → quant.revise → quant.propose" in out
        )

        db = str(tmp_path / "arc.db")
        with freeze_time("2026-09-28T13:00:00Z"):  # 09:00 ET
            rc = main(["routines", "emit", "approval", "--db", db, "--payload", '{"x": 1}'])
        assert rc == 0
        assert main(["routines", "emit", "approval", "--db", db, "--payload", "[1]"]) == 2
        assert main(["routines", "history", "--db", db]) == 0
        assert main(["context", "show", "--db", db]) == 0
        assert "(no active context entries)" in capsys.readouterr().out

    def test_cli_tick_json_and_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = str(tmp_path / "arc.db")
        cfg_path = tmp_path / "r.yaml"
        cfg_path.write_text(
            "personas:\n"
            "  research: {schedule: ['09:00'], chain: [quant.open], writes: [shortlist]}\n"
            "steps:\n  quant.open: {writes: [structures, note]}\n"
        )
        base = ["--config", str(cfg_path), "--db", db]
        rc = main(
            [
                "routines",
                "tick",
                *base,
                "--json",
                "--no-slack",
                "--lock-dir",
                str(tmp_path),
                "--now",
                "2026-09-28T09:00-04:00",
                "--since",
                "2026-09-28T08:55-04:00",
            ]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        # E5.2 handlers: no candidates → empty shortlist → no structures (no network touched)
        assert [o["status"] for o in data["outcomes"]] == ["ok", "ok"]
        rc = main(
            [
                "routines",
                "run",
                "research",
                *base,
                "--no-slack",
                "--lock-dir",
                str(tmp_path),
                "--now",
                "2026-09-28T09:30-04:00",
            ]
        )
        assert rc == 0
        assert main(["routines", "run", "ghost", *base, "--no-slack"]) == 2
        assert main(["routines", "history", "--db", db, "--job", "research"]) == 0
        assert "research" in capsys.readouterr().out

    def test_cli_context_show(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        c = connect(tmp_path / "arc.db")
        migrate(c)
        store = ContextStore(c)
        store.write(
            kind="shortlist",
            subject="market",
            payload={"shortlist": [], "market_regime": "risk_on", "session_notes": "x" * 200},
            produced_by="research",
            now=et(2026, 9, 28, 9, 0),
        )
        snap = store.snapshot(et(2026, 9, 28, 9, 5))
        db = str(tmp_path / "arc.db")
        args = ["context", "show", "--db", db, "--as-of", "2026-09-28T10:00"]
        assert main([*args, "--kind", "shortlist", "--subject", "market"]) == 0
        assert "shortlist" in capsys.readouterr().out
        assert main([*args, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["kind"] == "shortlist"
        assert main(["context", "show", "--db", db, "--snapshot", snap.id]) == 0
        assert "research" in capsys.readouterr().out
