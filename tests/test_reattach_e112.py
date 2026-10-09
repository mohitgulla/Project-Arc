"""E11.2 (D72): ladder liveness and re-attach.

In-process tests simulate a dead ladder with a broker that raises a
``BaseException`` mid-poll (the ladder only catches ``Exception``), leaving the
execution ``working`` and the order ``submitted`` exactly as a SIGKILL would.
``TestKillNine`` runs a real ladder in a child process and SIGKILLs it.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from arc.broker.base import BrokerOrderStatus
from arc.broker.reattach_job import reattach_job, reattach_notice
from arc.execution.fills import record_fill
from arc.execution.ladder import ExecStatus, ExecutionAdoptedError, LadderContext
from arc.execution.reattach import find_orphans, reattach
from arc.gate import proposal_hash
from arc.gate.band import PriceBand
from arc.models import OrderState
from arc.routines.locks import LockManager, NullLocks
from arc.routines.runs import RoutineEventRepo, RoutineRunRepo, RunStatus, owner_lock, pid_alive
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from tests import test_execution_ladder as L
from tests import test_execution_submit as S
from tests.file_broker import FileBroker

if TYPE_CHECKING:
    from arc.config import ArcSettings

NOW = S.NOW
DEAD_PID = 2**22 + 11  # above macOS/Linux pid_max defaults: never a live process


class Died(BaseException):
    """The ladder process vanished (SIGKILL): nothing after this line ran."""


class DyingBroker(FileBroker):
    """Submits, then 'dies' on the first status poll of attempt ``die_at``."""

    def __init__(self, path: Path, die_at: int = 0) -> None:
        super().__init__(path)
        self.die_at = die_at
        self.dead = False

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        if not self.dead and broker_order_id == f"fb-{self.die_at}":
            self.dead = True  # the next caller is the re-attach, a different process
            raise Died
        return super().order_status(broker_order_id)


def settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"execution_cancel_confirm_seconds": 2}
    base.update(kw)
    return L.cfg(**base)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _claim(conn: sqlite3.Connection, *, pid: int | None = DEAD_PID, beat: dt.datetime = NOW):
    ev = RoutineEventRepo(conn).emit("approval", {"proposal_hash": "x"}, now=NOW)
    run = RoutineRunRepo(conn).claim(
        job="broker", scheduled_for=NOW, reason="event:approval", now=NOW, event_id=ev.id
    )
    assert run is not None
    if pid is not None:
        RoutineRunRepo(conn).set_pid(run.run_id, pid, now=beat)
    return run, ev


def _die(conn: sqlite3.Connection, broker: FileBroker, *, run_id: str | None, **kw) -> None:
    """Run a ladder until it 'dies' at its first poll of the dying attempt."""
    with pytest.raises(Died):
        L.run(conn, broker, run_id=run_id, config=settings(), **kw)


def _filled(bid: str, qty: int) -> BrokerOrderStatus:
    return BrokerOrderStatus(
        broker_order_id=bid, status="filled", filled_qty=D(qty), filled_avg_price=D("-0.85")
    )


def _phash() -> str:
    return proposal_hash(S.proposal(expires_at=NOW + dt.timedelta(minutes=20)))


def _reattach(conn, broker, tmp_path: Path, *, now=None, **kw):
    clock = L.Clock()
    clock.t = now or NOW + dt.timedelta(minutes=5)
    return reattach(
        conn,
        broker,
        LockManager(tmp_path / "locks"),
        now=clock.t,
        settings=kw.pop("settings", settings()),
        run_id=kw.pop("run_id", "run-reattach"),
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


# ---------------------------------------------------------------------------
# Liveness helpers
# ---------------------------------------------------------------------------


def test_pid_alive() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(DEAD_PID) is False
    assert pid_alive(None) is None
    assert pid_alive(0) is None


def test_owner_lock_name_is_file_safe() -> None:
    assert owner_lock("abc") == "run-abc"


def test_set_pid_and_beat(conn: sqlite3.Connection) -> None:
    run, _ = _claim(conn, pid=4242)
    repo = RoutineRunRepo(conn)
    got = repo.get(run.run_id)
    assert got is not None and got.pid == 4242 and got.heartbeat_at == NOW
    repo.beat(run.run_id, NOW + dt.timedelta(seconds=30))
    got = repo.get(run.run_id)
    assert got is not None and got.heartbeat_at == NOW + dt.timedelta(seconds=30)
    repo.finish(run.run_id, status=RunStatus.OK, now=NOW)
    repo.beat(run.run_id, NOW + dt.timedelta(minutes=9))  # a finished run never beats
    got = repo.get(run.run_id)
    assert got is not None and got.heartbeat_at == NOW + dt.timedelta(seconds=30)
    assert not repo.fail_if_running(run.run_id, error="x", now=NOW)


def test_ladder_beats_every_poll(conn: sqlite3.Connection) -> None:
    beats: list[int] = []
    b = L.ScriptedBroker([["new", "new", "filled"]], fills={0: (2, "-0.85")})
    out = L.run(conn, b, heartbeat=lambda: beats.append(1))
    assert out.status is ExecStatus.FILLED
    assert len(beats) >= 3  # attempt start + every poll


def test_heartbeat_failure_never_stops_the_ladder(conn: sqlite3.Connection) -> None:
    def boom() -> None:
        raise sqlite3.OperationalError("database is locked")

    b = L.ScriptedBroker([["new", "filled"]], fills={0: (2, "-0.85")})
    assert L.run(conn, b, heartbeat=boom).status is ExecStatus.FILLED


# ---------------------------------------------------------------------------
# Re-attach: the four broker outcomes
# ---------------------------------------------------------------------------


def test_dead_ladder_working_order_is_cancelled(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, ev = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    phash = _phash()
    assert ExecutionRepo(conn).get(phash)["status"] == "working"  # type: ignore[index]

    rep = _reattach(conn, b, tmp_path)
    (a,) = rep.adopted
    assert a.final_status == "cancelled" and a.filled_qty == 0
    assert a.cancelled_broker_order_ids == ["fb-0"]
    assert b.calls("submit") == [("submit", b.calls("submit")[0][1])]  # never resubmitted
    ex = ExecutionRepo(conn).get(phash)
    assert ex is not None and ex["status"] == "cancelled"
    assert ex["adopted_by_run_id"] == "run-reattach" and ex["adopted_at"] is not None
    assert "re-attached" in ex["detail"] and f"pid {DEAD_PID}" in ex["detail"]
    state = conn.execute("SELECT state FROM orders").fetchone()[0]
    assert state == OrderState.CANCELLED.value
    dead = RoutineRunRepo(conn).get(run.run_id)
    assert dead is not None and dead.status is RunStatus.FAILED
    assert dead.error is not None and "adopted by reattach run run-reattach" in dead.error
    assert RoutineEventRepo(conn).get(ev.id).consumed_at is not None  # type: ignore[union-attr]
    assert "Re-attached SPY open" in a.alert and "cancelled [fb-0]" in a.alert
    # Idempotent: a second pass finds nothing working.
    again = _reattach(conn, b, tmp_path)
    assert again.checked == 0 and again.adopted == []


def test_dead_ladder_full_fill_reaches_position_model(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    b.set_status("fb-0", "filled", filled=2, price="-0.85")
    rep = _reattach(conn, b, tmp_path)
    (a,) = rep.adopted
    assert a.final_status == "filled" and a.filled_qty == 2 and a.fill_price == D("-0.85")
    assert b.calls("cancel") == []  # terminal at the broker: nothing to cancel
    os_row = OpenStructureRepo(conn).get(a.structure_id or "")
    assert os_row is not None and os_row["contracts"] == 2
    assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM tax_lots").fetchone()[0] >= 1
    ex = ExecutionRepo(conn).get(_phash())
    assert ex is not None and ex["status"] == "filled" and ex["structure_id"] == a.structure_id


def test_dead_ladder_partial_fill_cancels_remainder(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    b.set_status("fb-0", "partially_filled", filled=1, price="-0.85")
    rep = _reattach(conn, b, tmp_path)
    (a,) = rep.adopted
    assert a.final_status == "partially_filled" and a.filled_qty == 1 and a.contracts == 2
    assert ("cancel", "fb-0") in b.calls("cancel")
    os_row = OpenStructureRepo(conn).get(a.structure_id or "")
    assert os_row is not None and os_row["contracts"] == 1


def test_dead_ladder_rejected_order(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    b.set_status("fb-0", "rejected")
    (a,) = _reattach(conn, b, tmp_path).adopted
    assert a.final_status == "cancelled" and a.filled_qty == 0
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == OrderState.REJECTED.value


def test_fill_recorded_before_death_is_not_doubled(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The ladder wrote the fill, then died before the position model: applied once."""
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    b.set_status("fb-0", "filled", filled=2, price="-0.85")
    order_id = conn.execute("SELECT id FROM orders").fetchone()[0]
    st = _filled("fb-0", 2)
    record_fill(conn, order_id=order_id, status=st, limit_price=D("-0.85"), now=NOW, run_id=None)
    (a,) = _reattach(conn, b, tmp_path).adopted
    assert a.final_status == "filled" and a.filled_qty == 2
    assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM open_structures").fetchone()[0] == 1


