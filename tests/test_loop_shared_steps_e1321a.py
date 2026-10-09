"""E13.21a: a chain step shared by two chains no longer stops the second chain.

``exits.mandatory`` (and ``broker.execute``) are steps of both the ``research`` loop
chain and the ``positions.evaluate`` chain (``AUTO_CHAINS``, D56). At every :20/:50
slot both chains run in the same tick. Before this fix the second chain's
``exits.mandatory`` claim came back ``duplicate`` and the chain stopped: Research ran,
nothing was priced, and the truncated loop was recorded as the ``last_full_run``, so
the next slot was muted as ``no_change``.

Now:

* a later chain step another chain (or a standalone run) already ran for the slot is a
  ``duplicate`` on the record and the chain goes on; ``exits.mandatory`` runs once;
* ``broker.execute`` is chain-scoped (it publishes its own chain's proposals): one run
  per chain (migration 029);
* re-dispatching a chain for a slot it already ran still stops at step 0;
* a loop that ends before ``quant.propose`` (timeout, failure, duplicate stop) rolls
  ``last_full_run`` back, so the next slot with the same digest runs in full.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from arc.experiments.runner import arm_routines
from arc.ingest.scalp import load_fixture_docs
from arc.llm_routing import LLMRouting, Persona, TierSpec
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import open_db, pipeline_handlers
from arc.routines.config import AUTO_CHAINS, RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult
from arc.routines.headline import chain_facts, loop_headline
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.loop import LoopState
from arc.routines.runs import RoutineRunRepo, RunStatus
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import _settings

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

SLOT = dt.datetime(2026, 10, 7, 10, 20, tzinfo=ET)  # Wed, a :20 slot (both chains due)
RESEARCH = ["research", *AUTO_CHAINS["research"]]
POSITIONS = ["positions.evaluate", *AUTO_CHAINS["positions.evaluate"]]
AFTER_MANDATORY = RESEARCH[RESEARCH.index("exits.mandatory") + 1 :]
REMOTE = LLMRouting(
    tiers={"remote": TierSpec(model="anthropic/claude-x")},
    personas={p: "remote" for p in Persona},
)
SHIPPED = "shipped"  # loop.parallel_branches as in config/routines.yaml (D63)
SERIAL = "serial"  # loop.parallel_branches: [] (the rollback)


def _routines(mode: str, **loop: Any) -> RoutinesConfig:
    overrides: dict[tuple[str, ...], Any] = {("loop", k): v for k, v in loop.items()}
    if mode == SERIAL:
        overrides[("loop", "parallel_branches")] = []
    return load_routines(overrides=overrides)


class Fake:
    """Recording handlers for both chains (thread-safe: D63 branches)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stop: set[str] = set()
        self.fail: set[str] = set()
        self.slow: dict[str, float] = {}
        self.full_run = True  # research records last_full_run like _loop_record_full_run
        self._lock = threading.Lock()

    def handler(self, name: str) -> Callable[[JobContext], JobResult]:
        def run(ctx: JobContext) -> JobResult:
            with self._lock:
                self.calls.append(name)
            time.sleep(self.slow.get(name, 0.0))
            if name in self.fail:
                msg = f"{name} boom"
                raise RuntimeError(msg)
            if name == "research" and self.full_run and ctx.is_loop_run:
                LoopState(ctx.conn).record_digest("digest-1", ctx.now, full_run=True)
            metrics = {"no_change": False} if name == "research" else {}
            return JobResult(summary=f"{name} ok", metrics=metrics, stop_chain=name in self.stop)

        return run

    def handlers(self) -> dict[str, Callable[[JobContext], JobResult]]:
        return {n: self.handler(n) for n in {*RESEARCH, *POSITIONS}}


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _disp(conn: sqlite3.Connection, routines: RoutinesConfig, fake: Fake) -> Dispatcher:
    return Dispatcher(
        conn,
        routines,
        handlers=fake.handlers(),
        notifier=RecordingNotifier(),
        routing=REMOTE,
    )


def _rows(conn: sqlite3.Connection, job: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM routine_runs WHERE job = ? ORDER BY rowid", (job,)
    ).fetchall()


def _chain_of(conn: sqlite3.Connection, root: str) -> str:
    (row,) = _rows(conn, root)
    return str(row["chain_run_id"])


