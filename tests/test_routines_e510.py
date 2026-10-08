"""E5.10 / D39: the tick never waits on slow jobs.

- ``lane: background`` jobs are planned + claimed by the tick, then run by a detached
  ``arc routines run-claimed <run_id>`` child (same ``_execute`` path).
- The global LLM lock is only taken for personas routed to a local model.
- The tick heartbeat records its own wall time and its slowest jobs.
- EDGAR makes one submissions request per company and one tickers download per run.
- SQLite connections wait on each other's writes (busy_timeout).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError

from arc.llm_routing import LLMRouting, Persona, TierSpec, load_routing
from arc.routines.config import DEFAULT_ROUTINES_PATH, Lane, RoutinesConfig, load_routines
from arc.routines.dispatcher import Dispatcher, TickReport
from arc.routines.handlers import JobContext, JobResult
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.locks import LLM_LOCK, LockManager
from arc.routines.runs import RoutineRunRepo, RunStatus
from arc.store.db import BUSY_TIMEOUT_MS, connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

REPO = Path(__file__).resolve().parent.parent
NOON = dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)  # a Monday, in session

REMOTE = LLMRouting(
    tiers={"api": TierSpec(model="anthropic/claude-opus-5")},
    personas={p: "api" for p in Persona},
)
LOCAL = LLMRouting(
    tiers={"local": TierSpec(model="ollama/qwen3", local=True)},
    personas={p: "local" for p in Persona},
)

YAML = """
    sources:
      rss: {every: 30m, window: "06:00-20:00", days: trading}
      edgar: {every: 30m, window: "06:00-20:00", days: trading, lane: background}
    personas:
      scalp: {every: 30m, window: "09:00-16:00", days: trading, after_sources: true,
              ttl: 20m, lane: background}
      research: {every: 30m, window: "09:00-16:00", days: trading, ttl: 5m}
"""


def cfg(text: str = YAML, **tick: Any) -> RoutinesConfig:
    data = yaml.safe_load(textwrap.dedent(text))
    if tick:
        data["tick"] = tick
    return RoutinesConfig.model_validate(data)


class Spawner:
    """Records spawned argv/env instead of starting a process."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail = fail

    def __call__(self, argv: Sequence[str], env: Mapping[str, str] | None) -> int:
        if self.fail:
            msg = "fork failed"
            raise OSError(msg)
        self.calls.append((list(argv), dict(env or {})))
        return 4242 + len(self.calls)

    def run_ids(self) -> list[str]:
        return [argv[argv.index("run-claimed") + 1] for argv, _ in self.calls]


