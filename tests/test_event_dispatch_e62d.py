"""E6.2d: approval events are dispatched once (no inline ladder, one run per event,
halted approvals held until !resume or their TTL), plus the execute/reconcile CLIs."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import pytest

from arc.approvals.service import ApprovalService, LogCardPoster, PostedCard
from arc.broker.ladder_job import LAPSED_UNDER_HALT, approval_events, execute_step
from arc.config import ArcSettings
from arc.execution.ladder import execute
from arc.gate import HaltSwitch, proposal_hash
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.runner import fixture_run
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult, RunEnv
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.runs import RoutineEventRepo, RoutineRunRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo, HaltRepo, ProposalRepo
from tests import test_execution_ladder as L
from tests import test_execution_submit as S

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ARC_AUTO_APPROVE", "ARC_AUTO_EXIT_DEFINED_RISK", "ARC_ENV", "ARC_GATE_SECRET"):
        monkeypatch.delenv(var, raising=False)


def margin(**kw: object) -> ArcSettings:
    # E7.5a scorecard gate would hold every open on this empty journal; these tests
    # cover D34 dispatch, not the gate (tests/test_auto_approve_d34.py covers that).
    kw.setdefault("auto_approve_scorecard_gate", False)
    return ArcSettings(_env_file=None, account_profile="margin", **kw)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Fixture pipeline DB: one gated proposal (SPY iron condor, expires 16:20 ET)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _pipeline_db() -> bytes:
    conn, report = fixture_run(margin(), load_routines())
    assert len(report.proposals) == 1
    return conn.serialize()


@pytest.fixture
def pconn(_pipeline_db: bytes) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.deserialize(_pipeline_db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("UPDATE gate_decisions SET token = ?", ("tok-fixture",))
    conn.commit()
    return conn


def _phash(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])


class Spawner:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.argv.append(list(argv))
        return 4242


CHAIN_YAML: dict[str, Any] = {
    "personas": {
        # 15:55: the fixture pipeline already holds the 16:00 slot for propose/execute.
        "loop": {
            "schedule": ["15:55"],
            "days": "trading",
            "chain": ["quant.propose", "broker.execute"],
        },
        "broker": {"trigger": "approval", "llm": False},
    },
    "steps": {
        "quant.propose": {"writes": [], "llm": False},
        "broker.execute": {"reads": ["proposal"], "writes": [], "llm": False},
    },
}


def _chain_dispatcher(
    conn: sqlite3.Connection, spawner: Spawner, investor_calls: list[str]
) -> Dispatcher:
    """research → propose → execute (D34), with the fixture proposal adopted by propose."""

    def research(ctx: JobContext) -> JobResult:
        return JobResult(summary="shortlist")

    def propose(ctx: JobContext) -> JobResult:
        # Adopt the fixture proposal into this chain (as if propose had written it).
        ctx.conn.execute(
            "UPDATE routine_runs SET chain_run_id = ?"
            " WHERE run_id = (SELECT run_id FROM proposals)",
            (ctx.chain_run_id,),
        )
        ctx.conn.commit()
        return JobResult(summary="1 proposal")

    def execute_(ctx: JobContext) -> JobResult:
        svc = ApprovalService(ctx.conn, ctx.settings, LogCardPoster())
        return execute_step(ctx, spawn=spawner, service=svc)

    def investor(ctx: JobContext) -> JobResult:
        investor_calls.append(str(ctx.event.payload["proposal_hash"]) if ctx.event else "?")
        # An inline ladder would block here for its whole step budget.
        time.sleep(ctx.settings.execution_step_seconds / 300)
        return JobResult(summary="ladder")

    return Dispatcher(
        conn,
        RoutinesConfig.model_validate(CHAIN_YAML),
        handlers={
            "loop": research,
            "quant.propose": propose,
            "broker.execute": execute_,
            "broker": investor,
        },
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
        settings_factory=lambda: margin(),
        run_env=RunEnv(db_path="/tmp/x.db", slack=False),
    )


# ---------------------------------------------------------------------------
# D34: execute claims before spawn; the same tick's drain never runs it inline
# ---------------------------------------------------------------------------


class TestDispatchOnce:
    def test_execute_step_then_tick_drain_runs_no_inline_ladder(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        sp, calls = Spawner(), []
        d = _chain_dispatcher(pconn, sp, calls)
        report = d.tick(FIXTURE_NOW, since=FIXTURE_NOW - dt.timedelta(minutes=10))
        jobs = [(o.job, o.status) for o in report.outcomes]
        assert ("broker.execute", "ok") in jobs
        assert all(o.job != "broker" for o in report.outcomes), jobs
        assert calls == [] and len(sp.argv) == 1
        (argv,) = sp.argv
        evt_id = argv[argv.index("--event") + 1]
        ev = RoutineEventRepo(pconn).get(evt_id)
        exec_run = next(o for o in report.outcomes if o.job == "broker.execute")
        assert ev is not None and ev.dispatched_by == exec_run.run_id and ev.consumed_at is None
        # The spawned run owns it: it runs and consumes it, joined to the chain.
        chain = argv[argv.index("--chain-run-id") + 1]
        out = d.run_event(
            "broker",
            ev,
            now=FIXTURE_NOW + dt.timedelta(seconds=2),
            chain_run_id=chain,
            parent_run_id=exec_run.run_id,
        )
        assert [o.status for o in out] == ["ok"] and calls == [_phash(pconn)]
        ev = RoutineEventRepo(pconn).get(evt_id)
        assert ev is not None and ev.consumed_by == [out[0].run_id]
        assert RoutineRunRepo(pconn).get(out[0].run_id).event_id == evt_id  # type: ignore[union-attr]
        # `arc context trace <chain>` shows the lifecycle on both runs.
        from arc.routines.cli import trace_runs

        steps = {s["job"]: s for s in trace_runs(pconn, chain)}
        (dispatched,) = steps["broker.execute"]["events"]
        (ran_for,) = steps["broker"]["events"]
        assert dispatched["role"] == "dispatched" and ran_for["role"] == "ran_for"
        assert ran_for["id"] == evt_id and ran_for["dispatched_by"] == exec_run.run_id
        assert ran_for["consumed_by"] == [out[0].run_id]
        # Later ticks and a replayed spawn never start a second ladder.
        d.tick(FIXTURE_NOW + dt.timedelta(minutes=5))
        again = d.run_event("broker", ev, now=FIXTURE_NOW + dt.timedelta(minutes=6))
        assert [o.status for o in again] == ["duplicate"] and calls == [_phash(pconn)]

    @pytest.mark.serial  # wall-clock budget: runs alone, after the parallel pass (Makefile)
    @pytest.mark.parametrize("step_seconds", [1, 900])
    def test_tick_never_blocks_on_ladder(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, step_seconds: int
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        monkeypatch.setenv("ARC_EXECUTION_STEP_SECONDS", str(step_seconds))
        assert margin().execution_step_seconds == step_seconds
        sp, calls = Spawner(), []
        d = _chain_dispatcher(pconn, sp, calls)
        t0 = time.monotonic()
        d.tick(FIXTURE_NOW, since=FIXTURE_NOW - dt.timedelta(minutes=10))
        elapsed = time.monotonic() - t0
        # An inline ladder would sleep step/300 s (3 s at 900); the tick only spawns.
        assert calls == [] and len(sp.argv) == 1
        assert elapsed < 2.0, elapsed

    def test_dispatch_is_atomic_and_release_returns_to_drain(
        self, pconn: sqlite3.Connection
    ) -> None:
        repo = RoutineEventRepo(pconn)
        ev = repo.emit("approval", {"proposal_hash": "x"}, now=FIXTURE_NOW)
        assert repo.dispatch(ev.id, by="run-a", now=FIXTURE_NOW)
        assert not repo.dispatch(ev.id, by="run-b", now=FIXTURE_NOW)
        assert repo.pending(until=FIXTURE_NOW) == []
        repo.release(ev.id)
        assert [e.id for e in repo.pending(until=FIXTURE_NOW)] == [ev.id]
        assert repo.consume(ev.id, ["r"], now=FIXTURE_NOW)
        assert not repo.consume(ev.id, ["r2"], now=FIXTURE_NOW)  # first consumer wins
        assert not repo.dispatch(ev.id, by="run-c", now=FIXTURE_NOW)  # consumed: no claim

    def test_spawn_failure_hands_event_back(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        calls: list[str] = []

        def boom(argv: Sequence[str]) -> int:
            raise OSError("fork failed")

        d = _chain_dispatcher(pconn, Spawner(), calls)
        d.handlers["broker.execute"] = lambda ctx: execute_step(
            ctx, spawn=boom, service=ApprovalService(ctx.conn, ctx.settings, LogCardPoster())
        )
        report = d.tick(FIXTURE_NOW, since=FIXTURE_NOW - dt.timedelta(minutes=10))
        ex = next(o for o in report.outcomes if o.job == "broker.execute")
        assert ex.metrics["spawn_failed"] == 1 and ex.metrics["dispatched"] == 0
        # Released in the same tick: the drain runs it (the fallback path), once.
        assert calls == [_phash(pconn)]
        assert [o.status for o in report.outcomes if o.job == "broker"] == ["ok"]


# ---------------------------------------------------------------------------
# One run per event: identical created_at no longer collides on the slot key
# ---------------------------------------------------------------------------


NOW = L.NOW


def _seed(conn: sqlite3.Connection, thesis: str) -> Any:
    p = S.proposal(
        candidate_id=f"c-{thesis}", thesis=thesis, expires_at=NOW + dt.timedelta(minutes=20)
    )
    CandidateRepo(conn).insert(
        ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7, id=p.candidate_id
    )
    ProposalRepo(conn).insert(
        candidate_id=p.candidate_id,
        proposal_hash=proposal_hash(p),
        structure_json=p.structure.model_dump_json(),
        thesis=p.thesis,
        quant_json=p.quant.model_dump_json(),
        sizing_json=p.sizing.model_dump_json(),
        expires_at=p.expires_at.isoformat(),
    )
    return p


def _investor_dispatcher(
    conn: sqlite3.Connection,
    calls: list[str],
    *,
    halted: list[bool] | None = None,
    ladder: dict[str, Any] | None = None,
    **kw: Any,
) -> Dispatcher:
    flag = halted if halted is not None else [False]

    def investor(ctx: JobContext) -> JobResult:
        ph = str(ctx.event.payload["proposal_hash"]) if ctx.event else "?"
        calls.append(ph)
        if ladder is not None:
            p = ladder[ph]
            b = L.ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
            c = L.Clock()
            out = execute(
                p, L.gated(p), L.approved(p), conn=ctx.conn, broker=b, config=L.cfg(),
                halt=HaltSwitch(HaltRepo(ctx.conn)), clock=c, sleep=c.sleep, run_id=ctx.run_id,
            )  # fmt: skip
            return JobResult(summary=str(out.status))
        return JobResult(summary="worked")

    return Dispatcher(
        conn,
        RoutinesConfig.model_validate(
            {"personas": {"broker": {"trigger": "approval", "llm": False}}}
        ),
        handlers={"broker": investor},
        notifier=RecordingNotifier(),
        is_halted=lambda: flag[0],
        settings_factory=lambda: margin(),
        **kw,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


class TestOneRunPerEvent:
    def test_two_approvals_same_second_both_execute(self, conn: sqlite3.Connection) -> None:
        pa, pb = _seed(conn, "a"), _seed(conn, "b")
        ha, hb = proposal_hash(pa), proposal_hash(pb)
        calls: list[str] = []
        d = _investor_dispatcher(conn, calls, ladder={ha: pa, hb: pb})
        events = RoutineEventRepo(conn)
        events.emit("approval", {"proposal_hash": ha}, now=NOW)
        events.emit("approval", {"proposal_hash": hb}, now=NOW)  # identical created_at
        report = d.tick(NOW + dt.timedelta(minutes=1), since=NOW)
        inv = [o for o in report.outcomes if o.job == "broker"]
        assert [o.status for o in inv] == ["ok", "ok"], [(o.status, o.reason) for o in inv]
        assert sorted(calls) == sorted([ha, hb])
        rows = conn.execute("SELECT proposal_hash, status FROM executions").fetchall()
        assert sorted(r[0] for r in rows) == sorted([ha, hb])
        assert {r[1] for r in rows} == {"filled"}
        assert all(o.status != "duplicate" for o in report.outcomes)
        assert events.pending(until=NOW + dt.timedelta(hours=1)) == []

    def test_scheduled_slot_key_still_unique(self, conn: sqlite3.Connection) -> None:
        runs = RoutineRunRepo(conn)
        assert runs.claim(job="scalp", scheduled_for=NOW, reason="schedule", now=NOW)
        assert runs.claim(job="scalp", scheduled_for=NOW, reason="schedule", now=NOW) is None
        # event runs neither collide with the slot nor with each other
        assert runs.claim(job="scalp", scheduled_for=NOW, reason="e", now=NOW, event_id="e1")
        assert runs.claim(job="scalp", scheduled_for=NOW, reason="e", now=NOW, event_id="e2")
        assert runs.claim(job="scalp", scheduled_for=NOW, reason="e", event_id="e1") is None
        assert runs.find("scalp", NOW).event_id is None  # type: ignore[union-attr]
        assert runs.for_event("scalp", "e2") is not None

    def test_migration_keeps_existing_runs(self) -> None:
        from arc.store.migrate import MIGRATIONS_DIR

        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if int(path.stem.split("_", 1)[0]) >= 18:
                break
            c.executescript(path.read_text())
        c.execute(
            """INSERT INTO routine_runs (run_id, job, reason, scheduled_for, status,
               config_version)
               VALUES ('r1', 'scalp', 'schedule', '2026-10-09T14:00:00Z', 'ok', 3)"""
        )
        c.commit()
        c.executescript((MIGRATIONS_DIR / "018_event_runs.sql").read_text())
        row = c.execute("SELECT * FROM routine_runs").fetchone()
        assert (row["run_id"], row["config_version"], row["event_id"]) == ("r1", 3, None)
        cols = {r[1] for r in c.execute("PRAGMA table_info(routine_events)")}
        assert {"dispatched_at", "dispatched_by"} <= cols


# ---------------------------------------------------------------------------
# Halted: approvals wait until !resume inside their TTL, else lapse on the record
# ---------------------------------------------------------------------------


class RecordingSlackPoster:
    """Stands in for SlackCardPoster (make_service(slack=True)) in lapse tests."""

    updated: list[tuple[str, str, Any]] = []

    def __init__(self, conn: sqlite3.Connection, client: Any = None) -> None:
        pass

    def post(self, day: dt.date, view: Any) -> PostedCard:  # pragma: no cover - unused
        return PostedCard(channel="C1", thread_ts="1.0", message_ts="1.1")

    def update(self, channel: str, message_ts: str, view: Any) -> None:
        type(self).updated.append((channel, message_ts, view))

    def notify_user(self, *a: Any) -> None:  # pragma: no cover - unused
        pass


def _approve_on_card(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> str:
    """Publish + auto-approve the fixture proposal; pretend its card is in Slack."""
    monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
    rep = ApprovalService(conn, margin(), LogCardPoster()).publish_pending(FIXTURE_NOW)
    (ph,) = rep.auto_approved
    conn.execute("UPDATE approval_requests SET channel = 'C1', message_ts = '1.1'")
    conn.commit()
    return ph


class TestHaltedApprovals:
    def test_halted_approval_retried_after_resume(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ph = _approve_on_card(pconn, monkeypatch)
        calls: list[str] = []
        halted = [True]
        d = _investor_dispatcher(pconn, calls, halted=halted)
        t1 = FIXTURE_NOW + dt.timedelta(minutes=1)
        r1 = d.tick(t1, since=FIXTURE_NOW)
        (o,) = [o for o in r1.outcomes if o.job == "broker"]
        assert o.status == "deferred" and "until !resume" in o.reason and calls == []
        assert len(RoutineEventRepo(pconn).pending(until=t1)) == 1  # still pending
        d.tick(t1 + dt.timedelta(minutes=5))  # still halted: still waiting
        assert calls == []
        halted[0] = False  # !resume, inside the 16:20 TTL
        r3 = d.tick(FIXTURE_NOW + dt.timedelta(minutes=12))
        assert [o.status for o in r3.outcomes if o.job == "broker"] == ["ok"]
        assert calls == [ph]
        assert RoutineEventRepo(pconn).pending(until=FIXTURE_NOW + dt.timedelta(hours=1)) == []
        assert not pconn.execute("SELECT 1 FROM decisions WHERE reason_text = ?",
                                 (LAPSED_UNDER_HALT,)).fetchone()  # fmt: skip

    def test_halted_approval_expires_with_journal(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arc.approvals.slack as slack_mod

        monkeypatch.setattr(slack_mod, "SlackCardPoster", RecordingSlackPoster)
        RecordingSlackPoster.updated = []
        ph = _approve_on_card(pconn, monkeypatch)
        calls: list[str] = []
        d = _investor_dispatcher(pconn, calls, halted=[True], run_env=RunEnv(slack=True))
        d.tick(FIXTURE_NOW + dt.timedelta(minutes=1), since=FIXTURE_NOW)
        late = FIXTURE_NOW + dt.timedelta(minutes=25)  # past the 16:20 TTL, still halted
        report = d.tick(late)
        (o,) = [o for o in report.outcomes if o.job == "broker"]
        assert o.status == "skipped" and o.reason == LAPSED_UNDER_HALT and calls == []
        row = pconn.execute(
            """SELECT persona, stage, choice, reason_code, reason_text, run_id FROM decisions
               WHERE proposal_hash = ? AND stage = 'order'""",
            (ph,),
        ).fetchone()
        assert dict(row) == {
            "persona": "broker",
            "stage": "order",
            "choice": "rejected",
            "reason_code": "order:refused",
            "reason_text": LAPSED_UNDER_HALT,
            "run_id": o.run_id,
        }
        ev = pconn.execute("SELECT consumed_at, consumed_by FROM routine_events").fetchone()
        assert ev["consumed_at"] and json.loads(ev["consumed_by"]) == [o.run_id]
        ((channel, ts, view),) = RecordingSlackPoster.updated
        assert (channel, ts) == ("C1", "1.1")
        assert "not executed: approval lapsed under halt" in json.dumps(view.blocks)
        # Consumed: a later tick (even resumed) never runs it.
        d2 = _investor_dispatcher(pconn, calls, halted=[False])
        d2.tick(late + dt.timedelta(minutes=5))
        assert calls == []

    def test_resumed_after_ttl_lapses_too(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _approve_on_card(pconn, monkeypatch)
        calls: list[str] = []
        halted = [True]
        d = _investor_dispatcher(pconn, calls, halted=halted)
        d.tick(FIXTURE_NOW + dt.timedelta(minutes=1), since=FIXTURE_NOW)
        halted[0] = False  # resumed, but only after 16:20
        r = d.tick(FIXTURE_NOW + dt.timedelta(minutes=30))
        assert [o.status for o in r.outcomes if o.job == "broker"] == ["skipped"]
        assert calls == []

    def test_non_approval_event_unchanged_under_halt(self, conn: sqlite3.Connection) -> None:
        calls: list[str] = []
        d = Dispatcher(
            conn,
            RoutinesConfig.model_validate({"personas": {"research": {"trigger": "news"}}}),
            handlers={"research": lambda ctx: calls.append("d") or JobResult(summary="x")},
            notifier=RecordingNotifier(),
            is_halted=lambda: True,
        )
        RoutineEventRepo(conn).emit("news", {}, now=NOW)
        r = d.tick(NOW + dt.timedelta(minutes=1), since=NOW)
        assert [o.status for o in r.outcomes] == ["skipped"] and calls == []
        assert RoutineEventRepo(conn).pending(until=NOW + dt.timedelta(hours=1)) == []

    def test_approval_for_unknown_proposal_is_not_held(self, conn: sqlite3.Connection) -> None:
        calls: list[str] = []
        d = _investor_dispatcher(conn, calls, halted=[True])
        RoutineEventRepo(conn).emit("approval", {"proposal_hash": "nope"}, now=NOW)
        r = d.tick(NOW + dt.timedelta(minutes=1), since=NOW)
        assert [o.status for o in r.outcomes] == ["skipped"]  # halted (persona), consumed

    def test_execute_halted_leaves_event_for_the_drain(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
        HaltSwitch(HaltRepo(pconn)).halt(reason="test", actor="t", now=FIXTURE_NOW)
        sp, calls = Spawner(), []
        d = _chain_dispatcher(pconn, sp, calls)
        # The loop chain starts un-halted; the halt lands mid-chain (execute reads it
        # from the DB), and the same tick's drain then sees it too.
        halted = [False]
        d._is_halted = lambda: halted[0]
        inner = d.handlers["broker.execute"]

        def execute_then_halted(ctx: JobContext) -> JobResult:
            res = inner(ctx)
            halted[0] = True
            return res

        d.handlers["broker.execute"] = execute_then_halted
        report = d.tick(FIXTURE_NOW, since=FIXTURE_NOW - dt.timedelta(minutes=10))
        ex = next(o for o in report.outcomes if o.job == "broker.execute")
        assert ex.summary.startswith("halted:") and "until !resume" in ex.summary
        assert sp.argv == [] and calls == []
        assert [o.status for o in report.outcomes if o.job == "broker"] == ["deferred"]
        ph = _phash(pconn)
        (evt,) = approval_events(pconn, [ph]).values()  # pending, not dispatched
        ev = RoutineEventRepo(pconn).get(evt)
        assert ev is not None and ev.dispatched_at is None and ev.consumed_at is None
        halted[0] = False  # !resume inside the TTL: the next tick's drain runs it
        d.tick(FIXTURE_NOW + dt.timedelta(minutes=5))
        assert calls == [ph]

    def test_run_event_while_halted_releases_to_drain(self, conn: sqlite3.Connection) -> None:
        p = _seed(conn, "a")
        calls: list[str] = []
        halted = [True]
        d = _investor_dispatcher(conn, calls, halted=halted)
        repo = RoutineEventRepo(conn)
        ev = repo.emit("approval", {"proposal_hash": proposal_hash(p)}, now=NOW)
        assert repo.dispatch(ev.id, by="run-exec", now=NOW)
        out = d.run_event("broker", ev, now=NOW + dt.timedelta(seconds=1))
        assert [o.status for o in out] == ["deferred"] and calls == []
        got = repo.get(ev.id)
        assert got is not None and got.dispatched_at is None and got.consumed_at is None
        halted[0] = False
        r = d.tick(NOW + dt.timedelta(minutes=2), since=NOW)
        assert [o.status for o in r.outcomes if o.job == "broker"] == ["ok"]
        assert calls == [proposal_hash(p)]

    def test_run_event_consumed_is_duplicate(self, conn: sqlite3.Connection) -> None:
        calls: list[str] = []
        d = _investor_dispatcher(conn, calls)
        repo = RoutineEventRepo(conn)
        ev = repo.emit("approval", {"proposal_hash": "x"}, now=NOW)
        repo.consume(ev.id, ["r0"], now=NOW)
        assert [o.status for o in d.run_event("broker", ev, now=NOW)] == ["duplicate"]
        assert calls == []

    def test_run_claim_records_pid_and_dispatcher_beats(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """E11.2 (D72): the run row carries this pid; ctx.heartbeat moves heartbeat_at;
        the run's owner lock is held while the handler runs and its file removed after."""
        from arc.routines.locks import LockBusyError, LockManager
        from arc.routines.runs import RoutineRunRepo, owner_lock

        seen: dict[str, Any] = {}
        locks = LockManager(tmp_path / "locks")
        later = NOW + dt.timedelta(seconds=30)

        def broker(ctx: JobContext) -> JobResult:
            got = RoutineRunRepo(ctx.conn).get(ctx.run_id)
            seen["pid"] = got.pid if got else None
            seen["first_beat"] = got.heartbeat_at if got else None
            ctx.heartbeat()
            got = RoutineRunRepo(ctx.conn).get(ctx.run_id)
            seen["beat"] = got.heartbeat_at if got else None
            try:
                with locks.hold(owner_lock(ctx.run_id)):
                    seen["lock"] = "free"
            except LockBusyError:
                seen["lock"] = "held"
            seen["run_id"] = ctx.run_id
            return JobResult(summary="ok")

        d = Dispatcher(
            conn,
            RoutinesConfig.model_validate(
                {"personas": {"broker": {"trigger": "approval", "llm": False}}}
            ),
            handlers={"broker": broker},
            notifier=RecordingNotifier(),
            locks=locks,
            is_halted=lambda: False,
            clock=lambda: later,
        )
        ev = RoutineEventRepo(conn).emit("approval", {"proposal_hash": "x"}, now=NOW)
        out = d.run_event("broker", ev, now=NOW)
        assert [o.status for o in out][:1] == ["ok"]
        assert seen["pid"] == os.getpid()
        assert seen["first_beat"] == NOW and seen["beat"] == later
        assert seen["lock"] == "held"
        assert not (tmp_path / "locks" / f"{owner_lock(seen['run_id'])}.lock").exists()