# ---------------------------------------------------------------------------
# the :20 / :50 tick: positions.evaluate and research in the same tick
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [SHIPPED, SERIAL])
class TestSharedSlot:
    def test_both_chains_due_research_still_prices(self, tmp_path: Path, mode: str) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        routines = arm_routines(_routines(mode), ["positions.evaluate", "research"])
        disp = _disp(conn, routines, fake)
        now = SLOT + dt.timedelta(seconds=4)
        report = disp.tick(now, since=SLOT - dt.timedelta(minutes=10))

        # the tick order is the live one: positions.evaluate's chain first
        roots = [o.job for o in report.outcomes if o.step_index == 0]
        assert roots == ["positions.evaluate", "research"]
        # exits.mandatory ran once for the slot (one routine_runs row, one handler call)
        mandatory = _rows(conn, "exits.mandatory")
        assert len(mandatory) == 1
        assert mandatory[0]["chain_run_id"] == _chain_of(conn, "positions.evaluate")
        assert fake.calls.count("exits.mandatory") == 1
        # research's chain records the duplicate and goes on
        research_chain = _chain_of(conn, "research")
        by_job = {o.job: o for o in report.outcomes if o.chain_run_id == research_chain}
        dup = by_job["exits.mandatory"]
        assert dup.status == "duplicate" and dup.metrics == {"shared": True}
        assert dup.run_id == mandatory[0]["run_id"]
        for step in AFTER_MANDATORY:
            assert by_job[step].status == "ok", (step, by_job[step].reason)
            (row,) = [r for r in _rows(conn, step) if r["chain_run_id"] == research_chain]
            assert row["status"] == RunStatus.OK.value
            assert row["step_index"] == RESEARCH.index(step)
        # broker.execute is chain-scoped: one run per chain
        execs = _rows(conn, "broker.execute")
        assert {r["chain_run_id"] for r in execs} == {
            research_chain,
            _chain_of(conn, "positions.evaluate"),
        }
        # the loop is a full one: last_full_run stays this slot
        assert LoopState(conn).last_full_run() == now
        assert not any(o.status == "failed" for o in report.outcomes)

    def test_research_first_then_positions_chain_still_executes(
        self, tmp_path: Path, mode: str
    ) -> None:
        """The other order (e.g. positions.evaluate deferred by a lock): research's
        chain runs exits.mandatory; the position chain's duplicate goes on to its own
        broker.execute."""
        conn = _db(tmp_path)
        fake = Fake()
        disp = _disp(conn, _routines(mode), fake)
        disp.run_job("research", SLOT, reason="schedule", now=SLOT)
        outs = disp.run_job("positions.evaluate", SLOT, reason="schedule", now=SLOT)
        assert [(o.job, o.status) for o in outs] == [
            ("positions.evaluate", "ok"),
            ("exits.mandatory", "duplicate"),
            ("broker.execute", "ok"),
        ]
        assert len(_rows(conn, "exits.mandatory")) == 1
        assert len(_rows(conn, "broker.execute")) == 2  # noqa: PLR2004 - one per chain

    def test_redispatch_of_the_research_chain_stops_at_step_0(
        self, tmp_path: Path, mode: str
    ) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        disp = _disp(conn, _routines(mode), fake)
        first = disp.run_job("research", SLOT, reason="schedule", now=SLOT)
        assert all(o.status == "ok" for o in first)
        calls = list(fake.calls)
        again = disp.run_job("research", SLOT, reason="schedule", now=SLOT)
        assert [(o.job, o.status) for o in again] == [("research", "duplicate")]
        assert again[0].metrics == {}
        assert fake.calls == calls  # no handler (no LLM) call at all
        assert len(_rows(conn, "research")) == 1


def test_standalone_run_of_a_shared_step_counts_as_another_chain(tmp_path: Path) -> None:
    """A row of the step with no chain (a standalone/manual record) for the slot: the
    loop's duplicate is shared and the loop goes on."""
    conn = _db(tmp_path)
    fake = Fake()
    disp = _disp(conn, _routines(SHIPPED), fake)
    solo = RoutineRunRepo(conn).claim(
        job="exits.mandatory", scheduled_for=SLOT, reason="manual", status=RunStatus.OK
    )
    assert solo is not None
    outs = {o.job: o for o in disp.run_job("research", SLOT, reason="schedule", now=SLOT)}
    assert outs["exits.mandatory"].status == "duplicate"
    assert outs["exits.mandatory"].run_id == solo.run_id
    assert outs["quant.propose"].status == "ok"


def test_skipped_step_record_applies_the_shared_rule(tmp_path: Path) -> None:
    """``_record_skipped_step`` (timeout / no_change) on a step another chain owns."""
    conn = _db(tmp_path)
    disp = _disp(conn, _routines(SHIPPED), Fake())
    RoutineRunRepo(conn).claim(
        job="quant.exit", scheduled_for=SLOT, reason="x", chain_run_id="other", step_index=2
    )
    out = disp._record_skipped_step(  # noqa: SLF001
        "quant.exit",
        SLOT,
        reason="chain:research",
        chain_run_id="mine",
        step_index=2,
        summary="timeout: loop exceeded 7m",
        now=SLOT,
    )
    assert out.status == "duplicate" and out.metrics == {"shared": True}
    # same chain: not shared
    RoutineRunRepo(conn).claim(
        job="risk.exit", scheduled_for=SLOT, reason="x", chain_run_id="mine", step_index=3
    )
    out = disp._record_skipped_step(  # noqa: SLF001
        "risk.exit",
        SLOT,
        reason="chain:research",
        chain_run_id="mine",
        step_index=3,
        summary="timeout",
        now=SLOT,
    )
    assert out.status == "duplicate" and out.metrics == {}


