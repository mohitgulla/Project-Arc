"""E5.8 (D31 / D36): the 5-min trading loop.

Deterministic tests on the bundled fixtures:

* a loop slot whose inputs match the last full run reports ``no_change``: no
  Director/Quant/Risk LLM call, the chain's ``execute`` step still runs;
* after ``loop.max_idle`` the same inputs get a full run again;
* a new candidate, a filled position or a P&L bucket change breaks the digest;
* a loop slot that finds the previous loop (or a Scout) holding a lock is
  recorded as ``skipped`` and never deferred / caught up;
* the chain deadline: no step starts after ``loop.max_runtime``, the run is
  ``timeout``, and the notice goes out once per day;
* the manual ``arc propose`` path (reason ``manual``) never skips.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

import pytest

from arc.context.store import ContextStore
from arc.ingest.scout import load_fixture_docs
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import open_db, pipeline_handlers
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import LLM_LOCK, Dispatcher
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.locks import LockManager
from arc.routines.loop import LoopInputs, LoopState, pnl_bucket, slot_stamp
from arc.routines.runs import RoutineRunRepo
from arc.utils.calendar import ET
from tests.test_e59_director_portfolio import _settings

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

SLOT0 = FIXTURE_NOW.astimezone(ET)


def _loop_overrides(**loop: Any) -> dict[tuple[str, ...], Any]:
    return {("loop", k): v for k, v in loop.items()}


@pytest.fixture
def routines() -> RoutinesConfig:
    return load_routines()


def _disp(
    conn: sqlite3.Connection,
    routines: RoutinesConfig,
    *,
    notifier: RecordingNotifier | None = None,
    locks: LockManager | None = None,
) -> Dispatcher:
    settings = _settings()
    return Dispatcher(
        conn,
        routines,
        handlers=pipeline_handlers(PipelineEnv.fixtures()),
        locks=locks,
        notifier=notifier or RecordingNotifier(),
        settings_factory=lambda: settings,
    )


def _conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    return conn


def _slot(disp: Dispatcher, at: dt.datetime, *, reason: str = "schedule") -> dict[str, Any]:
    # A fresh fixture env per slot: the canned persona replies are consumed per env.
    disp.handlers = pipeline_handlers(PipelineEnv.fixtures())
    outs = disp.run_job("director", at, reason=reason, now=at, chain=True)
    return {o.job: o for o in outs}


def _warm(disp: Dispatcher) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two full loops: the second differs from the first only by the SPY proposal
    now in the dedupe window, so from the third slot on the inputs are stable."""
    first = _slot(disp, SLOT0)
    second = _slot(disp, SLOT0 + dt.timedelta(minutes=5))
    assert first["director"].metrics["no_change"] is False
    assert second["director"].metrics["no_change"] is False
    return first, second