def test_fills_unique_index_dedups(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, _ = _claim(conn)
    _die(conn, DyingBroker(tmp_path / "b.db"), run_id=run.run_id)
    order_id = conn.execute("SELECT id FROM orders").fetchone()[0]
    st = _filled("f1", 1)
    first = record_fill(
        conn, order_id=order_id, status=st, limit_price=D("-0.85"), now=NOW, run_id=None
    )
    second = record_fill(
        conn, order_id=order_id, status=st, limit_price=D("-0.85"), now=NOW, run_id=None
    )
    assert first == second == (1, D("-0.85"))
    assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fills (id, order_id, broker_fill_id, qty, price, filled_at) "
            "VALUES ('x', ?, 'f1', 1, '1', '2026-01-01')",
            (order_id,),
        )


# ---------------------------------------------------------------------------
# Liveness rules: alive, stale, wedged, off, dry run
# ---------------------------------------------------------------------------


def test_live_ladder_holding_its_lock_is_left_alone(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run, _ = _claim(conn, pid=os.getpid(), beat=NOW + dt.timedelta(minutes=5))
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    locks = LockManager(tmp_path / "locks")
    held = _Holder(tmp_path / "locks", owner_lock(run.run_id))
    try:
        rep = _reattach(conn, b, tmp_path)
        assert rep.adopted == [] and rep.skipped_alive == [_phash()] and rep.wedged == []
        assert (
            find_orphans(conn, locks, now=NOW + dt.timedelta(minutes=5), settings=settings()) == []
        )
    finally:
        held.release()
    assert b.calls("cancel") == []


def test_fresh_heartbeat_without_lock_is_adopted(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The lock is the kernel's word: free means the process is gone, beat or not."""
    run, _ = _claim(conn, beat=NOW + dt.timedelta(minutes=5))
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    assert len(_reattach(conn, b, tmp_path).adopted) == 1


def test_wedged_ladder_gets_sigterm_after_kill_after(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run, _ = _claim(conn)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        RoutineRunRepo(conn).set_pid(run.run_id, child.pid, now=NOW)
        b = DyingBroker(tmp_path / "b.db")
        _die(conn, b, run_id=run.run_id)
        held = _Holder(tmp_path / "locks", owner_lock(run.run_id))
        kills: list[tuple[int, int]] = []
        try:
            # Stale (> 90 s) but under kill_after (600 s): alert only.
            rep = _reattach(conn, b, tmp_path, now=NOW + dt.timedelta(seconds=300),
                            kill=lambda p, s: kills.append((p, s)))  # fmt: skip
            assert rep.wedged == [_phash()] and rep.terminated == [] and kills == []
            rep = _reattach(conn, b, tmp_path, now=NOW + dt.timedelta(seconds=700),
                            kill=lambda p, s: kills.append((p, s)))  # fmt: skip
            assert kills == [(child.pid, signal.SIGTERM)] and rep.terminated == [child.pid]
            assert rep.adopted == [] and b.calls("cancel") == []  # adopted next tick
            assert "SIGTERM sent to pid" in reattach_notice(rep)
        finally:
            held.release()
        rep = _reattach(conn, b, tmp_path, now=NOW + dt.timedelta(seconds=760))
        assert len(rep.adopted) == 1
    finally:
        child.kill()
        child.wait()


def test_reattach_off_lists_only(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    rep = _reattach(conn, b, tmp_path, settings=settings(execution_reattach=False))
    assert len(rep.orphans) == 1 and rep.adopted == [] and not rep.enabled
    assert b.calls("cancel") == []
    assert ExecutionRepo(conn).get(_phash())["status"] == "working"  # type: ignore[index]
    assert "listed only" in rep.summary()


def test_dry_run_touches_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    rep = reattach(
        conn, None, NullLocks(), now=NOW + dt.timedelta(minutes=5), settings=settings(),
        run_id="dry",
    )  # fmt: skip
    (o,) = rep.orphans
    assert o.liveness.lock_held is None and o.liveness.pid_alive is False
    assert o.orders[0].client_order_id.endswith(".s0") and o.orders[0].limit_price == D("-0.85")
    assert rep.dry_run and rep.adopted == []
    assert ExecutionRepo(conn).get(_phash())["adopted_by_run_id"] is None  # type: ignore[index]


def test_manual_ladder_without_run_waits_out_its_bound(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=None)
    soon = _reattach(conn, b, tmp_path, now=NOW + dt.timedelta(seconds=60))
    assert soon.adopted == [] and soon.skipped_alive == [_phash()]
    later = _reattach(conn, b, tmp_path, now=NOW + dt.timedelta(minutes=30))
    assert len(later.adopted) == 1


# ---------------------------------------------------------------------------
# The adoption fence
# ---------------------------------------------------------------------------


def test_ladder_abandons_when_adopted(conn: sqlite3.Connection) -> None:
    """Adopted mid-poll: the old ladder writes nothing more (no fill, move or finish)."""

    class AdoptingBroker(L.ScriptedBroker):
        def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            conn.execute("UPDATE executions SET adopted_by_run_id = 'run-new'")
            conn.commit()
            return super().order_status(broker_order_id)

    b = AdoptingBroker([["filled"]], fills={0: (2, "-0.85")})
    with pytest.raises(ExecutionAdoptedError, match="adopted by run-new"):
        L.run(conn, b, run_id="run-old")
    assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == OrderState.SUBMITTED.value
    ex = ExecutionRepo(conn).get(_phash())
    assert ex is not None and ex["status"] == "working" and ex["finished_at"] is None
    assert conn.execute("SELECT count(*) FROM open_structures").fetchone()[0] == 0


def test_close_orphan_reduces_structure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A dead *exit* ladder that filled: re-attach closes the structure like the ladder."""
    opened = L.run(conn, L.ScriptedBroker([["filled"]], fills={0: (2, "-0.85")}))
    assert opened.structure_id is not None
    close_p = S.proposal(
        thesis="exit", limit_price=D("0.40"), expires_at=NOW + dt.timedelta(minutes=20)
    )
    from arc.store.repos import ProposalRepo

    ProposalRepo(conn).insert(
        candidate_id=close_p.candidate_id,
        proposal_hash=proposal_hash(close_p),
        structure_json=close_p.structure.model_dump_json(),
        thesis=close_p.thesis,
        quant_json=close_p.quant.model_dump_json(),
        sizing_json=close_p.sizing.model_dump_json(),
        expires_at=close_p.expires_at.isoformat(),
        ticker="SPY",
        kind="close",
    )
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    band = PriceBand(lo=D("0.40"), hi=D("0.40"), max_steps=0)
    _die(conn, b, run_id=run.run_id, p=close_p, decision=L.gated(close_p, band=band),
         kind="close", structure_id=opened.structure_id)  # fmt: skip
    b.set_status("fb-0", "filled", filled=2, price="0.40")
    (a,) = _reattach(conn, b, tmp_path).adopted
    assert a.kind == "close" and a.final_status == "filled"
    row = OpenStructureRepo(conn).get(opened.structure_id)
    assert row is not None and row["status"] == "closed" and D(row["close_net"]) == D("0.40")
    assert (
        conn.execute("SELECT count(*) FROM decisions WHERE reason_code = 'exit:closed'").fetchone()[
            0
        ]
        == 1
    )


def test_orphan_without_broker_id_uses_client_id_lookup(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Died between submit and recording the broker id: found by client id, else never sent."""
    for known in (True, False):
        c = connect(":memory:")
        migrate(c)
        run, _ = _claim(c)
        b = DyingBroker(tmp_path / f"b{known}.db")
        _die(c, b, run_id=run.run_id)
        c.execute("UPDATE orders SET broker_order_id = NULL, state = 'approved'")
        c.commit()
        if not known:
            with b._db() as db:
                db.execute("DELETE FROM orders")
        (a,) = _reattach(c, b, tmp_path).adopted
        state = c.execute("SELECT state FROM orders").fetchone()[0]
        if known:
            assert a.final_status == "cancelled" and a.cancelled_broker_order_ids == ["fb-0"]
            assert c.execute("SELECT broker_order_id FROM orders").fetchone()[0] == "fb-0"
            assert state == OrderState.CANCELLED.value
        else:
            assert a.final_status == "cancelled" and a.cancelled_broker_order_ids == []
            assert state == OrderState.CANCELLED.value
            assert (
                c.execute(
                    "SELECT count(*) FROM decisions WHERE reason_code = 'order:submit_failed'"
                ).fetchone()[0]
                == 1
            )


def test_reattach_while_halted_cancels_but_never_submits(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from arc.store.repos import HaltRepo

    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    HaltRepo(conn).halt(reason="owner", actor="test")
    conn.commit()
    submits = len(b.calls("submit"))
    (a,) = _reattach(conn, b, tmp_path).adopted
    assert a.final_status == "cancelled" and b.calls("cancel")
    assert len(b.calls("submit")) == submits


def test_fence_stops_old_ladder_before_any_write(conn: sqlite3.Connection) -> None:
    b = L.ScriptedBroker([["new"]])
    L.run(conn, b, run_id="old", config=L.cfg(execution_step_seconds=2))
    phash = _phash()
    conn.execute(
        "UPDATE executions SET adopted_by_run_id = 'new', status = 'working' "
        "WHERE proposal_hash = ?",
        (phash,),
    )
    c = LadderContext(
        conn=conn, broker=b, config=L.cfg(), clock=lambda: NOW, sleep=lambda _s: None,
        run_id="old", phash=phash, ticker="SPY",
    )  # fmt: skip
    before = conn.execute("SELECT count(*) FROM order_events").fetchone()[0]
    with pytest.raises(ExecutionAdoptedError, match="adopted by new"):
        c.move("whatever", OrderState.CANCELLED)
    with pytest.raises(ExecutionAdoptedError):
        c.fence()
    assert conn.execute("SELECT count(*) FROM order_events").fetchone()[0] == before
    c.run_id = "new"
    c.fence()  # the adopter itself passes


def test_second_reattach_cannot_steal_a_live_adoption(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from arc.execution.reattach import _fence

    dead, _ = _claim(conn)
    _die(conn, DyingBroker(tmp_path / "b.db"), run_id=dead.run_id)
    ph = _phash()
    adopter = RoutineRunRepo(conn).claim(
        job="broker.reattach", scheduled_for=NOW, reason="schedule", now=NOW
    )
    assert adopter is not None
    assert _fence(conn, ph, adopter.run_id, NOW)  # adopter is 'running'
    assert _fence(conn, ph, adopter.run_id, NOW)  # re-entrant for itself
    assert not _fence(conn, ph, "other", NOW)
    RoutineRunRepo(conn).finish(adopter.run_id, status=RunStatus.FAILED, now=NOW)
    assert _fence(conn, ph, "other", NOW)  # the first adopter died: take it over


# ---------------------------------------------------------------------------
# Job + config wiring
# ---------------------------------------------------------------------------


def test_job_registered_and_scheduled() -> None:
    from arc.routines.config import load_routines
    from arc.routines.handlers import BUILTIN_HANDLERS

    assert BUILTIN_HANDLERS["broker.reattach"] == "arc.broker.reattach_job:reattach_step"
    found = load_routines().job("broker.reattach")
    assert found is not None
    spec = found[1]
    assert spec.halt_exempt and not spec.llm


def test_settings_not_exposed() -> None:
    from arc.control.registry import NOT_EXPOSED

    for key in (
        "execution_reattach",
        "execution_reattach_stale_s",
        "execution_reattach_kill_after_s",
    ):
        assert key in NOT_EXPOSED
    s = settings()
    assert s.execution_reattach is True
    assert s.execution_reattach_stale_s == 90 and s.execution_reattach_kill_after_s == 600


def test_reattach_job_metrics_and_notice(conn: sqlite3.Connection, tmp_path: Path) -> None:
    run, _ = _claim(conn)
    b = DyingBroker(tmp_path / "b.db")
    _die(conn, b, run_id=run.run_id)
    clock = L.Clock()
    clock.t = NOW + dt.timedelta(minutes=5)
    ctx = SimpleNamespace(conn=conn, settings=settings(), run_id="run-job", clock=clock)
    locks = LockManager(tmp_path / "locks")
    res = reattach_job(ctx, broker=b, locks=locks, sleep=clock.sleep)  # type: ignore[arg-type]
    assert res.metrics["adopted"] == 1 and res.metrics["checked"] == 1
    assert res.notice.startswith("Re-attached SPY open")
    assert "1 adopted" in res.summary


class _Holder:
    """Hold a flock from a child process (flock is per open file description)."""

    def __init__(self, lock_dir: Path, name: str) -> None:
        LockManager(lock_dir).lock_dir.mkdir(parents=True, exist_ok=True)
        path = LockManager(lock_dir)._path(name)
        code = (
            "import fcntl,os,sys,time;"
            f"fd=os.open({str(path)!r},os.O_RDWR|os.O_CREAT,0o644);"
            "fcntl.flock(fd,fcntl.LOCK_EX);print('ok',flush=True);time.sleep(60)"
        )
        self.proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
        assert self.proc.stdout is not None and self.proc.stdout.readline().strip() == b"ok"

    def release(self) -> None:
        self.proc.kill()
        self.proc.wait()


# ---------------------------------------------------------------------------
# kill -9 of a real ladder process
# ---------------------------------------------------------------------------


@pytest.mark.serial
class TestKillNine:
    def _setup(self, tmp_path: Path) -> tuple[str, str, str, str]:
        db = str(tmp_path / "arc.db")
        conn = connect(db)
        migrate(conn)
        p = S.proposal(expires_at=NOW + dt.timedelta(minutes=20))
        L.seed(conn, p)
        ev = RoutineEventRepo(conn).emit("approval", {"proposal_hash": proposal_hash(p)}, now=NOW)
        conn.close()
        return db, str(tmp_path / "locks"), str(tmp_path / "broker.db"), ev.id

    def _spawn(self, args: tuple[str, str, str, str]) -> subprocess.Popen[bytes]:
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root)}
        return subprocess.Popen([sys.executable, "-m", "tests.ladder_child", *args], cwd=root,
                                env=env)  # fmt: skip

    def _wait_working(self, db: str, broker: FileBroker, child: subprocess.Popen[bytes]) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert child.poll() is None, "ladder child exited early"
            c = sqlite3.connect(db)
            row = c.execute(
                "SELECT run_id, pid, heartbeat_at FROM routine_runs WHERE job = 'broker'"
            ).fetchone()
            c.close()
            if row and row[1] and row[2] and broker.order_ids() and broker.calls("status"):
                return str(row[0])
            time.sleep(0.1)
        raise AssertionError("ladder never started working")

    @pytest.mark.parametrize("outcome", ["working", "filled", "partial"])
    def test_kill9_then_reattach(self, tmp_path: Path, outcome: str) -> None:
        args = self._setup(tmp_path)
        db, lock_dir, broker_db, _ = args
        broker = FileBroker(broker_db)
        child = self._spawn(args)
        try:
            run_id = self._wait_working(db, broker, child)
            conn = connect(db)
            # Alive: lock held by the child, heartbeat fresh -> left alone.
            live = reattach(
                conn, broker, LockManager(lock_dir), now=NOW + dt.timedelta(seconds=5),
                settings=settings(), run_id="probe",
            )  # fmt: skip
            assert live.adopted == [] and live.skipped_alive, live
            os.kill(child.pid, signal.SIGKILL)
            child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        if outcome == "filled":
            broker.set_status("fb-0", "filled", filled=2, price="-0.85")
        elif outcome == "partial":
            broker.set_status("fb-0", "partially_filled", filled=1, price="-0.85")
        submits = len(broker.calls("submit"))
        clock = L.Clock()
        clock.t = NOW + dt.timedelta(seconds=10)  # heartbeat fresh: the lock alone decides
        rep = reattach(
            conn, broker, LockManager(lock_dir), now=clock.t, settings=settings(),
            run_id="run-reattach", clock=clock, sleep=clock.sleep,
        )  # fmt: skip
        (a,) = rep.adopted
        want = {"working": "cancelled", "filled": "filled", "partial": "partially_filled"}
        assert a.final_status == want[outcome]
        assert len(broker.calls("submit")) == submits  # never resubmits
        assert broker.order_status("fb-0").status in ("canceled", "filled")
        assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == (
            0 if outcome == "working" else 1
        )
        dead = RoutineRunRepo(conn).get(run_id)
        assert dead is not None and dead.status is RunStatus.FAILED
