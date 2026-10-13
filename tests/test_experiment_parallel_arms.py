"""E15.4 (D69): the arms tick runs the arms in parallel, one lock per arm,
at most ``max_parallel_arms`` at once; one arm's failure never stops another."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from arc.experiments import runner as runner_mod
from arc.experiments.runner import (
    ARMS_TICK_COMPONENT,
    ArmTask,
    arm_tick_lock,
    arms_tick,
)
from arc.monitoring.store import HeartbeatRepo
from arc.routines.handlers import JobResult
from arc.routines.locks import LLM_LOCK, LockBusyError, LockManager, arm_lock_dir, llm_lock_dir
from arc.utils.calendar import ET
from tests.test_experiment_multi_arm import (
    ACCT,
    _control,
    _registered,
    _shared,
    _spec,
    _start,
)

if TYPE_CHECKING:
    from pathlib import Path

AT = dt.datetime(2026, 10, 7, 10, 5, tzinfo=ET)  # Wednesday, RTH: the arm jobs are due
ARMS = {"XP-20:t1", "XP-20:t2", "XP-20:t3", "XP-20:t4"}


def _k4(tmp_path: Path) -> Any:
    """A K=4 A/A in shared mode, started on a file-backed control store."""
    conn = _control(tmp_path)
    _registered(conn, _spec("XP-20", k=4), _shared())
    _start(conn, "XP-20", _shared(), tmp_path)
    return conn


def _sleepy_handlers(seconds: float, live: list[int], peak: list[int]) -> dict[str, Any]:
    """Stub arm jobs that each take *seconds*; *peak* records the max arms in a job."""
    lock = threading.Lock()

    def handler(name: str) -> Any:
        def run(ctx: Any) -> JobResult:
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(seconds)
            with lock:
                live[0] -= 1
            return JobResult(summary=f"{name} stub")

        return run

    return {j: handler(j) for j in ("monitor", "positions.evaluate", "broker.reconcile", "broker")}


def _span(report: dict[str, Any], key: str) -> tuple[dt.datetime, dt.datetime]:
    arm = report["arms"][key]
    return dt.datetime.fromisoformat(arm["started_at"]), dt.datetime.fromisoformat(
        arm["finished_at"]
    )


@pytest.mark.serial  # compares wall times: no xdist CPU contention
def test_k4_arms_overlap_and_total_wall_is_max_not_sum(tmp_path: Path) -> None:
    conn = _k4(tmp_path)
    live, peak = [0], [0]
    report = arms_tick(
        conn,
        routines_path=None,
        now=AT,
        lock_dir=tmp_path / "locks",
        handlers=_sleepy_handlers(0.3, live, peak),
        account_number=lambda keys: ACCT,
    )
    assert set(report["arms"]) == ARMS
    for key in ARMS:
        arm = report["arms"][key]
        assert arm["status"] == "ok", arm
        assert len(arm["jobs"]) >= 1
        assert {"started_at", "finished_at", "wall_ms"} <= set(arm)
    # every arm's run overlaps every other's
    spans = [_span(report, k) for k in sorted(ARMS)]
    assert max(s for s, _ in spans) < min(e for _, e in spans)
    assert peak[0] >= 2
    walls = [report["arms"][k]["wall_ms"] for k in ARMS]
    assert report["arms_wall_ms_max"] == max(walls)
    assert report["wall_ms"] < sum(walls)  # parallel: ~max(arm), not the sum
    hb = HeartbeatRepo(conn).latest(ARMS_TICK_COMPONENT)
    assert hb is not None and hb.status == "ok"
    assert hb.detail["arms_wall_ms_max"] == report["arms_wall_ms_max"]
    assert set(hb.detail["arms"]) == ARMS


def test_one_arm_raises_the_others_finish_and_the_report_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _k4(tmp_path)
    real = runner_mod.plan_of

    def plan_of(ident: Any, *a: Any, **k: Any) -> Any:
        if ident.arm == "t2":
            msg = "t2 blew up"
            raise RuntimeError(msg)
        return real(ident, *a, **k)

    monkeypatch.setattr(runner_mod, "plan_of", plan_of)
    ran: list[str] = []

    def handler(name: str) -> Any:
        def run(ctx: Any) -> JobResult:
            ran.append(f"{ctx.run_env.db_path}:{name}")
            return JobResult(summary="ok")

        return run

    handlers = {j: handler(j) for j in ("monitor", "positions.evaluate", "broker.reconcile")}
    report = arms_tick(
        conn, routines_path=None, now=AT, lock_dir=tmp_path / "locks", handlers=handlers
    )
    bad = report["arms"]["XP-20:t2"]
    assert bad["status"] == "failed" and bad["error"] == "RuntimeError: t2 blew up"
    for key in ARMS - {"XP-20:t2"}:
        assert report["arms"][key]["status"] == "ok", report["arms"][key]
    assert {r.split(":")[0].rsplit("/", 1)[-1] for r in ran} == {
        "exp-XP-20-t1.db",
        "exp-XP-20-t3.db",
        "exp-XP-20-t4.db",
    }
    hb = HeartbeatRepo(conn).latest(ARMS_TICK_COMPONENT)
    assert hb is not None and hb.status == "failed"
    assert hb.detail["arms"]["XP-20:t2"]["error"] == "RuntimeError: t2 blew up"


def test_a_launcher_exception_fails_only_that_arm(tmp_path: Path) -> None:
    conn = _k4(tmp_path)
    seen: list[str] = []

    def launcher(task: ArmTask) -> dict[str, Any]:
        seen.append(task.key)
        if task.arm == "t3":
            msg = "child crashed"
            raise OSError(msg)
        return {"jobs": [], "pid": 1}

    report = arms_tick(conn, routines_path=None, now=AT, lock_dir=None, launcher=launcher)
    assert sorted(seen) == sorted(ARMS)
    t3 = report["arms"]["XP-20:t3"]
    assert (t3["status"], t3["error"]) == ("failed", "OSError: child crashed")
    assert all(report["arms"][k]["status"] == "ok" for k in ARMS - {"XP-20:t3"})


def test_max_parallel_arms_caps_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _k4(tmp_path)
    monkeypatch.setattr(runner_mod, "runner_config", lambda *_a, **_k: _shared(max_parallel_arms=2))
    lock = threading.Lock()
    live, peak = [0], [0]

    def launcher(task: ArmTask) -> dict[str, Any]:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        return {}

    report = arms_tick(conn, routines_path=None, now=AT, lock_dir=None, launcher=launcher)
    assert peak[0] == 2
    assert all(report["arms"][k]["status"] == "ok" for k in ARMS)


def test_a_still_running_arm_skips_its_slot_and_blocks_no_other(tmp_path: Path) -> None:
    conn = _k4(tmp_path)
    locks = tmp_path / "locks"
    held = LockManager(locks)
    ran: list[str] = []

    def handler(name: str) -> Any:
        def run(ctx: Any) -> JobResult:
            ran.append(str(ctx.run_env.db_path))
            return JobResult(summary="ok")

        return run

    handlers = {j: handler(j) for j in ("monitor", "positions.evaluate", "broker.reconcile")}
    with held.hold(arm_tick_lock("XP-20:t1")):  # t1's previous tick is still running
        report = arms_tick(conn, routines_path=None, now=AT, lock_dir=locks, handlers=handlers)
    t1 = report["arms"]["XP-20:t1"]
    assert t1["status"] == "skipped" and t1["skipped"] == "previous arm tick running"
    assert "jobs" not in t1
    for key in ARMS - {"XP-20:t1"}:
        assert report["arms"][key]["status"] == "ok"
    assert not any(p.endswith("exp-XP-20-t1.db") for p in ran)
    assert (locks / "experiment-arms_XP-20_t1.lock").is_file()


def test_plan_lock_covers_planning_only(tmp_path: Path) -> None:
    conn = _k4(tmp_path)
    locks = tmp_path / "locks"
    states: list[bool] = []

    def launcher(task: ArmTask) -> dict[str, Any]:
        # the planning lock is released before any arm runs: another arms-tick could plan
        try:
            with LockManager(locks).hold("experiment-arms"):
                states.append(True)
        except LockBusyError:
            states.append(False)
        return {}

    arms_tick(
        conn,
        routines_path=None,
        now=AT,
        lock_dir=locks,
        launcher=launcher,
        plan_lock=lambda: LockManager(locks).hold("experiment-arms"),
    )
    assert states and all(states)
    with LockManager(locks).hold("experiment-arms"), pytest.raises(LockBusyError):
        arms_tick(
            conn,
            routines_path=None,
            now=AT,
            lock_dir=locks,
            launcher=launcher,
            plan_lock=lambda: LockManager(locks).hold("experiment-arms"),
        )


def test_precheck_failures_are_reported_and_not_launched(tmp_path: Path) -> None:
    conn = _k4(tmp_path)
    (tmp_path / "arms" / "exp-XP-20-t4.db").unlink()
    launched: list[str] = []

    def launcher(task: ArmTask) -> dict[str, Any]:
        launched.append(task.key)
        return {}

    report = arms_tick(
        conn,
        routines_path=None,
        now=AT,
        lock_dir=None,
        launcher=launcher,
        account_number=lambda keys: ACCT,
    )
    assert sorted(launched) == ["XP-20:t1", "XP-20:t2", "XP-20:t3"]
    t4 = report["arms"]["XP-20:t4"]
    assert t4 == {"store": t4["store"], "error": "arm store missing", "status": "failed"}


def test_main_path_is_none_for_a_memory_store(tmp_path: Path) -> None:
    """A ``:memory:`` control can't be reopened per thread: the arms share it serially."""
    import sqlite3

    from arc.experiments.runner import _main_path

    assert _main_path(sqlite3.connect(":memory:")) is None
    assert _main_path(_control(tmp_path)) == str((tmp_path / "control.db").resolve())


