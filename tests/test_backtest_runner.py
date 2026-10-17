"""E26.3 (D121): the nightly backtest study runner (``backtest.studies``)."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from arc.backtest.queue import QueueEntry, StudyQueue, load_queue, save_queue, update_queue
from arc.backtest.runner import (
    RunnerPaths,
    SkipNightError,
    clear_dev_fail,
    estimate_s,
    night_window,
    run_night,
    rung_argv,
    rung_run,
)
from arc.backtest.study import LedgerEntry, StudySpec, defer_sigterm, study_hash
from arc.routines.config import (
    BacktestStudiesSettings,
    Lane,
    RoutinesConfig,
    load_routines,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parent.parent
APPROVAL = "slack:1791669727.601139"
CFG = BacktestStudiesSettings()
FRI_NIGHT = dt.datetime(2026, 10, 16, 21, 30, tzinfo=ET)  # Fri; next session Mon 10-19
TUE_NIGHT = dt.datetime(2026, 10, 13, 21, 30, tzinfo=ET)


def _real(a: dt.datetime, b: dt.datetime) -> dt.timedelta:
    return b.astimezone(dt.UTC) - a.astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _doc(sid: str, ladder: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": sid,
        "hypothesis": "take profit 0.4 beats 0.5",
        "owner": "Arc Analyst A-3",
        "created": "2026-10-16",
        "arms": {
            "control": {},
            "treatments": {
                "t1": {"overlay": {"exits": {"default": {"take_profit_pct_of_max_gain": 0.4}}}}
            },
        },
        "profiles": ["margin"],
        "universe_ladder": ladder or ["c125", "c500"],
    }


def _spec(sid: str, ladder: list[str] | None = None) -> tuple[StudySpec, str]:
    text = yaml.safe_dump(_doc(sid, ladder))
    return StudySpec.model_validate(yaml.safe_load(text)), text


@dataclass
class Main:
    """A fake ``origin/main`` view."""

    texts: dict[str, str] = field(default_factory=dict)

    def files(self, directory: str) -> dict[str, str]:
        return {f"{directory}/{k}.yaml": v for k, v in self.texts.items()}


@dataclass
class Clock:
    t: dt.datetime

    def __call__(self) -> dt.datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += dt.timedelta(seconds=s)


def _verdict(switch: bool, ci_hi: float, primary: float = -0.01) -> dict[str, Any]:
    return {
        "switch": switch,
        "call": "switch candidate" if switch else "keep control",
        "pnl_diff": -12.0,
        "ci_lo": -40.0,
        "ci_hi": ci_hi,
        "primary_diff": primary,
    }


def _ledger_row(
    spec: StudySpec,
    *,
    phase: str,
    uni: str,
    at: dt.datetime,
    run_dir: Path,
    runtime_s: float | None = 600.0,
    **v: Any,
) -> LedgerEntry:
    return LedgerEntry(
        at=at,
        study=spec.id,
        spec_hash=study_hash(spec),
        arm_set_hash="a" * 64,
        arms={},
        profiles=["margin"],
        phase=phase,  # type: ignore[arg-type]
        universe=uni,
        universe_version="v1",
        partitions={},
        start=dt.date(2024, 1, 1),
        end=dt.date(2024, 3, 1),
        data_end=dt.date(2024, 3, 1),
        git_sha="abc",
        primary_metric="pnl",
        metrics={},
        verdicts={"margin": {"t1": v or _verdict(False, 5.0)}},
        variants=1,
        run_dir=str(run_dir),
        runtime_s=runtime_s,
    )


@dataclass
class Handle:
    """A fake ``study run`` child: finishes after *duration* s of fake time."""

    clock: Clock
    duration: float
    on_done: Any
    rc: int = 0
    ignore_term: bool = False
    pid: int = 4242
    started: dt.datetime = field(init=False)
    terminated: bool = False
    killed: bool = False

    def __post_init__(self) -> None:
        self.started = self.clock()

    def poll(self) -> int | None:
        if self.killed:
            return -9
        if self.terminated and not self.ignore_term:
            return 143
        if (self.clock() - self.started).total_seconds() >= self.duration:
            if self.rc == 0:
                self.on_done()
            return self.rc
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@dataclass
class World:
    tmp: Path
    clock: Clock
    main: Main = field(default_factory=Main)
    #: (study, rung) -> (duration s, rc, verdict kwargs)
    plan: dict[tuple[str, str], tuple[float, int, dict[str, Any]]] = field(default_factory=dict)
    ignore_term: bool = False
    posts: list[str] = field(default_factory=list)
    launches: list[list[str]] = field(default_factory=list)
    handles: list[Handle] = field(default_factory=list)
    specs: dict[str, StudySpec] = field(default_factory=dict)
    #: where queue.json / ledger.jsonl / runs live under *tmp* (the handler's layout)
    sub: str = "."

    @property
    def paths(self) -> RunnerPaths:
        base = self.tmp / self.sub
        return RunnerPaths(
            queue=base / "queue.json",
            ledger=base / "ledger.jsonl",
            runs_dir=base / "runs",
            report_dir=None,
            data_dir=self.tmp / "data",
            cwd=self.tmp,
        )

    def add(self, sid: str, *, merged: bool = True, ladder: list[str] | None = None) -> None:
        spec, text = _spec(sid, ladder)
        self.specs[sid] = spec
        if merged:
            self.main.texts[sid] = text
        entry = QueueEntry(
            id=sid,
            spec_hash=study_hash(spec),
            approved_at=FRI_NIGHT - dt.timedelta(days=1),
            approval=APPROVAL,
        )
        update_queue(self.paths.queue, lambda q: q.entries.append(entry))

    def launcher(self, argv: Sequence[str], log_path: Path, cwd: Path) -> Handle:
        argv = list(argv)
        self.launches.append(argv)
        sid = argv[argv.index("run") + 1]
        uni = argv[argv.index("--universe") + 1]
        holdout = "--holdout" in argv
        rung = "holdout" if holdout else f"{uni}_dev"
        at = dt.datetime.fromisoformat(argv[argv.index("--now") + 1])
        dur, rc, v = self.plan.get((sid, rung), (600.0, 0, {}))
        run_dir = self.tmp / "runs" / sid / f"run-{len(self.launches)}"
        spec = self.specs[sid]

        def done() -> None:
            run_dir.mkdir(parents=True, exist_ok=True)
            row = _ledger_row(
                spec,
                phase="holdout" if holdout else "dev",
                uni=uni,
                at=at,
                run_dir=run_dir,
                **v,
            )
            with self.paths.ledger.open("a") as fh:
                fh.write(row.model_dump_json() + "\n")
            if holdout:
                (run_dir / "report.json").write_text(json.dumps({}))

        if rc != 0:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as fh:
                fh.write("Traceback ...\nValueError: boom\n")
        h = Handle(self.clock, dur, done, rc=rc, ignore_term=self.ignore_term)
        self.handles.append(h)
        return h

    def night(self, **kw: Any) -> Any:
        return run_night(
            cfg=kw.pop("cfg", CFG),
            paths=self.paths,
            clock=self.clock,
            sleep=self.clock.sleep,
            main=self.main,
            launcher=self.launcher,
            post=self.posts.append,
            **kw,
        )

    def entry(self, sid: str) -> QueueEntry:
        e = load_queue(self.paths.queue).get(sid)
        assert e is not None
        return e


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Clock(TUE_NIGHT))


# ---------------------------------------------------------------------------
# window maths
# ---------------------------------------------------------------------------


class TestWindow:
    def test_weeknight(self) -> None:
        start, end = night_window(TUE_NIGHT, CFG)  # type: ignore[misc]
        assert start == TUE_NIGHT
        assert end == dt.datetime(2026, 10, 14, 8, 30, tzinfo=ET)

    def test_early_morning_belongs_to_last_night(self) -> None:
        w = night_window(dt.datetime(2026, 10, 14, 3, 0, tzinfo=ET), CFG)
        assert w is not None and w[0] == TUE_NIGHT

    @pytest.mark.parametrize("t", [dt.time(8, 30), dt.time(12, 0), dt.time(21, 29, 59)], ids=str)
    def test_trading_day_is_closed(self, t: dt.time) -> None:
        assert night_window(dt.datetime.combine(dt.date(2026, 10, 14), t, tzinfo=ET), CFG) is None

    def test_weekend_runs_to_monday(self) -> None:
        start, end = night_window(FRI_NIGHT, CFG)  # type: ignore[misc]
        assert end == dt.datetime(2026, 10, 19, 8, 30, tzinfo=ET)
        sat_noon = dt.datetime(2026, 10, 17, 12, 0, tzinfo=ET)
        sun_noon = dt.datetime(2026, 10, 18, 12, 0, tzinfo=ET)
        assert night_window(sat_noon, CFG) == (start, end)
        # Sat 21:30 opens its own window with the same Monday end
        assert night_window(sun_noon, CFG)[1] == end  # type: ignore[index]

    def test_holiday_extends_the_night(self) -> None:
        # Thanksgiving 2026-11-26 (Thu): Wed's night ends Fri 08:30
        wed = dt.datetime(2026, 11, 25, 21, 30, tzinfo=ET)
        assert night_window(wed, CFG)[1] == dt.datetime(2026, 11, 27, 8, 30, tzinfo=ET)  # type: ignore[index]

    def test_dst_fall_back_night_has_the_extra_hour(self) -> None:
        # DST ends Sun 2026-11-01 02:00: Fri 10-30 night to Mon 11-02 08:30 ET
        fri = dt.datetime(2026, 10, 30, 21, 30, tzinfo=ET)
        start, end = night_window(fri, CFG)  # type: ignore[misc]
        assert end == dt.datetime(2026, 11, 2, 8, 30, tzinfo=ET)
        assert _real(start, end) == dt.timedelta(hours=59 + 1)
        assert end.utcoffset() == dt.timedelta(hours=-5)
        # 08:30 EST is 13:30 UTC: the window closes at the ET wall time, not 12:30 UTC
        assert night_window(dt.datetime(2026, 11, 2, 13, 29, tzinfo=dt.UTC), CFG) is not None
        assert night_window(dt.datetime(2026, 11, 2, 13, 30, tzinfo=dt.UTC), CFG) is None

    def test_dst_spring_forward(self) -> None:
        # DST starts Sun 2027-03-14: a Thursday night is 11 h, Friday's is an hour short
        thu = dt.datetime(2027, 3, 11, 21, 30, tzinfo=ET)
        start, end = night_window(thu, CFG)  # type: ignore[misc]
        assert _real(start, end) == dt.timedelta(hours=11)
        fri = dt.datetime(2027, 3, 12, 21, 30, tzinfo=ET)
        s2, e2 = night_window(fri, CFG)  # type: ignore[misc]
        assert e2.utcoffset() == dt.timedelta(hours=-4)
        assert _real(s2, e2) == dt.timedelta(hours=59 - 1)

    def test_outside_the_window_skips(self, world: World) -> None:
        world.clock.t = dt.datetime(2026, 10, 14, 12, 0, tzinfo=ET)
        with pytest.raises(SkipNightError, match="outside the study window"):
            world.night()

    def test_bad_weekend_end_refused(self) -> None:
        with pytest.raises(ValueError, match="weekend_window_end"):
            BacktestStudiesSettings(weekend_window_end="Monday 8")


# ---------------------------------------------------------------------------
# estimates, rungs, the clear-fail rule
# ---------------------------------------------------------------------------


def test_estimate_prefers_own_rows_then_any(tmp_path: Path) -> None:
    a, _ = _spec("BT-1")
    b, _ = _spec("BT-2")

    def row(spec: StudySpec, secs: float, i: int) -> LedgerEntry:
        at = TUE_NIGHT + dt.timedelta(hours=i)
        return _ledger_row(spec, phase="dev", uni="c125", at=at, run_dir=tmp_path, runtime_s=secs)

    rows = [row(a, 100, 0), row(a, 300, 1), row(b, 1000, 2)]
    assert estimate_s(rows, "BT-1", "dev", "c125", 1.2) == pytest.approx(240.0)
    assert estimate_s(rows, "BT-9", "dev", "c125", 1.2) == pytest.approx(360.0)
    assert estimate_s(rows, "BT-1", "dev", "c500", 1.2) is None


def test_rung_universes() -> None:
    spec, _ = _spec("BT-1")
    assert rung_run("c125_dev", spec) == ("dev", "c125")
    assert rung_run("c500_dev", spec) == ("dev", "c500")
    assert rung_run("holdout", spec) == ("holdout", "c500")
    only, _ = _spec("BT-2", ["c125"])
    assert rung_run("c500_dev", only) is None
    assert rung_run("holdout", only) == ("holdout", "c125")


@pytest.mark.parametrize(
    ("rows", "fail"),
    [
        ([_verdict(False, -1.0)], True),
        ([_verdict(False, -1.0), _verdict(False, -0.5)], True),
        ([_verdict(False, -1.0), _verdict(False, 2.0)], False),  # one row inconclusive
        ([_verdict(True, -1.0)], False),  # a switch is never a fail
        ([_verdict(False, 0.0)], False),  # CI touching 0 is not "clearly worse"
        ([_verdict(False, -1.0, primary=0.02)], False),  # primary metric not worse
        ([], False),
    ],
)
def test_clear_dev_fail(tmp_path: Path, rows: list[dict[str, Any]], fail: bool) -> None:
    spec, _ = _spec("BT-1")
    e = _ledger_row(spec, phase="dev", uni="c125", at=TUE_NIGHT, run_dir=tmp_path)
    e.verdicts = {f"p{i}": {"t1": r} for i, r in enumerate(rows)}
    assert clear_dev_fail(e, 0.0) is fail


def test_rung_argv(tmp_path: Path) -> None:
    w = World(tmp_path, Clock(TUE_NIGHT))
    argv = rung_argv(
        "BT-3",
        phase="holdout",
        universe="c500",
        studies_dir=tmp_path / "spec",
        paths=w.paths,
        workers=6,
        nice=10,
        now=TUE_NIGHT,
        python="py",
    )
    assert argv[:3] == [argv[0], "-n", "10"] and argv[0].endswith("nice")
    tail = argv[3:]
    assert tail[0] == "py" and tail[3:7] == ["backtest", "study", "run", "BT-3"]
    for flag, val in (("--workers", "6"), ("--universe", "c500"), ("--now", TUE_NIGHT.isoformat())):
        assert tail[tail.index(flag) + 1] == val
    assert "--holdout" in tail and "--no-report" in tail
    assert tail[tail.index("--ledger") + 1] == str(w.paths.ledger)


# ---------------------------------------------------------------------------
# a night
# ---------------------------------------------------------------------------


def test_full_ladder_in_one_night(world: World) -> None:
    world.add("BT-1")
    res = world.night()
    e = world.entry("BT-1")
    assert e.status == "concluded" and e.ladder_state is None
    assert [(r.rung, r.outcome) for r in e.runs] == [
        ("c125_dev", "done"),
        ("c500_dev", "done"),
        ("holdout", "done"),
    ]
    assert all(r.ledger_key and r.run_dir for r in e.runs)
    # one message per event: two rungs done + the holdout verdict
    assert len(world.posts) == 3
    assert world.posts[0].startswith("BT · BT-1 · c125_dev done (c125) · margin/t1: keep control")
    assert "next c500_dev" in world.posts[0]
    assert world.posts[2].startswith("BT · BT-1 · holdout verdict (c500):")
    assert res.metrics["rungs_done"] == 3
    assert ["--holdout" in a for a in world.launches] == [False, False, True]
    assert (world.paths.runs_dir / "BT-1" / "spec" / "BT-1.yaml").read_text() == (
        world.main.texts["BT-1"]
    )
    assert "queue empty" in res.summary


def test_c125_only_ladder_goes_straight_to_holdout(world: World) -> None:
    world.add("BT-1", ladder=["c125"])
    world.night()
    e = world.entry("BT-1")
    assert [r.rung for r in e.runs] == ["c125_dev", "holdout"]
    assert world.launches[-1][world.launches[-1].index("--universe") + 1] == "c125"
    assert world.posts[0].endswith("next holdout")


def test_one_study_at_a_time_in_queue_order(world: World) -> None:
    world.add("BT-1")
    world.add("BT-2")
    world.night()
    sids = [a[a.index("run") + 1] for a in world.launches]
    assert sids == ["BT-1"] * 3 + ["BT-2"] * 3
    assert world.entry("BT-2").status == "concluded"


def test_clear_dev_fail_stops_without_a_holdout(world: World) -> None:
    world.add("BT-1")
    world.add("BT-2")
    world.plan[("BT-1", "c125_dev")] = (600.0, 0, _verdict(False, -3.5))
    world.night()
    e = world.entry("BT-1")
    assert e.status == "stopped" and e.ladder_state is None
    assert e.stop_reason is not None and "dev fail on c125_dev" in e.stop_reason
    assert "CI [-40.0, -3.5]" in e.stop_reason and "primary_diff -0.01" in e.stop_reason
    assert len([a for a in world.launches if "BT-1" in a]) == 1
    assert world.posts[0].startswith("BT · BT-1 · stopped: dev fail on c125_dev (no holdout spent)")
    # the queue moves on
    assert world.entry("BT-2").status == "concluded"


def test_inconclusive_dev_continues(world: World) -> None:
    world.add("BT-1")
    world.plan[("BT-1", "c125_dev")] = (600.0, 0, _verdict(False, 4.0))
    world.night()
    assert world.entry("BT-1").status == "concluded"


def test_a_stopped_entry_is_never_picked(world: World) -> None:
    world.add("BT-1")
    update_queue(world.paths.queue, lambda q: setattr(q.get("BT-1"), "status", "stopped"))
    res = world.night()
    assert world.launches == [] and world.posts == []
    assert "queue empty" in res.summary


def test_unmerged_spec_is_skipped(world: World) -> None:
    world.add("BT-1", merged=False)
    world.add("BT-2")
    res = world.night()
    assert "BT-1 skipped: spec_unmerged" in res.summary
    assert world.entry("BT-1").status == "queued" and world.entry("BT-1").runs == []
    assert world.entry("BT-2").status == "concluded"
    assert res.metrics["spec_unmerged"] == ["BT-1"]
    assert not any("BT-1" in p for p in world.posts)


def test_spec_hash_mismatch_fails_the_entry(world: World) -> None:
    world.add("BT-1")
    world.main.texts["BT-1"] = yaml.safe_dump({**_doc("BT-1"), "hypothesis": "changed"})
    world.night()
    e = world.entry("BT-1")
    assert e.status == "failed" and "spec hash on main" in (e.stop_reason or "")
    assert world.launches == [] and len(world.posts) == 1


def test_failed_run_marks_failed_and_moves_on(world: World) -> None:
    world.add("BT-1")
    world.add("BT-2")
    world.plan[("BT-1", "c500_dev")] = (60.0, 1, {})
    world.night()
    e = world.entry("BT-1")
    assert e.status == "failed" and e.stop_reason == "exit 1"
    assert [(r.rung, r.outcome) for r in e.runs] == [("c125_dev", "done"), ("c500_dev", "failed")]
    log_text = (world.paths.runs_dir / "BT-1" / "runner.log").read_text()
    assert "ValueError: boom" in log_text and "=== " in log_text
    fail = [p for p in world.posts if "failed" in p]
    assert len(fail) == 1 and "ValueError: boom" in fail[0] and "runner.log" in fail[0]
    # no retry loop: BT-1 launched twice in total, then BT-2 ran
    assert len([a for a in world.launches if "BT-1" in a]) == 2
    assert world.entry("BT-2").status == "concluded"


def test_fit_or_defer(world: World, tmp_path: Path) -> None:
    world.add("BT-1")
    spec = world.specs["BT-1"]
    # history: c125 dev took 2 h; 8:30 is 1 h away
    with world.paths.ledger.open("a") as fh:
        row = _ledger_row(
            spec, phase="dev", uni="c125", at=TUE_NIGHT - dt.timedelta(days=7),
            run_dir=tmp_path, runtime_s=7200.0,
        )  # fmt: skip
        row.study = "BT-0"
        fh.write(row.model_dump_json() + "\n")
    world.clock.t = dt.datetime(2026, 10, 14, 7, 30, tzinfo=ET)
    res = world.night()
    assert world.launches == []
    assert "BT-1 c125_dev deferred: window (needs ~144 min, 60 min left)" in res.summary
    assert world.entry("BT-1").status == "queued" and world.posts == []


def test_window_end_interrupts_then_resumes(world: World) -> None:
    world.add("BT-1")
    world.plan[("BT-1", "c125_dev")] = (12 * 3600.0, 0, {})  # longer than the night
    res = world.night()
    h = world.handles[0]
    assert h.terminated and not h.killed
    e = world.entry("BT-1")
    assert e.status == "running" and e.ladder_state == "c125_dev"
    assert [(r.rung, r.outcome) for r in e.runs] == [("c125_dev", "interrupted")]
    assert "interrupted at the window end" in res.summary
    assert world.posts == []  # no message for an interrupt
    assert not world.paths.ledger.exists()
    # next night: the warm cache makes the retry fast
    world.plan[("BT-1", "c125_dev")] = (600.0, 0, {})
    world.clock.t = dt.datetime(2026, 10, 14, 21, 30, tzinfo=ET)
    world.night()
    e = world.entry("BT-1")
    assert [(r.rung, r.outcome) for r in e.runs][:2] == [
        ("c125_dev", "interrupted"),
        ("c125_dev", "done"),
    ]
    assert e.status == "concluded"


def test_sigkill_after_grace(world: World) -> None:
    world.add("BT-1")
    world.ignore_term = True
    world.plan[("BT-1", "c125_dev")] = (12 * 3600.0, 0, {})
    world.night()
    h = world.handles[0]
    assert h.terminated and h.killed
    end = dt.datetime(2026, 10, 14, 8, 30, tzinfo=ET)
    assert world.clock.t - end >= CFG.kill_grace
    assert world.entry("BT-1").runs[-1].outcome == "interrupted"


def test_owner_stop_mid_run(world: World) -> None:
    world.add("BT-1")
    world.plan[("BT-1", "c125_dev")] = (3600.0, 0, {})
    orig = world.clock.sleep
    calls = {"n": 0}

    def sleep(s: float) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            from arc.backtest.queue import stop_entry

            update_queue(world.paths.queue, lambda q: stop_entry(q, "BT-1", "not needed"))
        orig(s)

    world.clock.sleep = sleep  # type: ignore[method-assign]
    res = run_night(
        cfg=CFG, paths=world.paths, clock=world.clock, sleep=sleep, main=world.main,
        launcher=world.launcher, post=world.posts.append,
    )  # fmt: skip
    assert world.handles[0].terminated
    e = world.entry("BT-1")
    assert e.status == "stopped" and e.stop_reason == "owner: not needed"
    assert [(r.rung, r.outcome) for r in e.runs] == [("c125_dev", "stopped")]
    assert "stopped by the owner" in res.summary and world.posts == []


def test_waits_for_history_topup_then_skips(world: World) -> None:
    world.add("BT-1")
    with pytest.raises(SkipNightError, match="history.topup still running"):
        world.night(topup_running=lambda: True)
    assert world.clock.t - TUE_NIGHT == CFG.topup_wait
    assert world.launches == []


def test_topup_finishing_lets_the_night_run(world: World) -> None:
    world.add("BT-1")
    left = iter([True, True, False])
    world.night(topup_running=lambda: next(left))
    assert world.entry("BT-1").status == "concluded"


def test_holdout_never_spent_twice(tmp_path: Path) -> None:
    """The E7.12 path the runner relies on: a second holdout of a spec is refused."""
    from arc.backtest.study import StudyRefusedError, check_holdout

    spec, _ = _spec("BT-1")
    row = _ledger_row(spec, phase="holdout", uni="c500", at=TUE_NIGHT, run_dir=tmp_path)
    with pytest.raises(StudyRefusedError, match="holdout already ran"):
        check_holdout(spec, [row], allow_overfit_warning=False)


# ---------------------------------------------------------------------------
# SIGTERM: `study run` exits 143 with no ledger line
# ---------------------------------------------------------------------------

_CHILD = r"""
import sys, time
from pathlib import Path
import arc.backtest.study_cli as cli
ledger = Path(sys.argv[1])
def fake_run_study(spec, **kw):
    Path(sys.argv[2]).write_text("started")
    time.sleep(60)
    kw["ledger"].write_text("partial line\n")