class TestClaimScope:
    def test_slot_scope_is_one_per_slot_across_chains(self, tmp_path: Path) -> None:
        runs = RoutineRunRepo(_db(tmp_path))
        a = runs.claim(job="x", scheduled_for=SLOT, reason="r", chain_run_id="a", step_index=1)
        b = runs.claim(job="x", scheduled_for=SLOT, reason="r", chain_run_id="b", step_index=1)
        assert a is not None and b is None
        assert runs.slot_owner("x", SLOT) == a

    def test_chain_scope_is_one_per_chain(self, tmp_path: Path) -> None:
        runs = RoutineRunRepo(_db(tmp_path))
        kw: dict[str, Any] = {"scheduled_for": SLOT, "reason": "r", "scope": "chain"}
        a = runs.claim(job="x", chain_run_id="a", step_index=2, **kw)
        b = runs.claim(job="x", chain_run_id="b", step_index=2, **kw)
        assert a is not None and b is not None
        assert runs.claim(job="x", chain_run_id="a", step_index=2, **kw) is None

    def test_chain_scope_loses_to_a_root_run(self, tmp_path: Path) -> None:
        runs = RoutineRunRepo(_db(tmp_path))
        assert runs.claim(job="x", scheduled_for=SLOT, reason="manual") is not None
        kw: dict[str, Any] = {"scheduled_for": SLOT, "reason": "r", "scope": "chain"}
        assert runs.claim(job="x", chain_run_id="a", step_index=2, **kw) is None

    def test_a_root_run_is_still_unique_per_slot_in_the_index(self, tmp_path: Path) -> None:
        """Migration 029: a doubled root insert is refused by the index itself."""
        conn = _db(tmp_path)
        sql = (
            "INSERT INTO routine_runs (run_id, job, step_index, reason, scheduled_for, status)"
            " VALUES (?, 'x', 0, 'r', '2026-10-07T14:20:00.000000Z', 'ok')"
        )
        conn.execute(sql, ("run-1",))
        with pytest.raises(Exception, match="UNIQUE"):
            conn.execute(sql, ("run-2",))

    def test_event_runs_keep_their_own_key(self, tmp_path: Path) -> None:
        runs = RoutineRunRepo(_db(tmp_path))
        kw: dict[str, Any] = {"job": "broker", "scheduled_for": SLOT, "reason": "event:approval"}
        assert runs.claim(event_id="ev-1", **kw) is not None
        assert runs.claim(event_id="ev-2", **kw) is not None
        assert runs.claim(event_id="ev-1", **kw) is None


# ---------------------------------------------------------------------------
# a truncated loop is not a full run (D31 no_change)
# ---------------------------------------------------------------------------