# ---------------------------------------------------------------------------
# arc execute / arc reconcile CLIs (exit codes)
# ---------------------------------------------------------------------------


SECRET = "g" * 40


def _json_out(text: str) -> dict[str, Any]:
    """The CLI's JSON document from captured stdout (structlog lines share the stream)."""
    start = text.index("\n{") + 1 if not text.startswith("{") else 0
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _cli_db(pconn: sqlite3.Connection, tmp_path: Path) -> str:
    ApprovalService(pconn, margin(), LogCardPoster()).publish_pending(FIXTURE_NOW)
    path = tmp_path / "cli.db"
    dst = sqlite3.connect(path)
    pconn.backup(dst)
    dst.close()
    return str(path)


def _exec_args(db: str, proposal: str, token: str) -> Any:
    import argparse

    return argparse.Namespace(db=db, proposal=proposal, token=token)


class TestExecuteCli:
    def test_execute_cli_refuses_without_secret(
        self,
        pconn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import arc.config
        from arc.execution.cli import run_execute

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=None))
        db = _cli_db(pconn, tmp_path)
        assert run_execute(_exec_args(db, _phash(pconn), "tok-fixture")) == 2
        out = _json_out(capsys.readouterr().out)
        assert out["status"] == "refused" and "ARC_GATE_SECRET" in out["detail"]

    def test_execute_cli_token_mismatch(
        self,
        pconn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import arc.config
        from arc.execution.cli import run_execute

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        db = _cli_db(pconn, tmp_path)
        assert run_execute(_exec_args(db, _phash(pconn)[:16], "tok-other")) == 2
        assert "not this proposal's gate token" in _json_out(capsys.readouterr().out)["detail"]

    def test_execute_cli_market_closed(
        self,
        pconn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import arc.config
        import arc.utils.calendar
        from arc.execution.cli import run_execute

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        monkeypatch.setattr(arc.utils.calendar, "is_open", lambda t: False)

        class NoBroker:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"broker.{name} called with the market closed")

        db = _cli_db(pconn, tmp_path)
        rc = run_execute(_exec_args(db, _phash(pconn), "tok-fixture"), broker=NoBroker())  # type: ignore[arg-type]
        assert rc == 1
        assert "market closed" in _json_out(capsys.readouterr().out)["detail"]

    def test_execute_cli_unknown_proposal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import arc.config
        from arc.execution.cli import run_execute

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        assert run_execute(_exec_args(str(tmp_path / "e.db"), "abc", "t")) == 1
        assert "no unique approval request" in _json_out(capsys.readouterr().out)["detail"]


class TestReconcileCli:
    def _args(self, db: str, *, no_halt: bool) -> Any:
        import argparse

        return argparse.Namespace(db=db, no_halt=no_halt, no_settle=True)

    def test_reconcile_cli_exit_1_on_mismatch_no_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import arc.config
        from arc.reconcile.cli import run_reconcile
        from tests.test_reconcile import FakeBroker

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        db = str(tmp_path / "r.db")
        rc = run_reconcile(self._args(db, no_halt=True), broker=FakeBroker(fail={"account"}))
        out = _json_out(capsys.readouterr().out)
        assert rc == 1 and out["clean"] is False
        assert any(m["kind"] == "broker_error" for m in out["mismatches"])
        c = connect(db)
        assert not HaltSwitch(HaltRepo(c)).is_halted()  # --no-halt: reported only

    def test_reconcile_cli_clean_exit_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import arc.config
        from arc.reconcile.cli import run_reconcile
        from tests.test_reconcile import FakeBroker

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        rc = run_reconcile(self._args(str(tmp_path / "r.db"), no_halt=False), broker=FakeBroker())
        out = _json_out(capsys.readouterr().out)
        assert rc == 0 and out["clean"] is True


# ---------------------------------------------------------------------------
# E6.2e: a dispatched event whose Investor never ran is reclaimed after the grace
# ---------------------------------------------------------------------------


GRACE = dt.timedelta(minutes=10)


def _dispatch_via_execute(
    pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> tuple[Dispatcher, str, str]:
    """Run the loop chain: execute claims the event and "spawns" a child that never runs."""
    monkeypatch.setenv("ARC_AUTO_APPROVE", "true")
    sp = Spawner()  # returns a pid, never runs the child
    d = _chain_dispatcher(pconn, sp, calls)
    report = d.tick(FIXTURE_NOW, since=FIXTURE_NOW - dt.timedelta(minutes=10))
    assert report.reclaimed == 0
    (argv,) = sp.argv
    evt_id = argv[argv.index("--event") + 1]
    exec_run = next(o for o in report.outcomes if o.job == "broker.execute")
    ev = RoutineEventRepo(pconn).get(evt_id)
    assert ev is not None and ev.dispatched_at == FIXTURE_NOW and ev.consumed_at is None
    assert ev.dispatched_by == exec_run.run_id
    return d, evt_id, str(exec_run.run_id)


class TestReclaimStranded:
    def test_dispatch_grace_config(self) -> None:
        assert load_routines().tick.dispatch_grace == GRACE
        cfg = RoutinesConfig.model_validate({"tick": {"dispatch_grace": "3m"}})
        assert cfg.tick.dispatch_grace == dt.timedelta(minutes=3)
        with pytest.raises(ValueError, match="dispatch_grace"):
            RoutinesConfig.model_validate({"tick": {"dispatch_grace": "0m"}})

    def test_dispatched_event_with_no_run_is_reclaimed_after_grace(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        calls: list[str] = []
        d, evt_id, exec_run = _dispatch_via_execute(pconn, monkeypatch, calls)
        # Inside the grace: the child may still be starting, nothing happens.
        early = d.tick(FIXTURE_NOW + GRACE - dt.timedelta(minutes=1))
        assert early.reclaimed == 0 and calls == []
        assert all(o.job != "broker" for o in early.outcomes)
        ev = RoutineEventRepo(pconn).get(evt_id)
        assert ev is not None and ev.dispatched_at is not None and ev.consumed_at is None
        # At the grace: released and run on the normal path, exactly once.
        with capture_logs() as logs:
            at = d.tick(FIXTURE_NOW + GRACE)
        (rec,) = [e for e in logs if e["event"] == "routines.event_reclaimed"]
        assert rec["event_id"] == evt_id and rec["dispatched_by"] == exec_run
        assert rec["age_s"] == GRACE.total_seconds()
        assert at.reclaimed == 1 and "reclaimed 1 stranded event(s)" in at.lines()[0]
        inv = [o for o in at.outcomes if o.job == "broker"]
        assert [o.status for o in inv] == ["ok"] and calls == [_phash(pconn)]
        ev = RoutineEventRepo(pconn).get(evt_id)
        assert ev is not None and ev.consumed_by == [inv[0].run_id]
        assert RoutineRunRepo(pconn).get(inv[0].run_id).event_id == evt_id  # type: ignore[arg-type,union-attr]
        # Later ticks never run it again.
        later = d.tick(FIXTURE_NOW + GRACE + dt.timedelta(minutes=5))
        assert later.reclaimed == 0 and calls == [_phash(pconn)]

    def test_event_whose_run_started_is_not_reclaimed(self, conn: sqlite3.Connection) -> None:
        p = _seed(conn, "a")
        repo = RoutineEventRepo(conn)
        ev = repo.emit("approval", {"proposal_hash": proposal_hash(p)}, now=NOW)
        assert repo.dispatch(ev.id, by="run-exec", now=NOW)
        # The child claimed its run (a long ladder still running): leave it alone.
        RoutineRunRepo(conn).claim(
            job="broker", scheduled_for=NOW, reason="event:approval", now=NOW, event_id=ev.id
        )
        calls: list[str] = []
        d = _investor_dispatcher(conn, calls)
        r = d.tick(NOW + GRACE + dt.timedelta(minutes=5), since=NOW)
        assert r.reclaimed == 0 and calls == []
        got = repo.get(ev.id)
        assert got is not None and got.dispatched_at == NOW and got.consumed_at is None
        assert repo.stranded(dispatched_before=NOW + dt.timedelta(hours=1)) == []
        assert not repo.reclaim(ev.id, dispatched_at=NOW)

    def test_reclaim_requires_the_same_dispatch(self, conn: sqlite3.Connection) -> None:
        repo = RoutineEventRepo(conn)
        ev = repo.emit("approval", {"proposal_hash": "x"}, now=NOW)
        assert repo.dispatch(ev.id, by="run-a", now=NOW)
        # A stale view (another dispatch since) never undoes the newer claim.
        assert not repo.reclaim(ev.id, dispatched_at=NOW - dt.timedelta(seconds=1))
        assert repo.reclaim(ev.id, dispatched_at=NOW)
        assert [e.id for e in repo.pending(until=NOW)] == [ev.id]

    def test_reclaimed_event_past_ttl_lapses_with_journal(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arc.approvals.slack as slack_mod
        from arc.broker.ladder_job import LAPSED_NOT_STARTED

        monkeypatch.setattr(slack_mod, "SlackCardPoster", RecordingSlackPoster)
        RecordingSlackPoster.updated = []
        calls: list[str] = []
        d, evt_id, _ = _dispatch_via_execute(pconn, monkeypatch, calls)
        pconn.execute("UPDATE approval_requests SET channel = 'C1', message_ts = '1.1'")
        pconn.commit()
        ph = _phash(pconn)
        d.run_env = RunEnv(slack=True)
        late = FIXTURE_NOW + dt.timedelta(minutes=25)  # past the 16:20 TTL (and the grace)
        report = d.tick(late)
        assert report.reclaimed == 1
        (o,) = [o for o in report.outcomes if o.job == "broker"]
        assert o.status == "skipped" and o.reason == LAPSED_NOT_STARTED and calls == []
        row = pconn.execute(
            """SELECT persona, stage, choice, reason_code, reason_text, run_id FROM decisions
               WHERE proposal_hash = ? AND stage = 'order'""",
            (ph,),
        ).fetchone()
        assert dict(row) == {
            "persona": "broker",
            "stage": "order",
            "choice": "rejected",
            "reason_code": "order:refused",
            "reason_text": LAPSED_NOT_STARTED,
            "run_id": o.run_id,
        }
        ev = RoutineEventRepo(pconn).get(evt_id)
        assert ev is not None and ev.consumed_by == [o.run_id]
        ((channel, ts, view),) = RecordingSlackPoster.updated
        assert (channel, ts) == ("C1", "1.1")
        assert f"not executed: {LAPSED_NOT_STARTED}" in json.dumps(view.blocks)
        d.tick(late + dt.timedelta(minutes=5))
        assert calls == []

    def test_reclaimed_while_halted_is_deferred(
        self, pconn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        d, evt_id, _ = _dispatch_via_execute(pconn, monkeypatch, calls)
        halted = [True]
        d._is_halted = lambda: halted[0]
        r = d.tick(FIXTURE_NOW + GRACE)
        assert r.reclaimed == 1
        assert [o.status for o in r.outcomes if o.job == "broker"] == ["deferred"]
        halted[0] = False  # !resume inside the TTL
        d.tick(FIXTURE_NOW + GRACE + dt.timedelta(minutes=5))
        assert calls == [_phash(pconn)]


class TestStrandedEventsCheck:
    def test_stranded_events_check_fires_once(self, conn: sqlite3.Connection) -> None:
        from arc.monitoring import alerts, checks

        routines = RoutinesConfig.model_validate({})
        repo = RoutineEventRepo(conn)
        ev = repo.emit("approval", {"proposal_hash": "abcdef0123456789"}, now=NOW)
        assert repo.dispatch(ev.id, by="run-exec", now=NOW)
        ok = checks.stranded_events(conn, routines, NOW + GRACE - dt.timedelta(minutes=1))
        assert ok.severity == "ok" and ok.findings == ()
        r = checks.stranded_events(conn, routines, NOW + GRACE)
        (f,) = r.findings
        assert r.severity == "failed" and f.severity == "failed"
        assert f.key == f"stranded:{ev.id}" and f.mode == checks.ONE_OFF
        assert "abcdef012345" in f.message and "run-exec" in f.message
        n = alerts.RecordingOpsNotifier()
        alerts.apply(conn, [r], now=NOW + GRACE, correlation={}, notifier=n)
        later = NOW + GRACE + dt.timedelta(minutes=30)
        alerts.apply(
            conn, [checks.stranded_events(conn, routines, later)], now=later,
            correlation={}, notifier=n,
        )  # fmt: skip
        assert len(n.posts) == 1 and ev.id in n.posts[0]
        # Once a run claims it (or the tick reclaims it), it is no longer stranded.
        RoutineRunRepo(conn).claim(
            job="broker", scheduled_for=NOW, reason="event:approval", now=NOW, event_id=ev.id
        )
        assert checks.stranded_events(conn, routines, later).severity == "ok"

    def test_health_check_includes_stranded_events(self, tmp_path: Path) -> None:
        import argparse

        from arc.monitoring.cli import run_checks

        c = connect(str(tmp_path / "h.db"))
        migrate(c)
        args = argparse.Namespace(config=None, no_gateway=True, no_remote=True)
        names = [r.name for r in run_checks(c, args, NOW)]
        assert "stranded_events" in names


class TestEventsCli:
    def test_events_lists_dispatched_unconsumed_with_age(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import argparse

        from arc.routines.cli import run_routines

        db = str(tmp_path / "ev.db")
        c = connect(db)
        migrate(c)
        repo = RoutineEventRepo(c)
        stranded = repo.emit("approval", {"proposal_hash": "aaaa1111bbbb2222"}, now=NOW)
        running = repo.emit("approval", {"proposal_hash": "cccc"}, now=NOW)
        fresh = repo.emit("approval", {"proposal_hash": "dddd"}, now=NOW)
        repo.emit("approval", {"proposal_hash": "pending-only"}, now=NOW)  # not dispatched
        done = repo.emit("approval", {"proposal_hash": "eeee"}, now=NOW)
        repo.dispatch(stranded.id, by="run-x", now=NOW)
        repo.dispatch(running.id, by="run-x", now=NOW)
        repo.dispatch(fresh.id, by="run-y", now=NOW + dt.timedelta(minutes=10))
        repo.dispatch(done.id, by="run-x", now=NOW)
        repo.consume(done.id, ["r"], now=NOW)
        RoutineRunRepo(c).claim(
            job="broker", scheduled_for=NOW, reason="e", now=NOW, event_id=running.id
        )
        c.close()
        now = (NOW + dt.timedelta(minutes=12)).isoformat()
        base = {"routines_command": "events", "db": db, "config": None, "now": now}
        assert run_routines(argparse.Namespace(**base, json=True)) == 0
        out = _json_out(capsys.readouterr().out)
        by_id = {e["id"]: e for e in out["events"]}
        assert set(by_id) == {stranded.id, running.id, fresh.id}
        assert by_id[stranded.id]["stranded"] is True and by_id[stranded.id]["age_s"] == 720
        assert by_id[running.id]["stranded"] is False and by_id[running.id]["run_id"]
        assert by_id[fresh.id]["stranded"] is False and by_id[fresh.id]["age_s"] == 120
        assert run_routines(argparse.Namespace(**base, json=False)) == 0
        text = capsys.readouterr().out
        assert "STRANDED" in text and "age 12m00s" in text and "aaaa1111bbbb" in text


# ---------------------------------------------------------------------------
# E11.1 (D71): reconcile.intraday event → one run of the real handler
# ---------------------------------------------------------------------------


class TestIntradayReconcileEvent:
    def _dispatcher(self, conn: sqlite3.Connection, broker: Any) -> tuple[Dispatcher, Any]:
        from arc.broker.reconcile_job import intraday_reconcile

        notifier = RecordingNotifier()
        cfg = load_routines()
        d = Dispatcher(
            conn,
            RoutinesConfig.model_validate(
                {
                    "personas": {
                        "reconcile.intraday": cfg.personas["reconcile.intraday"].model_dump(
                            exclude_none=True
                        )
                    }
                }
            ),  # fmt: skip
            handlers={"reconcile.intraday": lambda ctx: intraday_reconcile(ctx, broker=broker)},
            notifier=notifier,
            is_halted=lambda: HaltSwitch(HaltRepo(conn)).is_halted(),
            settings_factory=lambda: margin(gate_secret=SECRET),
        )
        return d, notifier

    def test_shipped_job_is_event_triggered_and_halt_exempt(self) -> None:
        from arc.routines.handlers import BUILTIN_HANDLERS as HANDLERS

        cfg = load_routines()
        job = cfg.personas["reconcile.intraday"]
        assert job.trigger == "reconcile.intraday" and job.halt_exempt and job.llm is False
        assert not job.schedule
        assert HANDLERS["reconcile.intraday"].endswith(":intraday_reconcile_step")

    def test_reconcile_intraday_event_runs_handler(self, conn: sqlite3.Connection) -> None:
        from tests.test_reconcile import ClientIdBroker, unconfirmed_order

        pos = unconfirmed_order(conn)
        d, notifier = self._dispatcher(conn, ClientIdBroker(by_coid={}, lookup_error=True))
        RoutineEventRepo(conn).emit(
            "reconcile.intraday", {"proposal_hash": pos["phash"], "reason": "t"}, now=NOW
        )
        r = d.tick(NOW + dt.timedelta(minutes=1), since=NOW)
        assert [(o.job, o.status) for o in r.outcomes] == [("reconcile.intraday", "ok")]
        assert RoutineEventRepo(conn).pending(until=NOW + dt.timedelta(hours=1)) == []
        assert any("still unknown" in text for _, text in notifier.posts), notifier.posts
        assert HaltSwitch(HaltRepo(conn)).is_halted()
        # a second tick does not run it again (dispatch once)
        r2 = d.tick(NOW + dt.timedelta(minutes=2), since=NOW + dt.timedelta(minutes=1))
        assert [o.job for o in r2.outcomes] == []

    def test_reconcile_intraday_runs_under_halt(self, conn: sqlite3.Connection) -> None:
        from tests.test_reconcile import ClientIdBroker, unconfirmed_order

        pos = unconfirmed_order(conn)
        HaltSwitch(HaltRepo(conn)).halt(actor="U1", reason="owner", now=NOW)
        d, notifier = self._dispatcher(conn, ClientIdBroker(by_coid={}))
        RoutineEventRepo(conn).emit("reconcile.intraday", {"proposal_hash": pos["phash"]}, now=NOW)
        r = d.tick(NOW + dt.timedelta(minutes=1), since=NOW)
        assert [(o.job, o.status) for o in r.outcomes] == [("reconcile.intraday", "ok")]
        assert notifier.posts == [] or all("still unknown" not in t for _, t in notifier.posts)

    def test_reconcile_cli_intraday_never_halts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import argparse

        import arc.config
        from arc.reconcile.cli import run_reconcile
        from tests.test_reconcile import ClientIdBroker, unconfirmed_order

        monkeypatch.setattr(arc.config, "get_settings", lambda **kw: margin(gate_secret=SECRET))
        db = str(tmp_path / "r.db")
        c = connect(db)
        migrate(c)
        unconfirmed_order(c)
        c.close()
        args = argparse.Namespace(db=db, no_halt=False, no_settle=False, intraday=True)
        rc = run_reconcile(args, broker=ClientIdBroker(by_coid={}, lookup_error=True))
        out = _json_out(capsys.readouterr().out)
        assert rc == 1 and out["clean"] is False and out["scope"] == "intraday"
        assert not HaltSwitch(HaltRepo(connect(db))).is_halted()
