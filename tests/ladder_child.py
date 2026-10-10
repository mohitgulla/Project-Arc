"""Subprocess entry for the E11.2 kill -9 tests: one real ladder run via ``run_event``.

``python -m tests.ladder_child <db> <lock_dir> <broker_db> <event_id>``

Claims the ``broker`` run for the event through the real
:meth:`~arc.routines.dispatcher.Dispatcher.run_event` (per-event lock + the run
lock, pid recorded, heartbeat wired), then works the ladder against
:class:`tests.file_broker.FileBroker`. The step is long (600 s) so the order stays
working until the test kills this process. The clock is ``NOW`` plus the real
elapsed time, so the gate token issued at ``NOW`` stays valid.
"""

from __future__ import annotations

import datetime as dt
import sys
import time

from arc.execution.ladder import execute
from arc.gate import HaltSwitch, issue_token, proposal_hash
from arc.gate.band import PriceBand
from arc.models import GateDecision, Proposal
from arc.routines.config import RoutinesConfig
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.locks import LockManager
from arc.routines.runs import RoutineEventRepo
from arc.store.db import connect
from arc.store.repos import HaltRepo
from tests import test_execution_ladder as L
from tests import test_execution_submit as S
from tests.file_broker import FileBroker

NOW = S.NOW
BAND = PriceBand(lo=L.BAND.lo, hi=L.BAND.hi, max_steps=0)  # one attempt: no next step


def make_proposal() -> Proposal:
    return S.proposal(expires_at=NOW + dt.timedelta(minutes=20))


def routines() -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        {"personas": {"broker": {"trigger": "approval", "llm": False}}}
    )


def main(argv: list[str]) -> int:
    db, lock_dir, broker_db, event_id = argv
    conn = connect(db)
    t0 = time.monotonic()

    def clock() -> dt.datetime:
        return NOW + dt.timedelta(seconds=time.monotonic() - t0)

    broker = FileBroker(broker_db)
    p = make_proposal()
    decision = issue_token(
        GateDecision(proposal_hash=proposal_hash(p), passed=True),
        p,
        secret=S.SECRET.encode(),
        now=NOW,
        band=BAND,
    )

    def handler(ctx: JobContext) -> JobResult:
        out = execute(
            p,
            decision,
            L.approved(p),
            conn=ctx.conn,
            broker=broker,
            config=L.cfg(execution_step_seconds=600, execution_poll_seconds=0.2),
            halt=HaltSwitch(HaltRepo(ctx.conn)),
            clock=clock,
            sleep=time.sleep,
            run_id=ctx.run_id,
            heartbeat=ctx.heartbeat,
        )
        return JobResult(summary=str(out.status))

    d = Dispatcher(
        conn,
        routines(),
        handlers={"broker": handler},
        notifier=RecordingNotifier(),
        locks=LockManager(lock_dir),
        is_halted=lambda: False,
        settings_factory=lambda: L.cfg(),
        clock=clock,
    )
    ev = RoutineEventRepo(conn).get(event_id)
    assert ev is not None
    d.run_event("broker", ev, now=NOW)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
