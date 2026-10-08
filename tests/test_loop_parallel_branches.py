"""E13.21 (D63): the loop's exit and open branches run side by side.

``loop.parallel_branches`` forks the research chain after ``exits.mandatory`` into
the exit branch (``quant.exit -> risk.exit``) and the open branch (``quant.open ->
risk.open -> quant.revise``); each runs in its own thread on its own SQLite
connection and they join before ``quant.propose``. Every per-step rule applies per
branch; ``parallel_branches: []`` is the serial rollback.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from arc.llm_routing import LLMRouting, Persona, TierSpec
from arc.routines.config import AUTO_CHAINS, RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher, branch_indices
from arc.routines.handlers import JobContext, JobResult, JobSkippedError
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.loop import LoopState
from arc.routines.manifest import ManifestRepo
from arc.routines.runs import RoutineRunRepo, RunStatus
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

SLOT = dt.datetime(2026, 10, 7, 10, 0, tzinfo=ET)
CHAIN = ["research", *AUTO_CHAINS["research"]]
EXIT = ["quant.exit", "risk.exit"]
OPEN = ["quant.open", "risk.open", "quant.revise"]
REMOTE = LLMRouting(
    tiers={"remote": TierSpec(model="anthropic/claude-x")},
    personas={p: "remote" for p in Persona},
)
LOCAL = LLMRouting(
    tiers={
        "remote": TierSpec(model="anthropic/claude-x"),
        "local": TierSpec(model="ollama/qwen3", local=True),
    },
    personas={p: ("local" if p is Persona.RISK else "remote") for p in Persona},
)


def _routines(**loop: Any) -> RoutinesConfig:
    return load_routines(overrides={("loop", k): v for k, v in loop.items()})


SERIAL = {"parallel_branches": []}


class Fake:
    """Handlers that sleep, record (start, end, thread) and can fail/skip/stop."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.delays: dict[str, float] = {}
        self.spans: dict[str, tuple[float, float, int]] = {}
        self.fail: set[str] = set()
        self.skip: set[str] = set()
        self.stop: set[str] = set()
        self.no_change = False
        self.calls: list[str] = []
        self.conns: dict[str, int] = {}
        self._lock = threading.Lock()

    def handler(self, name: str) -> Callable[[JobContext], JobResult]:
        def run(ctx: JobContext) -> JobResult:
            t0 = time.monotonic()
            with self._lock:
                self.calls.append(name)
                self.conns[name] = id(ctx.conn)
            time.sleep(self.delays.get(name, self.delay))
            with self._lock:
                self.spans[name] = (t0, time.monotonic(), threading.get_ident())
            if name in self.fail:
                msg = f"{name} boom"
                raise RuntimeError(msg)
            if name in self.skip:
                raise JobSkippedError("nothing to do", continue_chain=True)
            metrics = {"no_change": self.no_change} if name == "research" else {}
            return JobResult(summary=f"{name} ok", metrics=metrics, stop_chain=name in self.stop)

        return run

    def handlers(self) -> dict[str, Callable[[JobContext], JobResult]]:
        return {n: self.handler(n) for n in CHAIN}