class Handlers:
    def __init__(self, *, sleep: float = 0.0, slow: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.sleep = sleep
        self.slow = slow

    def __call__(self, name: str) -> Callable[[JobContext], JobResult]:
        def handler(ctx: JobContext) -> JobResult:
            self.calls.append(name)
            if name in self.slow:
                time.sleep(self.sleep)
            return JobResult(summary=f"{name} done")

        return handler

    def all(self) -> dict[str, Callable[[JobContext], JobResult]]:
        return {n: self(n) for n in ("rss", "edgar", "scalp", "research")}


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def make(
    conn: sqlite3.Connection,
    routines: RoutinesConfig | None = None,
    *,
    handlers: Handlers | None = None,
    spawner: Spawner | None = None,
    locks: LockManager | None = None,
    routing: LLMRouting = REMOTE,
    halted: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> Dispatcher:
    return Dispatcher(
        conn,
        routines or cfg(),
        handlers=(handlers or Handlers()).all(),
        locks=locks,
        notifier=RecordingNotifier(),
        is_halted=halted,
        spawner=spawner,
        routing=routing,
        sleep=sleep,
    )


def status(conn: sqlite3.Connection, run_id: str) -> str:
    run = RoutineRunRepo(conn).get(run_id)
    assert run is not None
    return run.status.value


def manifest(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM run_manifests WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["payload"])


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_lane_defaults_inline_and_parses(self) -> None:
        c = cfg()
        assert c.sources["rss"].lane is Lane.INLINE
        assert c.sources["edgar"].lane is Lane.BACKGROUND
        assert c.tick.after_sources_wait == dt.timedelta(minutes=5)
        assert cfg(after_sources_wait="90s").tick.after_sources_wait == dt.timedelta(seconds=90)

    def test_unknown_lane_rejected(self) -> None:
        with pytest.raises(ValidationError):
            cfg(YAML.replace("lane: background}", "lane: sideways}"))

    def test_negative_after_sources_wait_rejected(self) -> None:
        with pytest.raises(ValidationError, match="after_sources_wait"):
            cfg(after_sources_wait="-1m")
        with pytest.raises(ValidationError, match="positive"):
            cfg(after_sources_wait="0s")

    def test_background_chain_rejected(self) -> None:
        text = YAML + "      loop: {every: 30m, days: trading, chain: [quant], lane: background}\n"
        with pytest.raises(ValidationError, match="lane background"):
            cfg(text)

    def test_shipped_lanes(self) -> None:
        """D39: the jobs with p90 > 30 s run in the background; the loop never does."""
        c = load_routines(DEFAULT_ROUTINES_PATH)
        background = sorted(n for n, (_, s) in c.jobs().items() if s.lane is Lane.BACKGROUND)
        assert background == [
            "edgar",
            "finnhub.earnings_history",
            "finnhub.fundamentals",
            "finnhub.insider",
            "finnhub.recs",
            "iv.record",
            "options_fast",  # E13.6: ~50 chain requests, ~45 s
            "scalp",
            "scalp.overnight",
            "scout",  # E13.7: one daily cheap LLM call
            "ticker_news",  # E14.1: up to 20 pages per symbol chunk + Finnhub fallback
            "youtube.briefs",
        ]  # fmt: skip  (E4.8: the Finnhub jobs wait on the shared 55/min budget)
        for job in ("research", "monitor", "positions.evaluate"):
            assert c.jobs()[job][1].lane is Lane.INLINE

    def test_shipped_routing_is_remote(self) -> None:
        """Every persona routes to the API today, so nothing takes the LLM lock."""
        routing = load_routing()
        assert not any(routing.is_local(p) for p in Persona)


# ---------------------------------------------------------------------------
# background lane: the tick side
# ---------------------------------------------------------------------------


class TestTickSpawns:
    def test_background_jobs_are_claimed_and_spawned_not_run(
        self, conn: sqlite3.Connection
    ) -> None:
        h, sp = Handlers(), Spawner()
        report = make(conn, handlers=h, spawner=sp).tick(NOON)
        assert h.calls == ["rss", "research"]  # inline work only
        by_job = {o.job: o for o in report.outcomes}
        assert by_job["edgar"].status == "spawned" and by_job["scalp"].status == "spawned"
        assert sp.run_ids() == [by_job["edgar"].run_id, by_job["scalp"].run_id]
        for run_id in sp.run_ids():
            assert status(conn, run_id) == "running"  # claimed: a doubled tick is a no-op
        argv, _ = sp.calls[0]
        assert argv[argv.index("routines") : argv.index("routines") + 2] == [
            "routines",
            "run-claimed",
        ]

    def test_doubled_tick_does_not_respawn(self, conn: sqlite3.Connection) -> None:
        sp = Spawner()
        d = make(conn, spawner=sp)
        d.tick(NOON)
        d.tick(NOON, since=NOON - dt.timedelta(minutes=5))
        assert len(sp.calls) == 2

    def test_child_inherits_the_tick_correlation(self, conn: sqlite3.Connection) -> None:
        import structlog

        sp = Spawner()
        with structlog.contextvars.bound_contextvars(tick_id="tick-abc123"):
            make(conn, spawner=sp).tick(NOON)
        assert all(env["ARC_TICK_ID"] == "tick-abc123" for _, env in sp.calls)

    def test_without_spawner_everything_runs_inline(self, conn: sqlite3.Connection) -> None:
        """Dry runs, tests and `arc routines run` keep the old behaviour."""
        h = Handlers()
        report = make(conn, handlers=h).tick(NOON)
        assert sorted(h.calls) == ["edgar", "research", "rss", "scalp"]
        assert {o.status for o in report.outcomes} == {"ok"}

    def test_busy_job_lock_defers_without_claiming(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A previous run still holding the job's flock: deferred, nothing recorded."""
        sp, locks = Spawner(), LockManager(tmp_path)
        d = make(conn, spawner=sp, locks=locks)
        with LockManager(tmp_path).hold("scalp"):
            report = d.tick(NOON)
        scalp = next(o for o in report.outcomes if o.job == "scalp")
        assert scalp.status == "deferred" and scalp.run_id is None
        assert [r for r in sp.run_ids() if status(conn, r)] and len(sp.calls) == 1  # edgar
        assert (
            conn.execute("SELECT COUNT(*) FROM routine_runs WHERE job='scalp'").fetchone()[0] == 0
        )

    def test_spawn_failure_fails_the_run_and_alerts(self, conn: sqlite3.Connection) -> None:
        notes = RecordingNotifier()
        d = make(conn, spawner=Spawner(fail=True))
        d.heartbeats._notifier = notes  # type: ignore[attr-defined]
        report = d.tick(NOON)
        scalp = next(o for o in report.outcomes if o.job == "scalp")
        assert scalp.status == "failed" and "spawn failed" in scalp.reason
        assert scalp.run_id is not None and status(conn, scalp.run_id) == "failed"
        assert any("spawn failed" in text for _, text in notes.posts)

    def test_tick_does_not_wait_on_slow_background_jobs(self, conn: sqlite3.Connection) -> None:
        """Scalp + EDGAR sleep 1.5 s each; the tick's inline work is far below that."""
        h = Handlers(sleep=1.5, slow=frozenset({"scalp", "edgar"}))
        t0 = time.monotonic()
        report = make(conn, handlers=h, spawner=Spawner()).tick(NOON)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"tick took {elapsed:.2f}s"
        assert next(o for o in report.outcomes if o.job == "research").status == "ok"
        # the same tick inline (no spawner) waits for both: the bound is meaningful
        conn2 = connect(":memory:")
        migrate(conn2)
        t1 = time.monotonic()
        make(conn2, handlers=h).tick(NOON)
        assert time.monotonic() - t1 >= 3.0

    def test_report_ranks_slowest_jobs(self) -> None:
        r = TickReport(now=NOON, since=None, dry_run=False, halted=False)
        r.durations_ms = {"a": 5, "b": 900, "c": 40, "d": 900}
        assert r.slowest(3) == [
            {"job": "b", "ms": 900},
            {"job": "d", "ms": 900},
            {"job": "c", "ms": 40},
        ]


# ---------------------------------------------------------------------------
# background lane: the child side (`arc routines run-claimed`)
# ---------------------------------------------------------------------------


class TestRunClaimed:
    def _claimed(self, conn: sqlite3.Connection, job: str, **kw: Any) -> tuple[Dispatcher, str]:
        sp = Spawner()
        make(conn, spawner=sp).tick(NOON)
        repo = RoutineRunRepo(conn)
        run_id = ""
        for rid in sp.run_ids():
            run = repo.get(rid)
            assert run is not None
            if run.job == job:
                run_id = rid
            else:  # siblings done, so after_sources never waits here
                repo.finish(rid, status=RunStatus.OK, now=NOON)
        return make(conn, **kw), run_id

    def test_child_runs_the_same_execute_path(self, conn: sqlite3.Connection) -> None:
        h = Handlers()
        d, run_id = self._claimed(conn, "edgar", handlers=h)
        (out,) = d.run_claimed(run_id)
        assert out.status == "ok" and h.calls == ["edgar"]
        assert status(conn, run_id) == "ok"
        m = manifest(conn, run_id)
        assert m["metrics"]["lane"] == "background"  # the child marker (E8.2 trace)
        assert m["tick_now"].startswith("2026-09-28T12:00")

    def test_second_child_for_the_same_run_is_a_noop(self, conn: sqlite3.Connection) -> None:
        h = Handlers()
        d, run_id = self._claimed(conn, "edgar", handlers=h)
        d.run_claimed(run_id)
        (again,) = d.run_claimed(run_id)
        assert again.status == "duplicate" and h.calls == ["edgar"]

    def test_unknown_run(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(KeyError):
            make(conn).run_claimed("run-nope")

    def test_lock_busy_in_child_is_skipped_not_queued(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        h = Handlers()
        d, run_id = self._claimed(conn, "scalp", handlers=h, locks=LockManager(tmp_path))
        with LockManager(tmp_path).hold("scalp"):
            (out,) = d.run_claimed(run_id)
        assert out.status == "skipped" and "previous run still going" in out.reason
        assert h.calls == [] and status(conn, run_id) == "skipped"
        # never queued: the next tick does not run this slot again
        sp = Spawner()
        make(conn, spawner=sp).tick(NOON + dt.timedelta(minutes=5))
        assert sp.calls == []

    def test_halt_is_checked_in_the_child(self, conn: sqlite3.Connection) -> None:
        h = Handlers()
        d, run_id = self._claimed(conn, "scalp", handlers=h, halted=lambda: True)
        (out,) = d.run_claimed(run_id)
        assert out.status == "skipped" and out.reason == "halted (persona)"
        assert h.calls == [] and status(conn, run_id) == "skipped"

    def test_sources_keep_fetching_while_halted(self, conn: sqlite3.Connection) -> None:
        h = Handlers()
        d, run_id = self._claimed(conn, "edgar", handlers=h, halted=lambda: True)
        assert d.run_claimed(run_id)[0].status == "ok"


class TestAfterSources:
    """D39: a background Scalp waits (bounded) for same-tick background sources."""

    def test_scalp_waits_for_running_same_tick_source(self, conn: sqlite3.Connection) -> None:
        sp = Spawner()
        make(conn, spawner=sp).tick(NOON)
        repo = RoutineRunRepo(conn)
        edgar_id, scalp_id = sp.run_ids()
        order: list[str] = []

        def sleep(_s: float) -> None:  # the source finishes while the Scalp waits
            order.append("wait")
            repo.finish(edgar_id, status=RunStatus.OK, summary="done", now=NOON)

        h = Handlers()
        d = make(conn, handlers=h, sleep=sleep)
        (out, *_) = d.run_claimed(scalp_id)
        assert out.status == "ok"
        assert order == ["wait"] and h.calls == ["scalp"]

    def test_no_wait_when_sources_already_done(self, conn: sqlite3.Connection) -> None:
        sp = Spawner()
        make(conn, spawner=sp).tick(NOON)
        edgar_id, scalp_id = sp.run_ids()
        RoutineRunRepo(conn).finish(edgar_id, status=RunStatus.OK, now=NOON)
        waits: list[float] = []
        make(conn, sleep=waits.append).run_claimed(scalp_id)
        assert waits == []

    def test_wait_is_bounded(self, conn: sqlite3.Connection) -> None:
        """A stuck source never holds the Scalp past tick.after_sources_wait."""
        routines = cfg(after_sources_wait="1s")
        sp = Spawner()
        make(conn, routines, spawner=sp).tick(NOON)
        _, scalp_id = sp.run_ids()
        h = Handlers()
        (out, *_) = make(conn, routines, handlers=h).run_claimed(scalp_id)
        assert out.status == "ok" and h.calls == ["scalp"]

    def test_other_ticks_sources_are_ignored(self, conn: sqlite3.Connection) -> None:
        sp = Spawner()
        make(conn, spawner=sp).tick(NOON)
        edgar_id, scalp_id = sp.run_ids()
        RoutineRunRepo(conn).finish(edgar_id, status=RunStatus.OK, now=NOON)
        # an older, still-running edgar run from an earlier tick
        RoutineRunRepo(conn).claim(
            job="edgar", scheduled_for=NOON - dt.timedelta(hours=1), reason="schedule",
            now=NOON - dt.timedelta(hours=1),
        )  # fmt: skip
        waits: list[float] = []
        make(conn, sleep=waits.append).run_claimed(scalp_id)
        assert waits == []


# ---------------------------------------------------------------------------
# LLM lock: local models only
# ---------------------------------------------------------------------------


class TestLLMLock:
    def test_remote_routes_take_no_llm_lock(self, conn: sqlite3.Connection) -> None:
        d = make(conn, routing=REMOTE)
        assert d._lock_names(["scalp"]) == ["scalp"]
        assert LLM_LOCK not in d._lock_names(["research"])

    def test_local_routes_take_the_llm_lock(self, conn: sqlite3.Connection) -> None:
        d = make(conn, routing=LOCAL)
        assert d._lock_names(["scalp"]) == ["scalp", LLM_LOCK]
        assert d._lock_names(["rss"]) == ["rss"]  # sources never call an LLM

    def test_mixed_routing_per_persona(self, conn: sqlite3.Connection) -> None:
        routing = LLMRouting(
            tiers={
                "api": TierSpec(model="anthropic/x"),
                "box": TierSpec(model="ollama/y", local=True),
            },
            personas={**{p: "api" for p in Persona}, Persona.QUANT: "box"},
        )
        routines = cfg(YAML.replace("ttl: 5m}", "ttl: 5m, chain: [quant]}"))
        d = make(conn, routines, routing=routing)
        assert d._lock_names(["scalp"]) == ["scalp"]
        assert d._lock_names(["research", "quant"]) == ["research", LLM_LOCK]

    def test_unreadable_routing_fails_safe(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        from arc.config import ArcSettings

        bad = tmp_path / "routing.yaml"
        bad.write_text("tiers: {}\n")
        d = Dispatcher(
            conn,
            cfg(),
            handlers=Handlers().all(),
            settings_factory=lambda: ArcSettings(llm_routing_file=bad, _env_file=None),  # type: ignore[call-arg]
        )
        assert d._lock_names(["scalp"]) == ["scalp", LLM_LOCK]

    def test_scalp_holding_llm_lock_no_longer_blocks_the_loop(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        h = Handlers()
        d = make(conn, handlers=h, locks=LockManager(tmp_path), routing=REMOTE)
        with LockManager(tmp_path).hold(LLM_LOCK):
            report = d.tick(NOON)
        assert {o.job: o.status for o in report.outcomes}["research"] == "ok"
        assert "scalp" in h.calls

    def test_local_route_still_serialises(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        h = Handlers()
        d = make(conn, handlers=h, locks=LockManager(tmp_path), routing=LOCAL)
        with LockManager(tmp_path).hold(LLM_LOCK):
            report = d.tick(NOON)
        assert {o.job: o.status for o in report.outcomes}["scalp"] == "deferred"


# ---------------------------------------------------------------------------
# tick heartbeat: duration + slowest jobs
# ---------------------------------------------------------------------------


def test_tick_heartbeat_records_duration_and_slowest(conn: sqlite3.Connection) -> None:
    from arc.monitoring.store import HeartbeatRepo
    from arc.routines.cli import _record_tick

    report = make(conn).tick(NOON)
    _record_tick(conn, report, {"tick_id": "tick-x"}, duration_ms=1234)
    hb = HeartbeatRepo(conn).latest("tick")
    assert hb is not None
    assert hb.detail["tick_duration_ms"] == 1234
    slow = hb.detail["slowest_jobs"]
    assert len(slow) == 3 and {s["job"] for s in slow} <= {"rss", "edgar", "scalp", "research"}
    assert all(isinstance(s["ms"], int) for s in slow)


# ---------------------------------------------------------------------------
# end to end: a real detached child
# ---------------------------------------------------------------------------


def test_live_tick_spawns_a_real_child(tmp_path: Path) -> None:
    """`arc routines tick` spawns `run-claimed`; the child finishes the run under the tick id."""
    routines = tmp_path / "routines.yaml"
    routines.write_text(
        textwrap.dedent(
            """
            sources:
              slowsrc: {every: 30m, window: "06:00-20:00", days: trading, lane: background,
                        handler: "arc.routines.handlers:not_implemented"}
            """
        )
    )
    db, locks = tmp_path / "arc.db", tmp_path / "locks"
    argv = [sys.executable, "-c", "from arc.cli import main; raise SystemExit(main())"]
    argv += ["routines", "tick", "--json", "--no-slack", "--db", str(db)]
    argv += ["--config", str(routines), "--lock-dir", str(locks), "--now", NOON.isoformat()]
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=REPO, timeout=120, check=False)  # noqa: S603
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    (o,) = out["outcomes"]
    assert o["status"] == "spawned"
    tick_id = out["correlation"]["tick_id"]
    conn = connect(db)
    run_id = o["run_id"]
    has_manifest = "SELECT 1 FROM run_manifests WHERE run_id = ?"
    deadline = time.monotonic() + 60
    # the child finishes the run, then writes its manifest: wait for both
    while time.monotonic() < deadline and (
        status(conn, run_id) == "running"
        or conn.execute(has_manifest, (run_id,)).fetchone() is None
    ):
        time.sleep(0.2)
    assert status(conn, run_id) == "skipped"  # not_implemented -> skipped, by the child
    m = manifest(conn, run_id)
    assert m["metrics"]["lane"] == "background"
    assert m["correlation"]["tick_id"] == tick_id


# ---------------------------------------------------------------------------
# SQLite: tick and child write concurrently
# ---------------------------------------------------------------------------


class TestSqliteConcurrency:
    def test_busy_timeout_is_set(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "a.db")
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_two_writers_both_commit(self, tmp_path: Path) -> None:
        """The tick holds the write lock; the child's write waits for it, then commits."""
        path = tmp_path / "a.db"
        migrate(connect(path))
        locked, failures = threading.Event(), []

        def tick_writer() -> None:  # its own connection, in its own thread
            try:
                tick = connect(path)
                tick.execute("BEGIN IMMEDIATE")
                tick.execute(
                    "INSERT INTO routine_state (key, value, updated_at) VALUES ('tick', '1', 'x')"
                )
                locked.set()
                time.sleep(0.3)
                tick.commit()
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
                failures.append(exc)
                locked.set()

        t = threading.Thread(target=tick_writer)
        t.start()
        assert locked.wait(10)
        child = connect(path)
        with child:  # blocks on the tick's write lock, then commits (no "database is locked")
            child.execute(
                "INSERT INTO routine_state (key, value, updated_at) VALUES ('child', '1', 'x')"
            )
        t.join()
        assert failures == []
        keys = {r[0] for r in child.execute("SELECT key FROM routine_state")}
        assert {"tick", "child"} <= keys


# ---------------------------------------------------------------------------
# EDGAR: request count
# ---------------------------------------------------------------------------


def test_edgar_one_submissions_request_per_company(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.ingest.edgar as E
    from arc.config import ArcSettings

    calls: dict[str, int] = {"tickers": 0, "submissions": 0, "filing": 0}

    def tickers(_s: Any) -> dict[str, str]:
        calls["tickers"] += 1
        return {"AAPL": "0000320193", "MSFT": "0000789019"}

    def subs(cik: str, _s: Any) -> dict[str, Any]:
        calls["submissions"] += 1
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-Q", "4"],
                    "accessionNumber": [f"{cik}-1", f"{cik}-2", f"{cik}-3"],
                    "filingDate": ["2026-09-01"] * 3,
                    "primaryDocument": ["a.htm", "b.htm", "c.xml"],
                }
            }
        }

    def filing(_url: str, _s: Any) -> str:
        calls["filing"] += 1
        return "text"

    monkeypatch.setattr(E, "_company_tickers", tickers)
    monkeypatch.setattr(E, "_fetch_submissions", subs)
    monkeypatch.setattr(E, "_fetch_filing_text", filing)
    monkeypatch.setattr(
        E.IngestUniverse, "from_settings", classmethod(lambda cls, s, **_kw: _NoMaster())
    )
    settings = ArcSettings(universe=["AAPL", "MSFT", "QQQ"], _env_file=None)  # type: ignore[call-arg]
    docs = E.fetch_edgar(conn, settings)
    # E14.2 (D60): Form 4 is no longer fetched -> 8-K + 10-Q per company
    assert calls == {"tickers": 1, "submissions": 2, "filing": 4}
    assert len(docs) == 4
    # second run: cursors hold, no filing downloads, still one request per company
    calls.update(tickers=0, submissions=0, filing=0)
    assert E.fetch_edgar(conn, settings) == []
    assert calls == {"tickers": 1, "submissions": 2, "filing": 0}


class _NoMaster:
    seed = ("AAPL", "MSFT")

    def cik(self, _t: str) -> None:
        return None

    def tickers_in(self, _text: str) -> list[str]:
        return []


def test_filings_of_form_filters_and_caps() -> None:
    from arc.ingest.edgar import _filings_of_form

    data = {
        "filings": {
            "recent": {
                "form": ["4", "8-K", "4", "4"],
                "accessionNumber": ["a", "b", "c", "d"],
                "filingDate": ["x", "y", "z", "w"],
                "primaryDocument": ["1", "2", "3", "4"],
            }
        }
    }
    assert [f["accessionNumber"] for f in _filings_of_form(data, "1", "4", count=2)] == ["a", "c"]
    assert _filings_of_form(None, "1", "4", count=2) == []