# --- locks: per-arm dirs share control's D39 llm lock -------------------------------------


def test_arm_lock_dirs_share_controls_llm_lock(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    arm = arm_lock_dir(locks, "XP-20", "t1")
    assert arm == locks / "arm-XP-20-t1"
    assert llm_lock_dir(arm) == locks and llm_lock_dir(locks) == locks
    with LockManager(locks).hold(LLM_LOCK), pytest.raises(LockBusyError):  # noqa: SIM117
        with LockManager(arm).hold("research", LLM_LOCK):
            pass
    # job locks stay per arm: two arms run the same job at once
    with (
        LockManager(arm).hold("research"),
        LockManager(arm_lock_dir(locks, "XP-20", "t2")).hold("research"),
    ):
        pass
    assert (locks / "_llm.lock").is_file() and not (arm / "_llm.lock").exists()


def test_store_reset_still_sees_a_held_arm_lock(tmp_path: Path) -> None:
    """E19.3's reset probe walks the lock dir recursively; the per-arm lock is inside it."""
    from arc.store.reset import _held_locks

    locks = tmp_path / "locks"
    locks.mkdir()
    p = locks / "experiment-arms_XP-20_t1.lock"
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _held_locks(locks) == ["experiment-arms_XP-20_t1.lock"]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- the routines tick heartbeat ---------------------------------------------------------


def test_tick_heartbeat_carries_the_latest_arms_wall_ms_max(tmp_path: Path) -> None:
    from arc.routines.cli import _arms_wall_ms_max

    conn = _control(tmp_path)
    assert _arms_wall_ms_max(conn) is None
    HeartbeatRepo(conn).record(ARMS_TICK_COMPONENT, "ok", at=AT, detail={"arms_wall_ms_max": 1234})
    assert _arms_wall_ms_max(conn) == 1234


# --- CLI: arm-tick and the process launcher ----------------------------------------------


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def test_arm_tick_cli_refuses_bad_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _k4(tmp_path).close()
    db = ("--db", str(tmp_path / "control.db"))
    assert _arc("experiment", "arm-tick", "t1", *db) == 2
    assert "not an arm id" in capsys.readouterr().err
    assert _arc("experiment", "arm-tick", "XP-99:t1", *db) == 2
    assert "XP-99 is not running" in capsys.readouterr().err
    assert _arc("experiment", "arm-tick", "XP-20:t9", *db) == 2
    assert "has no arm 't9'" in capsys.readouterr().err


def test_arm_tick_cli_skips_when_the_arm_lock_is_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _k4(tmp_path).close()
    locks = tmp_path / "locks"
    before = (tmp_path / "control.db").stat().st_mtime_ns
    with LockManager(locks).hold(arm_tick_lock("XP-20:t2")):
        code = _arc("experiment", "arm-tick", "XP-20:t2", "--db", str(tmp_path / "control.db"),
                    "--lock-dir", str(locks), "--now", AT.isoformat(), "--json")  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{") :])["skipped"] == "previous arm tick running"
    assert (tmp_path / "control.db").stat().st_mtime_ns == before  # control read mode=ro


def test_process_launcher_reads_the_childs_json_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from arc.experiments import cli_arms
    from arc.routines import spawn

    conn = _control(tmp_path)
    args = argparse.Namespace(
        db=str(tmp_path / "control.db"), config=None, lock_dir=str(tmp_path / "l"),
        no_slack=True, now=AT.isoformat(),
    )  # fmt: skip
    argvs: list[list[str]] = []
    script = {"t1": "print('log line'); print('{\"jobs\": [[\"monitor\", \"ok\"]]}')",
              "t2": "import sys; sys.exit(3)"}  # fmt: skip

    def fake(env: Any, a: list[str]) -> list[str]:
        argvs.append(list(a))
        return [sys.executable, "-c", script[a[2].split(":")[1]]]

    monkeypatch.setattr(spawn, "arc_command", fake)
    launch = cli_arms._process_launcher(args, conn)
    res = launch(ArmTask("XP-20", "t1", tmp_path / "x.db"))
    assert res["jobs"] == [["monitor", "ok"]] and isinstance(res["pid"], int)
    assert launch(ArmTask("XP-20", "t2", tmp_path / "x.db")) == {
        "error": "arm-tick exited 3 with no report"
    }
    assert argvs[0] == ["experiment", "arm-tick", "XP-20:t1", "--json", "--now", AT.isoformat()]


def test_process_launcher_times_out_without_killing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from arc.experiments import cli_arms
    from arc.routines import spawn

    conn = _control(tmp_path)
    args = argparse.Namespace(db=None, config=None, lock_dir="l", no_slack=True, now=None)
    monkeypatch.setattr(
        spawn, "arc_command", lambda env, a: [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    monkeypatch.setattr(cli_arms, "_child_timeout", lambda _c, _cfg: 0.2)
    launch = cli_arms._process_launcher(args, conn)
    res = launch(ArmTask("XP-20", "t1", tmp_path / "x.db"))
    assert "still running after" in res["error"]


def test_child_timeout_is_twice_the_loop_deadline_plus_grace(tmp_path: Path) -> None:
    from arc.experiments import cli_arms

    conn = _control(tmp_path)
    assert cli_arms._child_timeout(conn, None) == 2 * 7 * 60 + cli_arms._ARM_CHILD_GRACE_S