def _db(tmp_path: Path, name: str = "arc.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    migrate(conn)
    return conn


def _disp(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    fake: Fake,
    *,
    routing: LLMRouting = REMOTE,
    notifier: RecordingNotifier | None = None,
) -> Dispatcher:
    return Dispatcher(
        conn,
        routines,
        handlers=fake.handlers(),
        notifier=notifier or RecordingNotifier(),
        routing=routing,
    )


def _slot(disp: Dispatcher) -> dict[str, Any]:
    return {o.job: o for o in disp.run_job("research", SLOT, reason="schedule", now=SLOT)}


def _rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """routine_runs by job, plus each run's manifest ``parent_run_id``."""
    parents = {
        m.run_id: m.parent_run_id
        for r in conn.execute("SELECT run_id FROM routine_runs")
        for m in ManifestRepo(conn).for_run(r["run_id"])
    }
    return {
        r["job"]: {**dict(r), "parent_run_id": parents.get(r["run_id"])}
        for r in conn.execute(
            "SELECT job, status, step_index, run_id, chain_run_id, summary FROM routine_runs"
        )
    }


def _overlap(fake: Fake, a: str, b: str) -> bool:
    (a0, a1, _), (b0, b1, _) = fake.spans[a], fake.spans[b]
    return a0 < b1 and b0 < a1


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_branches(self) -> None:
        r = load_routines()
        assert [list(b) for b in r.loop.parallel_branches] == [EXIT, OPEN]
        assert branch_indices(r, CHAIN) == [[2, 3], [4, 5, 6]]
        assert branch_indices(_routines(**SERIAL), CHAIN) == []
        assert branch_indices(r, ["positions.evaluate", "exits.mandatory"]) == []

    def test_shipped_branches_are_independent_by_reads_and_writes(self) -> None:
        """Computed from the steps' reads/writes: fails if a future change makes one
        branch read (or write) a non-accumulate kind the other branch writes."""
        r = load_routines()
        assert r.branch_conflicts(r.loop.parallel_branches) == []
        for b_exit in EXIT:
            for b_open in OPEN:
                w_exit = set(r.step(b_exit)[1].writes or [])
                w_open = set(r.step(b_open)[1].writes or [])
                accumulate = {
                    k
                    for k in (w_exit | w_open)
                    if r.context_policy(k, b_exit).supersede.value == "accumulate"
                    or r.context_policy(k, b_open).supersede.value == "accumulate"
                }
                assert not (w_exit & set(r.step(b_open)[1].reads or [])) - accumulate
                assert not (w_open & set(r.step(b_exit)[1].reads or [])) - accumulate

    def test_cross_branch_read_is_rejected(self) -> None:
        # risk.open reading exit_case (written by quant.exit) would make the open
        # branch depend on the exit branch.
        reads = list(load_routines().step("risk.open")[1].reads or [])
        with pytest.raises(ValueError, match="risk.open reads 'exit_case', written by quant.exit"):
            load_routines(overrides={("steps", "risk.open", "reads"): [*reads, "exit_case"]})

    def test_shared_write_is_rejected_unless_accumulate(self) -> None:
        writes = list(load_routines().step("risk.exit")[1].writes or [])
        with pytest.raises(ValueError, match="both write 'structures'"):
            load_routines(overrides={("steps", "risk.exit", "writes"): [*writes, "structures"]})
        # note is accumulate: both branches append it today
        r = load_routines()
        assert "note" in (r.step("quant.exit")[1].writes or [])
        assert "note" in (r.step("quant.open")[1].writes or [])

    @pytest.mark.parametrize(
        ("branches", "match"),
        [
            ([["quant.exit", "risk.exit"], ["risk.open", "quant.revise"]], "no gap"),
            ([["risk.exit", "quant.exit"], OPEN], "consecutive chain steps in order"),
            ([["quant.exit"], ["quant.open", "quant.revise"]], "consecutive chain steps"),
            ([["exits.mandatory", "quant.exit"], OPEN], "deterministic"),
            ([EXIT, [*OPEN, "quant.propose"]], "deterministic"),
            ([EXIT, ["quant.open", "nope"]], "not a step of the research chain"),
            ([EXIT], "at least two"),
            ([EXIT, []], "at least two"),
            ([EXIT, ["quant.exit", "quant.open"]], "listed twice"),
        ],
    )
    def test_bad_branches_rejected(self, branches: list[list[str]], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            _routines(parallel_branches=branches)


# ---------------------------------------------------------------------------
# fork / join
# ---------------------------------------------------------------------------


class TestParallel:
    def test_branches_overlap_and_wall_is_max_not_sum(self, tmp_path: Path) -> None:
        def run(r: RoutinesConfig, name: str) -> tuple[Fake, dict[str, Any], float]:
            fake = Fake()
            fake.delays = {s: 0.5 for s in [*EXIT, *OPEN]}
            conn = _db(tmp_path, name)
            t0 = time.monotonic()
            outs = _slot(_disp(conn, r, fake))
            return fake, outs, time.monotonic() - t0

        _, _, serial_wall = run(_routines(**SERIAL), "serial.db")
        fake, outs, wall = run(load_routines(), "parallel.db")
        conn = _db(tmp_path, "parallel.db")
        assert [o.status for o in outs.values()] == ["ok"] * len(CHAIN)
        assert _overlap(fake, "quant.exit", "quant.open")
        assert _overlap(fake, "risk.exit", "risk.open")
        assert fake.spans["quant.exit"][2] != fake.spans["quant.open"][2]
        # within a branch the order holds
        assert fake.spans["quant.exit"][1] <= fake.spans["risk.exit"][0]
        assert fake.spans["quant.open"][1] <= fake.spans["risk.open"][0]
        assert fake.spans["risk.open"][1] <= fake.spans["quant.revise"][0]
        # the join: quant.propose starts after both branches end
        assert fake.spans["quant.propose"][0] >= fake.spans["risk.exit"][1]
        assert fake.spans["quant.propose"][0] >= fake.spans["quant.revise"][1]
        # exit branch 1.0 s + open branch 1.5 s: serial pays the sum, parallel the max
        assert serial_wall >= 2.5
        assert wall < serial_wall - 0.7
        # own connection per branch, never the dispatcher's
        assert fake.conns["quant.exit"] != fake.conns["quant.open"]
        assert fake.conns["research"] not in {fake.conns["quant.exit"], fake.conns["quant.open"]}
        summary = LoopState(conn).chain_summary(outs["research"].chain_run_id)
        assert summary is not None and summary["parallel"] is True
        assert summary["loop_wall_ms"] < sum(summary["durations_ms"].values())
        assert set(summary["durations_ms"]) == set(CHAIN)

    def test_rows_keep_declared_index_chain_and_parent_links(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        outs = _slot(_disp(conn, load_routines(), Fake()))
        rows = _rows(conn)
        assert [o.job for o in outs.values()] == CHAIN
        assert {r["step_index"] for r in rows.values()} == set(range(len(CHAIN)))
        for job, row in rows.items():
            assert row["step_index"] == CHAIN.index(job)
        assert len({r["chain_run_id"] for r in rows.values()}) == 1
        mandatory = rows["exits.mandatory"]["run_id"]
        assert rows["quant.exit"]["parent_run_id"] == mandatory
        assert rows["quant.open"]["parent_run_id"] == mandatory
        assert rows["risk.exit"]["parent_run_id"] == rows["quant.exit"]["run_id"]
        assert rows["quant.revise"]["parent_run_id"] == rows["risk.open"]["run_id"]
        # after the join: the last declared branch
        assert rows["quant.propose"]["parent_run_id"] == rows["quant.revise"]["run_id"]

    def test_failure_in_one_branch_lets_the_other_finish_then_stops(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.fail = {"quant.exit"}
        fake.delays = {"risk.open": 0.2}
        outs = _slot(_disp(conn, load_routines(), fake))
        assert outs["quant.exit"].status == "failed"
        assert "risk.exit" not in outs  # its branch ended
        assert [outs[s].status for s in OPEN] == ["ok", "ok", "ok"]  # the other finished
        # the existing stop rule after the join: a failed step stops the chain
        assert "quant.propose" not in outs and "broker.execute" not in outs
        assert "quant.propose" not in fake.calls

    def test_stop_chain_in_a_branch_stops_after_the_join(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.stop = {"risk.exit"}
        outs = _slot(_disp(conn, load_routines(), fake))
        assert outs["risk.exit"].status == "ok"
        assert [outs[s].status for s in OPEN] == ["ok", "ok", "ok"]
        assert "quant.propose" not in outs

    def test_optional_skip_continues(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.skip = {"quant.revise"}
        outs = _slot(_disp(conn, load_routines(), fake))
        assert outs["quant.revise"].status == "skipped"
        assert outs["quant.propose"].status == "ok" and outs["broker.execute"].status == "ok"

    def test_deadline_applies_per_branch(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.delays = {"exits.mandatory": 1.2}
        outs = _slot(_disp(conn, _routines(max_runtime="1s"), fake))
        for step in [*EXIT, *OPEN]:
            assert outs[step].status == "skipped"
            assert outs[step].reason.startswith("timeout")
        assert not set(fake.calls) & {*EXIT, *OPEN}

    def test_min_remaining_uses_the_shared_deadline(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.delays = {"quant.exit": 0.6, "risk.exit": 0.6, "quant.open": 0.6, "risk.open": 0.6}
        r = _routines(max_runtime="3s")
        steps = dict(r.steps)
        steps["quant.revise"] = steps["quant.revise"].model_copy(update={"min_remaining_s": 2})
        for s in EXIT:  # shipped 60 s each; out of this 3 s budget's way
            steps[s] = steps[s].model_copy(update={"min_remaining_s": None})
        r = r.model_copy(update={"steps": steps})
        outs = _slot(_disp(conn, r, fake))
        # 3 s budget - 1.2 s spent before quant.revise < 2 s: skipped; the tail still runs
        assert outs["quant.revise"].status == "skipped"
        assert outs["quant.revise"].reason.startswith("step_skipped_deadline")
        assert "quant.revise" not in fake.calls
        assert outs["risk.exit"].status == "ok"
        assert outs["quant.propose"].status == "ok"

    def test_no_change_skips_the_llm_steps_in_both_branches(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.no_change = True
        outs = _slot(_disp(conn, load_routines(), fake))
        for step in [*EXIT, *OPEN, "quant.propose"]:  # every on_no_change: skip step
            assert outs[step].status == "skipped"
            assert outs[step].reason.startswith("no_change")
        assert outs["broker.execute"].status == "ok"
        assert fake.calls == ["research", "exits.mandatory", "broker.execute"]

    def test_resume_after_a_crash_mid_branch(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        fake = Fake()
        fake.fail = {"risk.open"}
        outs = _slot(_disp(conn, load_routines(), fake))
        chain_id = outs["research"].chain_run_id
        assert outs["risk.open"].status == "failed"
        fake2 = Fake()
        again = _disp(conn, load_routines(), fake2).resume_chain(chain_id, now=SLOT)
        by = {o.job: o for o in again}
        assert fake2.calls == ["risk.open", "quant.revise", "quant.propose", "broker.execute"]
        assert by["quant.exit"].reason == "already done (resume)"
        assert by["quant.open"].reason == "already done (resume)"
        assert all(by[s].status == "ok" for s in CHAIN)

    def test_duplicate_claim_in_a_branch(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        disp = _disp(conn, load_routines(), Fake())
        chain = "chain-dup"
        RoutineRunRepo(conn).claim(
            job="quant.open",
            scheduled_for=SLOT,
            reason="chain:research",
            chain_run_id="other",
            step_index=4,
            now=SLOT,
        )
        outs = {
            o.job: o
            for o in disp._run_steps(  # noqa: SLF001 - the chain runner, with a fixed id
                CHAIN, SLOT, reason="schedule", now=SLOT, chain_run_id=chain
            )
        }
        assert outs["quant.open"].status == "duplicate"
        assert outs["risk.exit"].status == "ok"
        assert "quant.propose" not in outs

    def test_a_branch_exception_is_raised_on_the_parent(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        disp = _disp(conn, load_routines(), Fake())

        def boom(*_a: Any, **_k: Any) -> Any:
            msg = "child broke"
            raise RuntimeError(msg)

        disp._branch_child = boom  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(RuntimeError, match="child broke"):
            disp._run_steps(  # noqa: SLF001
                CHAIN, SLOT, reason="schedule", now=SLOT, chain_run_id="chain-x"
            )

    def test_slack_cards_still_thread_under_the_loop_root(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        notifier = RecordingNotifier()
        outs = _slot(_disp(conn, load_routines(), Fake(), notifier=notifier))
        assert outs["broker.execute"].status == "ok"
        (root,) = notifier.roots
        meta = [t for t in notifier.in_thread(root) if "[Routines]" in t]
        assert meta, notifier.posts
        assert "wall=" in meta[-1] and "parallel" in meta[-1]
        assert "quant.exit=" in meta[-1] and "quant.open=" in meta[-1]


# ---------------------------------------------------------------------------
# serial fallbacks and the rollback
# ---------------------------------------------------------------------------


def _shape(outs: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(o.job, o.status, o.step_index, o.summary) for o in outs.values()]


class TestSerial:
    @pytest.mark.parametrize(
        "setup",
        ["ok", "fail_exit", "fail_open", "stop", "skip", "no_change"],
    )
    def test_empty_branches_is_the_serial_chain(self, tmp_path: Path, setup: str) -> None:
        def run(r: RoutinesConfig, name: str) -> tuple[list[Any], list[str]]:
            fake = Fake()
            fake.fail = {"quant.exit"} if setup == "fail_exit" else set()
            fake.fail |= {"risk.open"} if setup == "fail_open" else set()
            fake.stop = {"risk.exit"} if setup == "stop" else set()
            fake.skip = {"quant.revise"} if setup == "skip" else set()
            fake.no_change = setup == "no_change"
            outs = _slot(_disp(_db(tmp_path, name), r, fake))
            return _shape(outs), fake.calls

        serial, serial_calls = run(_routines(**SERIAL), "serial.db")
        parallel, _ = run(load_routines(), "parallel.db")
        # serial: today's order exactly
        assert serial_calls == [j for j in CHAIN if j in serial_calls]
        if setup == "ok":
            assert serial == parallel
        if setup in {"fail_exit", "stop"}:
            # serial stops at the exit branch; parallel also ran the open branch
            assert not {"quant.open", "risk.open"} & {s[0] for s in serial}
        if setup == "fail_open":
            assert serial == parallel

    def test_serial_rows_are_linear(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        _slot(_disp(conn, _routines(**SERIAL), Fake()))
        rows = _rows(conn)
        for prev, nxt in zip(CHAIN, CHAIN[1:], strict=False):
            assert rows[nxt]["parent_run_id"] == rows[prev]["run_id"]

    def test_local_tier_falls_back_to_serial(self, tmp_path: Path) -> None:
        """D39: risk runs on a local model, so the branches must not both run one."""
        conn = _db(tmp_path)
        fake = Fake()
        fake.delays = {s: 0.1 for s in [*EXIT, *OPEN]}
        outs = _slot(_disp(conn, load_routines(), fake, routing=LOCAL))
        assert all(o.status == "ok" for o in outs.values())
        assert not _overlap(fake, "quant.exit", "quant.open")
        assert not _overlap(fake, "risk.exit", "risk.open")
        assert fake.calls == CHAIN
        assert len({fake.spans[s][2] for s in CHAIN}) == 1  # one thread

    def test_in_memory_store_falls_back_to_serial(self) -> None:
        conn = connect(":memory:")
        migrate(conn)
        fake = Fake()
        outs = _slot(_disp(conn, load_routines(), fake))
        assert all(o.status == "ok" for o in outs.values())
        assert fake.calls == CHAIN

    def test_other_chains_never_fork(self, tmp_path: Path) -> None:
        r = load_routines()
        assert branch_indices(r, ["positions.evaluate", "exits.mandatory", "broker.execute"]) == []


# ---------------------------------------------------------------------------
# experiments: an arm's paired copy forks inside or before the branches
# ---------------------------------------------------------------------------


class TestPairedFork:
    @pytest.mark.parametrize(
        ("fork", "ran"),
        [
            ("quant.exit", [*EXIT, *OPEN, "quant.propose", "broker.execute"]),
            ("quant.open", [*OPEN, "quant.propose", "broker.execute"]),
            ("risk.open", ["risk.open", "quant.revise", "quant.propose", "broker.execute"]),
        ],
    )
    def test_reused_upstream_then_both_branches(
        self, tmp_path: Path, fork: str, ran: list[str]
    ) -> None:
        """Steps before the fork are reused (``ok``, never re-run); from the fork on the
        arm runs its own copy, the branches in parallel."""
        conn = _db(tmp_path)
        runs = RoutineRunRepo(conn)
        chain = "chain-arm.treatment"
        upstream = CHAIN[: CHAIN.index(fork)]
        reused = {}
        for i, job in enumerate(upstream):
            run = runs.claim(
                job=job,
                scheduled_for=SLOT,
                reason="schedule" if i == 0 else "chain:research",
                chain_run_id=chain,
                step_index=i,
                now=SLOT,
            )
            assert run is not None
            runs.finish(run.run_id, status=RunStatus.OK, outputs=[], summary="reused", now=SLOT)
            reused[job] = runs.get(run.run_id)
        fake = Fake()
        fake.delays = {s: 0.15 for s in [*EXIT, *OPEN]}
        outs = {
            o.job: o
            for o in _disp(conn, load_routines(), fake).run_paired(
                CHAIN, SLOT, now=SLOT, chain_run_id=chain, reused=reused
            )
        }
        assert sorted(fake.calls) == sorted(ran)
        assert all(outs[s].status == "ok" for s in CHAIN)
        for job in upstream:
            assert outs[job].reason == "already done (resume)"
        if fork == "quant.exit":
            assert _overlap(fake, "quant.exit", "quant.open")
        if fork == "quant.open":
            # the exit branch was reused whole; the open branch ran alone
            assert outs["quant.exit"].reason == outs["risk.exit"].reason == "already done (resume)"
        if fork == "risk.open":
            assert outs["quant.open"].reason == "already done (resume)"
            assert "risk.exit" not in fake.calls