def _llm_calls(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM persona_calls").fetchone()[0]


def _seed_scout(conn: sqlite3.Connection, routines: RoutinesConfig) -> None:
    disp = _disp(conn, routines)
    (out,) = disp.run_job("scout", SLOT0, reason="manual", now=SLOT0)
    assert out.status == "ok", out.reason


# ---------------------------------------------------------------------------
# change-aware skip
# ---------------------------------------------------------------------------


class TestNoChange:
    def test_same_inputs_skip_llm_but_run_execute(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        disp = _disp(conn, routines)
        first, second = _warm(disp)
        assert {s for s, o in first.items() if o.status == "ok"} >= {"quant", "risk", "propose"}
        calls = _llm_calls(conn)
        assert calls >= 6  # 2 x (director + quant + risk)
        digest = second["director"].metrics["loop_digest"]

        third = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        d = third["director"]
        assert d.status == "ok" and d.metrics["no_change"] is True, d.summary
        assert d.metrics["loop_digest"] == digest
        assert d.summary.startswith("no_change")
        assert third["quant"].status == "skipped" and third["risk"].status == "skipped"
        assert third["propose"].status == "skipped"
        assert third["execute"].status == "ok", third["execute"].reason
        assert _llm_calls(conn) == calls  # not one more LLM call
        # the journal explains the skip
        row = conn.execute(
            "SELECT reason_code, reason_text FROM decisions WHERE reason_code='loop_no_change'"
        ).fetchone()
        assert row is not None and "unchanged" in row["reason_text"]
        # the skipped steps are on the record with the reason
        runs = RoutineRunRepo(conn)
        skipped = [r for r in runs.history(limit=30) if r.status.value == "skipped"]
        assert {r.job for r in skipped} == {"quant", "risk", "propose"}
        assert all("no_change" in (r.summary or "") for r in skipped)
        # the run manifest carries the digest that was compared
        (payload,) = conn.execute(
            "SELECT payload FROM run_manifests WHERE run_id = ?", (d.run_id,)
        ).fetchone()
        assert digest in payload

    def test_full_run_again_after_max_idle(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        disp = _disp(conn, routines)
        _warm(disp)
        before = _llm_calls(conn)
        late = SLOT0 + dt.timedelta(minutes=5) + routines.loop.max_idle
        out = _slot(disp, late)
        assert out["director"].metrics["no_change"] is False
        assert _llm_calls(conn) > before
        assert LoopState(conn).last_full_run() == late

    def test_new_candidate_changes_digest(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        disp = _disp(conn, routines)
        _, second = _warm(disp)
        # a new Scout candidate lands between slots
        store = ContextStore(conn)
        cand = store.snapshot(SLOT0, kinds=["candidate"]).latest("candidate", "SPY")
        assert cand is not None
        payload = dict(cand.payload)
        payload["ticker"] = "QQQ"
        store.write(
            kind="candidate",
            subject="QQQ",
            payload=payload,
            produced_by="scout",
            run_id="run-test",
            now=SLOT0 + dt.timedelta(minutes=6),
        )
        third = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert third["director"].metrics["no_change"] is False
        assert third["director"].metrics["loop_digest"] != second["director"].metrics["loop_digest"]

    def test_manual_propose_never_skips(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        disp = _disp(conn, routines)
        _warm(disp)
        before = _llm_calls(conn)
        out = _slot(disp, SLOT0 + dt.timedelta(minutes=10), reason="manual")
        assert out["director"].metrics["no_change"] is False
        assert _llm_calls(conn) > before

    def test_director_outside_the_loop_never_skips(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        other = load_routines(overrides={("loop", "job"): "monitor"})
        disp = _disp(conn, other)
        _warm(disp)
        before = _llm_calls(conn)
        out = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert out["director"].metrics["no_change"] is False
        assert _llm_calls(conn) > before


class TestDigest:
    def test_digest_is_order_independent_and_rounded(self) -> None:
        a = LoopInputs(
            candidates=["c1@1", "c2@1"],
            regimes=["SPY@r1"],
            positions=["s1:2"],
            pnl_bucket=3,
            pending_orders=0,
            budget_tier="normal",
            suppressed=[],
        )
        b = a.model_copy(update={"candidates": ["c1@1", "c2@1"]})
        assert a.digest() == b.digest()
        assert a.model_copy(update={"pnl_bucket": 4}).digest() != a.digest()
        assert a.model_copy(update={"pending_orders": 1}).digest() != a.digest()
        assert a.model_copy(update={"positions": []}).digest() != a.digest()

    @pytest.mark.parametrize(
        ("pnl", "equity", "pct", "bucket"),
        [
            (0.0, 100_000, 0.5, 0),
            (499.0, 100_000, 0.5, 0),
            (500.0, 100_000, 0.5, 1),
            (-1.0, 100_000, 0.5, -1),
            (None, 100_000, 0.5, 0),
            (250.0, 0, 0.5, 0),
        ],
    )
    def test_pnl_bucket(self, pnl: float | None, equity: float, pct: float, bucket: int) -> None:
        assert pnl_bucket(pnl, equity, pct) == bucket

    def test_slot_stamp(self) -> None:
        assert slot_stamp(dt.datetime(2026, 9, 28, 13, 40, tzinfo=dt.UTC)) == "2026-09-28 09:40ET"


# ---------------------------------------------------------------------------
# non-overlap and deadline
# ---------------------------------------------------------------------------


class TestOverlapAndDeadline:
    def test_lock_busy_slot_is_skipped_not_deferred(
        self, routines: RoutinesConfig, tmp_path: Path
    ) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        locks = LockManager(tmp_path)
        disp = _disp(conn, routines, locks=locks)
        with locks.hold("director"):
            (out,) = disp.run_job("director", SLOT0, reason="schedule", now=SLOT0, chain=True)
        assert out.status == "skipped" and "previous loop running" in out.reason
        assert out.run_id is not None
        run = RoutineRunRepo(conn).get(out.run_id)
        assert run is not None and run.status.value == "skipped"
        # the Scout holding the LLM lock is the other case
        with locks.hold(LLM_LOCK):
            (out2,) = disp.run_job(
                "director", SLOT0 + dt.timedelta(minutes=5), reason="schedule",
                now=SLOT0 + dt.timedelta(minutes=5), chain=True,
            )  # fmt: skip
        assert out2.status == "skipped" and "lock busy" in out2.reason
        # a manual run still defers (the caller retries), as before
        with locks.hold("director"):
            (out3,) = disp.run_job(
                "director", SLOT0 + dt.timedelta(minutes=10), reason="manual",
                now=SLOT0 + dt.timedelta(minutes=10), chain=True,
            )  # fmt: skip
        assert out3.status == "deferred"

    def test_skipped_slot_advances_the_cursor(
        self, routines: RoutinesConfig, tmp_path: Path
    ) -> None:
        """A skipped loop slot is never caught up on the next tick."""
        conn = _conn()
        _seed_scout(conn, routines)
        locks = LockManager(tmp_path)
        notes = RecordingNotifier()
        disp = _disp(conn, routines, locks=locks, notifier=notes)
        slot = SLOT0.replace(hour=9, minute=40)
        with locks.hold("director"):
            disp.tick(slot + dt.timedelta(seconds=30), since=slot - dt.timedelta(minutes=5))
        runs = [r for r in RoutineRunRepo(conn).history(limit=50) if r.job == "director"]
        assert [r.status.value for r in runs] == ["skipped"]
        # next tick: only the next slot is planned, not the skipped one
        due = [
            j for j in disp.plan(slot + dt.timedelta(minutes=5, seconds=30)) if j.job == "director"
        ]
        assert [j.slot for j in due] == [slot + dt.timedelta(minutes=5)]

    def test_chain_deadline_skips_later_steps_and_alerts_once(
        self, routines: RoutinesConfig
    ) -> None:
        conn = _conn()
        _seed_scout(conn, routines)
        fast = load_routines(overrides=_loop_overrides(max_runtime="1s"))
        notes = RecordingNotifier()
        disp = _disp(conn, fast, notifier=notes)

        def run_slow(at: dt.datetime) -> dict[str, Any]:
            handlers = pipeline_handlers(PipelineEnv.fixtures())
            real = handlers["director"]

            def slow_director(ctx: Any) -> Any:  # the Director alone eats the budget
                time.sleep(1.1)
                return real(ctx)

            handlers["director"] = slow_director
            disp.handlers = handlers
            return {
                o.job: o
                for o in disp.run_job("director", at, reason="schedule", now=at, chain=True)
            }

        out = run_slow(SLOT0)
        assert out["director"].status == "ok"
        assert out["quant"].status == "skipped" and "timeout" in out["quant"].reason
        assert out["execute"].status == "skipped"
        alerts = [t for _, t in notes.posts if "exceeded" in t]
        assert len(alerts) == 1
        # second timeout on the same day: no second notice
        run_slow(SLOT0 + dt.timedelta(minutes=5))
        assert len([t for _, t in notes.posts if "exceeded" in t]) == 1
        chain_id = out["director"].chain_run_id
        assert chain_id is not None
        summary = LoopState(conn).chain_summary(chain_id)
        assert summary is not None and summary["timeout"] is True
        assert set(summary["durations_ms"]) == {"director"}