class TestTruncatedLoopIsNotFull:
    def _slot(self, disp: Dispatcher, at: dt.datetime = SLOT) -> dict[str, Any]:
        return {o.job: o for o in disp.run_job("research", at, reason="schedule", now=at)}

    @pytest.mark.parametrize("mode", [SHIPPED, SERIAL])
    def test_failed_branch_step_rolls_back(self, tmp_path: Path, mode: str) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.fail = {"quant.open"}
        outs = self._slot(_disp(conn, _routines(mode), fake))
        assert outs["quant.open"].status == "failed"
        assert "quant.propose" not in outs
        assert LoopState(conn).last_full_run() is None
        assert LoopState(conn).last_digest() is None

    def test_rollback_restores_the_previous_full_run(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        disp = _disp(conn, _routines(SHIPPED), fake)
        self._slot(disp)
        assert LoopState(conn).last_full_run() == SLOT
        fake.fail = {"quant.open"}
        self._slot(disp, SLOT + dt.timedelta(minutes=10))
        assert LoopState(conn).last_full_run() == SLOT  # not the truncated 10:30 loop
        assert LoopState(conn).last_digest() == "digest-1"

    def test_clean_stop_after_research_is_full(self, tmp_path: Path) -> None:
        """Research's own stop_chain (nothing to price) ends the loop cleanly."""
        conn = _db(tmp_path)
        fake = Fake()
        fake.stop = {"research"}
        outs = self._slot(_disp(conn, _routines(SHIPPED), fake))
        assert list(outs) == ["research"]
        assert LoopState(conn).last_full_run() == SLOT

    def test_duplicate_stop_before_propose_rolls_back(self, tmp_path: Path) -> None:
        """A lost claim to this chain's own row stops the chain: not a full loop."""
        conn = _db(tmp_path)
        fake = Fake()
        disp = _disp(conn, _routines(SERIAL), fake)
        orig = disp._run_steps  # noqa: SLF001

        def with_preclaimed(steps: list[str], *a: Any, **k: Any) -> Any:
            RoutineRunRepo(conn).claim(
                job="quant.exit",
                scheduled_for=SLOT,
                reason="x",
                chain_run_id=k["chain_run_id"],
                step_index=2,
            )
            return orig(steps, *a, **k)

        disp._run_steps = with_preclaimed  # type: ignore[method-assign]  # noqa: SLF001
        outs = self._slot(disp)
        assert outs["quant.exit"].status == "duplicate" and outs["quant.exit"].metrics == {}
        assert "quant.propose" not in outs
        assert LoopState(conn).last_full_run() is None


def _fixture_conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    return conn


def _fixture_disp(conn: sqlite3.Connection, routines: RoutinesConfig) -> Dispatcher:
    settings = _settings()
    return Dispatcher(
        conn,
        routines,
        handlers=pipeline_handlers(PipelineEnv.fixtures()),
        notifier=RecordingNotifier(),
        settings_factory=lambda: settings,
    )


def test_timeout_before_propose_then_same_digest_runs_in_full() -> None:
    """The card's repro on the real pipeline: slot 1 times out after Research (no
    quant.propose), slot 2 has the same inputs. Slot 2 must evaluate in full and
    price (quant.open ok), not report no_change."""
    slot0 = FIXTURE_NOW.astimezone(ET)
    conn = _fixture_conn()
    routines = load_routines()
    seed = _fixture_disp(conn, routines)
    (out,) = seed.run_job("scalp", slot0, reason="manual", now=slot0)
    assert out.status == "ok", out.reason

    # slot 1: Research alone eats the whole (1 s) loop budget
    slow_routines = load_routines(overrides={("loop", "max_runtime"): "1s"})
    slow = _fixture_disp(conn, slow_routines)
    handlers = pipeline_handlers(PipelineEnv.fixtures())
    real = handlers["research"]

    def slow_research(ctx: Any) -> Any:
        time.sleep(1.1)
        return real(ctx)

    handlers["research"] = slow_research
    slow.handlers = handlers
    first = {o.job: o for o in slow.run_job("research", slot0, reason="schedule", now=slot0)}
    assert first["research"].status == "ok"
    assert first["research"].metrics["no_change"] is False
    digest = first["research"].metrics["loop_digest"]
    assert first["quant.open"].status == "skipped" and "timeout" in first["quant.open"].reason
    assert first["quant.propose"].status == "skipped"
    assert LoopState(conn).last_full_run() is None  # rolled back
    assert LoopState(conn).last_digest() is None

    # slot 2, 10 min later, identical inputs: a full evaluation that prices
    at = slot0 + dt.timedelta(minutes=10)
    disp = _fixture_disp(conn, routines)
    second = {o.job: o for o in disp.run_job("research", at, reason="schedule", now=at)}
    assert second["research"].metrics["no_change"] is False, second["research"].summary
    assert second["research"].metrics["loop_digest"] == digest
    assert second["quant.open"].status == "ok"
    assert second["quant.propose"].status == "ok"
    assert LoopState(conn).last_full_run() == at


# ---------------------------------------------------------------------------
# headline: a loop that continued past the duplicate is not "stopped"
# ---------------------------------------------------------------------------


def test_headline_of_a_loop_past_a_shared_duplicate(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    disp = _disp(conn, arm_routines(_routines(SHIPPED), ["positions.evaluate", "research"]), Fake())
    disp.tick(SLOT + dt.timedelta(seconds=4), since=SLOT - dt.timedelta(minutes=10))
    chain = _chain_of(conn, "research")
    run_id = conn.execute(
        "SELECT run_id FROM routine_runs WHERE chain_run_id = ? AND job = 'research'", (chain,)
    ).fetchone()[0]
    stamp = SLOT.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for t in ("NFLX", "GCT", "PLTR"):
        conn.execute(
            """INSERT INTO decisions (id, chain_run_id, run_id, persona, stage, subject,
                   choice, reason_code, payload, at, reason_text)
               VALUES (?, ?, ?, 'research', 'shortlist', ?, 'selected', 'shortlisted',
                       '{"stance": "bullish"}', ?, '')""",
            (f"dec-{t}", chain, run_id, t, stamp),
        )
    conn.commit()
    facts = chain_facts(conn, chain)
    assert "stopped" not in facts.blocked
    assert "stopped before pricing" not in " ".join(loop_headline(conn, chain))