cli.run_study = fake_run_study
from arc.cli import main
raise SystemExit(main(["backtest", "study", "run", "BT-1", "--studies-dir", sys.argv[3],
    "--ledger", str(ledger), "--runs-dir", sys.argv[4], "--no-report",
    "--now", "2026-10-13T21:30:00-04:00"]))
"""


@pytest.mark.serial
def test_study_run_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "BT-1.yaml").write_text(yaml.safe_dump(_doc("BT-1")))
    ledger, started = tmp_path / "ledger.jsonl", tmp_path / "started"
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script), str(ledger), str(started), str(specs), str(tmp_path / "r")],
        stderr=subprocess.PIPE,
        cwd=REPO,
    )
    deadline = time.monotonic() + 60
    while not started.exists():
        assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
        assert time.monotonic() < deadline
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=30)
    err = proc.stderr.read().decode() if proc.stderr else ""
    assert rc == 143, err
    assert "interrupted (SIGTERM); no ledger line" in err
    assert not ledger.exists()


def test_defer_sigterm_holds_the_signal_until_the_block_ends() -> None:
    got: list[int] = []
    old = signal.signal(signal.SIGTERM, lambda s, f: got.append(s))
    try:
        with defer_sigterm():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            assert got == []  # held while the ledger line + report are written
        time.sleep(0.05)
        assert got == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, old)


# ---------------------------------------------------------------------------
# the routine contract
# ---------------------------------------------------------------------------


def test_routine_registered_with_its_contract() -> None:
    from arc.routines.handlers import BUILTIN_HANDLERS as HANDLERS

    r = load_routines()
    kind, spec = r.step("backtest.studies")
    assert kind.value == "source"
    assert spec.schedule == [dt.time(21, 30)]  # type: ignore[union-attr]
    assert spec.lane is Lane.BACKGROUND  # type: ignore[union-attr]
    assert spec.writes == [] and spec.reference  # type: ignore[union-attr]
    assert spec.ttl is not None and spec.ttl.duration == dt.timedelta(hours=2)  # type: ignore[union-attr]
    assert HANDLERS["backtest.studies"] == "arc.routines.handlers:backtest_studies_source"
    assert r.backtest_studies == BacktestStudiesSettings()
    b = r.backtest_studies
    assert (b.window_start, b.window_end, b.weekend_window_end) == (
        dt.time(21, 30),
        dt.time(8, 30),
        "Mon 08:30",
    )
    assert (b.workers, b.nice, b.fail_rule.ci_hi_below) == (6, 10, 0.0)
    # after history.topup's slot + its runtime cap
    _, topup = r.step("history.topup")
    t0 = dt.datetime.combine(dt.date(2026, 10, 13), topup.schedule[0])  # type: ignore[union-attr]
    cap = dt.timedelta(seconds=r.history_topup.max_runtime_s)
    assert (t0 + cap).time() < b.window_start
    # the stuck-run monitor tolerates a weekend night
    from arc.monitoring.config import MonitoringSettings

    m = MonitoringSettings.model_validate(load_routines().monitoring.model_dump())
    assert m.stuck_after_for("backtest.studies") >= dt.timedelta(hours=59)


def test_knobs_are_not_runtime_tunable() -> None:
    from arc.control.registry import REGISTRY, lookup

    assert not any(k.startswith("backtest_studies") for k in REGISTRY)
    with pytest.raises(Exception):  # noqa: B017, PT011
        lookup("backtest_studies.workers")
    # the pause switch is the generic routine one (D121: `!arc config set ... off`)
    assert lookup("routines.backtest.studies.enabled").key == "routines.backtest.studies.enabled"


def _ctx(conn: sqlite3.Connection, now: dt.datetime, slack: bool = False) -> Any:
    from arc.config import ArcSettings
    from arc.context.store import ContextStore
    from arc.routines.handlers import JobContext, RunEnv

    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "backtest.studies": {"schedule": ["21:30"], "writes": [], "lane": "background"}
            }
        }
    )
    kind, spec = routines.step("backtest.studies")
    return JobContext(
        job="backtest.studies", kind=kind, spec=spec, run_id="r1", chain_run_id=None,
        scheduled_for=now, now=now, conn=conn, snapshot=ContextStore(conn).snapshot(now),
        routines=routines, settings_factory=lambda: ArcSettings(),
        clock_fn=lambda: now, run_env=RunEnv(slack=slack),
    )  # fmt: skip


def test_handler_runs_the_night(tmp_path: Path) -> None:
    from arc.routines.handlers import JobSkippedError, backtest_studies_source
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(":memory:")
    migrate(conn)
    w = World(tmp_path, Clock(TUE_NIGHT), sub="data/backtest")
    w.add("BT-1")
    ctx = _ctx(conn, TUE_NIGHT)
    res = backtest_studies_source(
        ctx, launcher=w.launcher, main=w.main, post=w.posts.append, sleep=w.clock.sleep,
        repo_root=tmp_path,
    )  # fmt: skip
    q = load_queue(tmp_path / "data/backtest/queue.json")
    assert q.get("BT-1").status == "concluded"  # type: ignore[union-attr]
    assert (tmp_path / "data/backtest/runs/BT-1/runner.log").exists()
    assert res.metrics["rungs_done"] == 3 and "queue empty" in res.summary
    assert len(w.posts) == 3
    noon = dt.datetime(2026, 10, 14, 12, 0, tzinfo=ET)
    with pytest.raises(JobSkippedError, match="outside the study window"):
        backtest_studies_source(
            _ctx(conn, noon), launcher=w.launcher, main=w.main, post=w.posts.append,
            sleep=w.clock.sleep, repo_root=tmp_path,
        )  # fmt: skip


def test_no_slack_logs_only() -> None:
    from arc.routines.handlers import _analyst_poster
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(":memory:")
    migrate(conn)
    post = _analyst_poster(_ctx(conn, TUE_NIGHT, slack=False))
    post("hello")  # never builds a Slack client


def test_queue_is_unchanged_by_an_empty_night(world: World) -> None:
    save_queue(world.paths.queue, StudyQueue())
    res = world.night()
    assert res.summary.endswith("queue empty") and world.posts == []


def test_weekend_night_picks_up_a_saturday_approval(world: World) -> None:
    """The slot runs on trading days only; Friday's run waits for work until Mon 08:30."""
    world.clock.t = FRI_NIGHT
    save_queue(world.paths.queue, StudyQueue())
    sat = dt.datetime(2026, 10, 17, 14, 0, tzinfo=ET)
    orig = world.clock.sleep
    added = {"done": False}

    def sleep(s: float) -> None:
        orig(s)
        if not added["done"] and world.clock.t >= sat:
            added["done"] = True
            world.add("BT-1")

    world.clock.sleep = sleep  # type: ignore[method-assign]
    res = run_night(
        cfg=CFG, paths=world.paths, clock=world.clock, sleep=sleep, main=world.main,
        launcher=world.launcher, post=world.posts.append,
    )  # fmt: skip
    e = world.entry("BT-1")
    assert e.status == "concluded" and e.runs[0].at >= sat
    assert e.runs[0].at - sat < CFG.idle_poll + dt.timedelta(seconds=1)
    assert "window closed" in res.summary  # then idled to Monday 08:30
    assert world.clock.t >= dt.datetime(2026, 10, 19, 8, 30, tzinfo=ET)


def test_weeknight_with_an_empty_queue_ends_at_once(world: World) -> None:
    save_queue(world.paths.queue, StudyQueue())
    world.night()
    assert world.clock.t == TUE_NIGHT
