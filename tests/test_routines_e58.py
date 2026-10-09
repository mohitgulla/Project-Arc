"""E5.8 (D31 / D36): the 5-min trading loop.

Deterministic tests on the bundled fixtures:

* a loop slot whose inputs match the last full run reports ``no_change``: no
  Research/Quant/Risk LLM call, the chain's ``execute`` step still runs;
* after ``loop.max_idle`` the same inputs get a full run again;
* a new candidate, a filled position or a P&L bucket change breaks the digest;
* a loop slot that finds the previous loop (or a Scalp) holding a lock is
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

from arc.approvals.service import ApprovalService, LogCardPoster, PostedCard
from arc.broker.ladder_job import execute_step
from arc.context.store import ContextStore
from arc.ingest.llm import LLMResult, ScalpLLMError
from arc.ingest.scalp import load_fixture_docs
from arc.llm_routing import LLMRouting, Persona, TierSpec
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import open_db, pipeline_handlers
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.dispatcher import LLM_LOCK, Dispatcher
from arc.routines.handlers import RunEnv
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.locks import LockManager
from arc.routines.loop import (
    LoopInputs,
    LoopState,
    loop_root_from_db,
    pnl_bucket,
    refresh_loop_root,
    slot_stamp,
)
from arc.routines.runs import RoutineRunRepo
from arc.slack.loop import LoopRoot
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import _settings

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
    routing: LLMRouting | None = None,
) -> Dispatcher:
    settings = _settings()
    return Dispatcher(
        conn,
        routines,
        handlers=pipeline_handlers(PipelineEnv.fixtures()),
        locks=locks,
        notifier=notifier or RecordingNotifier(),
        settings_factory=lambda: settings,
        routing=routing,
    )


def _conn() -> sqlite3.Connection:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    return conn


def _slot(disp: Dispatcher, at: dt.datetime, *, reason: str = "schedule") -> dict[str, Any]:
    # A fresh fixture env per slot: the canned persona replies are consumed per env.
    disp.handlers = pipeline_handlers(PipelineEnv.fixtures())
    outs = disp.run_job("research", at, reason=reason, now=at, chain=True)
    return {o.job: o for o in outs}


def _warm(disp: Dispatcher) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two full loops: the second differs from the first only by the SPY proposal
    now in the dedupe window, so from the third slot on the inputs are stable."""
    first = _slot(disp, SLOT0)
    second = _slot(disp, SLOT0 + dt.timedelta(minutes=5))
    assert first["research"].metrics["no_change"] is False
    assert second["research"].metrics["no_change"] is False
    return first, second


def _llm_calls(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM persona_calls").fetchone()[0]


def _seed_scalp(conn: sqlite3.Connection, routines: RoutinesConfig) -> None:
    disp = _disp(conn, routines)
    (out,) = disp.run_job("scalp", SLOT0, reason="manual", now=SLOT0)
    assert out.status == "ok", out.reason


# ---------------------------------------------------------------------------
# change-aware skip
# ---------------------------------------------------------------------------


class TestNoChange:
    def test_same_inputs_skip_llm_but_run_execute(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        first, second = _warm(disp)
        assert {s for s, o in first.items() if o.status == "ok"} >= {
            "quant.open",
            "risk.open",
            "quant.propose",
        }
        calls = _llm_calls(conn)
        assert calls >= 6  # 2 x (research + quant + risk)
        digest = second["research"].metrics["loop_digest"]

        third = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        d = third["research"]
        assert d.status == "ok" and d.metrics["no_change"] is True, d.summary
        assert d.metrics["loop_digest"] == digest
        assert d.summary.startswith("no_change")
        assert third["quant.open"].status == "skipped" and third["risk.open"].status == "skipped"
        assert third["quant.propose"].status == "skipped"
        assert third["broker.execute"].status == "ok", third["broker.execute"].reason
        assert _llm_calls(conn) == calls  # not one more LLM call
        # the journal explains the skip
        row = conn.execute(
            "SELECT reason_code, reason_text FROM decisions WHERE reason_code='loop_no_change'"
        ).fetchone()
        assert row is not None and "unchanged" in row["reason_text"]
        # the skipped steps are on the record with the reason
        runs = RoutineRunRepo(conn)
        skipped = [r for r in runs.history(limit=30) if r.status.value == "skipped"]
        # E13.15: the exit steps skip on their own (no open positions / no cases)
        assert {r.job for r in skipped} >= {"quant.open", "risk.open", "quant.propose"}
        loop_skips = [r for r in skipped if r.job in ("quant.open", "risk.open", "quant.propose")]
        assert all("no_change" in (r.summary or "") for r in loop_skips)
        # the run manifest carries the digest that was compared
        (payload,) = conn.execute(
            "SELECT payload FROM run_manifests WHERE run_id = ?", (d.run_id,)
        ).fetchone()
        assert digest in payload

    def test_full_run_again_after_max_idle(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        _warm(disp)
        before = _llm_calls(conn)
        late = SLOT0 + dt.timedelta(minutes=5) + routines.loop.max_idle
        out = _slot(disp, late)
        assert out["research"].metrics["no_change"] is False
        assert _llm_calls(conn) > before
        assert LoopState(conn).last_full_run() == late

    def test_new_candidate_changes_digest(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        _, second = _warm(disp)
        # a new Scalp candidate lands between slots
        store = ContextStore(conn)
        cand = store.snapshot(SLOT0, kinds=["candidate"]).latest("candidate", "SPY")
        assert cand is not None
        payload = dict(cand.payload)
        payload["ticker"] = "QQQ"
        store.write(
            kind="candidate",
            subject="QQQ",
            payload=payload,
            produced_by="scalp",
            run_id="run-test",
            now=SLOT0 + dt.timedelta(minutes=6),
        )
        third = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert third["research"].metrics["no_change"] is False
        assert third["research"].metrics["loop_digest"] != second["research"].metrics["loop_digest"]

    def test_manual_propose_never_skips(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        _warm(disp)
        before = _llm_calls(conn)
        out = _slot(disp, SLOT0 + dt.timedelta(minutes=10), reason="manual")
        assert out["research"].metrics["no_change"] is False
        assert _llm_calls(conn) > before

    def test_research_outside_the_loop_never_skips(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        other = load_routines(overrides={("loop", "job"): "monitor"})
        disp = _disp(conn, other)
        _warm(disp)
        before = _llm_calls(conn)
        out = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert out["research"].metrics["no_change"] is False
        assert _llm_calls(conn) > before

    def test_failed_research_does_not_mute_the_next_slot(self, routines: RoutinesConfig) -> None:
        """Review round 1 repro: slot 1 evaluates; slot 2 brings new inputs (the SPY
        proposal now in the dedupe window) but Research's LLM raises; slot 3 has
        the same inputs as slot 2. Slot 2 must not become ``last_full_run``, so slot 3
        is a full evaluation and not ``no_change`` with zero LLM calls."""
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        first = _slot(disp, SLOT0)
        assert first["research"].metrics["no_change"] is False
        digest1 = first["research"].metrics["loop_digest"]
        assert LoopState(conn).last_full_run() == SLOT0
        calls = _llm_calls(conn)

        # slot 2: a slow, then failing fake Research LLM (transport outage)
        env = PipelineEnv.fixtures()
        env.llms["research"] = _BrokenLLM()
        disp.handlers = pipeline_handlers(env)
        at2 = SLOT0 + dt.timedelta(minutes=5)
        outs = {
            o.job: o for o in disp.run_job("research", at2, reason="schedule", now=at2, chain=True)
        }
        assert outs["research"].status == "failed" and "outage" in outs["research"].summary
        assert "quant.open" not in outs  # the chain stopped at the failure
        assert LoopState(conn).last_full_run() == SLOT0  # the failed slot did not advance it
        assert LoopState(conn).last_digest() == digest1
        # the failure is on the record as an llm_error persona call, not a completed evaluation
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM persona_calls WHERE status = 'llm_error'"
            ).fetchone()[0]
            == 1
        )
        calls_after_fail = _llm_calls(conn)

        # slot 3: same inputs as slot 2; the LLM is back. A full run, not no_change.
        third = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert third["research"].status == "ok"
        assert third["research"].metrics["no_change"] is False
        assert third["research"].metrics["loop_digest"] != digest1
        assert _llm_calls(conn) > calls_after_fail > calls
        assert {s for s, o in third.items() if o.status == "ok"} >= {
            "quant.open",
            "risk.open",
            "quant.propose",
        }
        assert LoopState(conn).last_full_run() == SLOT0 + dt.timedelta(minutes=10)
        # and from here the same inputs do skip (the skip itself still works)
        fourth = _slot(disp, SLOT0 + dt.timedelta(minutes=15))
        assert fourth["research"].metrics["no_change"] is True

    def test_failed_first_research_records_no_full_run(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = _disp(conn, routines)
        env = PipelineEnv.fixtures()
        env.llms["research"] = _BrokenLLM()
        disp.handlers = pipeline_handlers(env)
        (out,) = disp.run_job("research", SLOT0, reason="schedule", now=SLOT0, chain=True)
        assert out.status == "failed"
        assert LoopState(conn).last_full_run() is None and LoopState(conn).last_digest() is None
        nxt = _slot(disp, SLOT0 + dt.timedelta(minutes=5))
        assert nxt["research"].status == "ok" and nxt["research"].metrics["no_change"] is False


class _BrokenLLM:
    """A Research LLM that is slow and then fails (the card's "slow fake LLM")."""

    model = "fake-broken"

    def complete(self, prompt: str) -> LLMResult:
        time.sleep(0.05)
        msg = "simulated LLM outage"
        raise ScalpLLMError(msg)


class TestConfigOnlyCadence:
    """Acceptance: a config-only cadence change (loop ``every: 10m``) needs no code change."""

    def _day_plan(self, cfg: RoutinesConfig) -> list[dt.datetime]:
        from arc.store.db import connect
        from arc.store.migrate import migrate

        conn = connect(":memory:")
        migrate(conn)
        d = Dispatcher(conn, cfg, is_halted=lambda: False)
        start = dt.datetime(2026, 9, 28, 0, 0, tzinfo=ET)  # a Monday
        end = start + dt.timedelta(days=1)
        slots: list[dt.datetime] = []
        prev, cur = start, start + dt.timedelta(minutes=5)
        while cur <= end:
            slots += [j.slot for j in d.plan(cur, since=prev, halted=False) if j.job == "research"]
            prev, cur = cur, cur + dt.timedelta(minutes=5)
        return slots

    def test_every_10m_gives_38_slots_and_loop_semantics_hold(
        self, routines: RoutinesConfig
    ) -> None:
        assert len(self._day_plan(routines)) == 38  # shipped (D52): 10m, 09:40-15:50
        ten = load_routines(overrides={("personas", "research", "every"): "10m"})
        slots = self._day_plan(ten)
        assert len(slots) == 38  # 09:40, 09:50, …, 15:50
        assert slots[0].strftime("%H:%M") == "09:40" and slots[-1].strftime("%H:%M") == "15:50"
        assert ten.is_loop("research") and ten.personas["research"].ttl is not None
        # the loop semantics (no_change skip) still apply under the new cadence
        conn = _conn()
        _seed_scalp(conn, ten)
        disp = _disp(conn, ten)
        _slot(disp, SLOT0)
        _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        third = _slot(disp, SLOT0 + dt.timedelta(minutes=20))
        assert third["research"].metrics["no_change"] is True
        assert third["quant.open"].status == "skipped" and third["broker.execute"].status == "ok"


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
        _seed_scalp(conn, routines)
        locks = LockManager(tmp_path)
        # D39: the LLM lock only matters for local models; route every persona locally.
        local = LLMRouting(
            tiers={"local": TierSpec(model="ollama/qwen3", local=True)},
            personas={p: "local" for p in Persona},
        )
        disp = _disp(conn, routines, locks=locks, routing=local)
        with locks.hold("research"):
            (out,) = disp.run_job("research", SLOT0, reason="schedule", now=SLOT0, chain=True)
        assert out.status == "skipped" and "previous loop running" in out.reason
        assert out.run_id is not None
        run = RoutineRunRepo(conn).get(out.run_id)
        assert run is not None and run.status.value == "skipped"
        # the Scalp holding the LLM lock is the other case
        with locks.hold(LLM_LOCK):
            (out2,) = disp.run_job(
                "research", SLOT0 + dt.timedelta(minutes=5), reason="schedule",
                now=SLOT0 + dt.timedelta(minutes=5), chain=True,
            )  # fmt: skip
        assert out2.status == "skipped" and "lock busy" in out2.reason
        # a manual run still defers (the caller retries), as before
        with locks.hold("research"):
            (out3,) = disp.run_job(
                "research", SLOT0 + dt.timedelta(minutes=10), reason="manual",
                now=SLOT0 + dt.timedelta(minutes=10), chain=True,
            )  # fmt: skip
        assert out3.status == "deferred"

    def test_skipped_slot_advances_the_cursor(
        self, routines: RoutinesConfig, tmp_path: Path
    ) -> None:
        """A skipped loop slot is never caught up on the next tick."""
        conn = _conn()
        _seed_scalp(conn, routines)
        locks = LockManager(tmp_path)
        notes = RecordingNotifier()
        disp = _disp(conn, routines, locks=locks, notifier=notes)
        slot = SLOT0.replace(hour=9, minute=40)
        with locks.hold("research"):
            disp.tick(slot + dt.timedelta(seconds=30), since=slot - dt.timedelta(minutes=10))
        runs = [r for r in RoutineRunRepo(conn).history(limit=50) if r.job == "research"]
        assert [r.status.value for r in runs] == ["skipped"]
        # next tick: only the next slot is planned, not the skipped one
        due = [
            j for j in disp.plan(slot + dt.timedelta(minutes=10, seconds=30)) if j.job == "research"
        ]
        assert [j.slot for j in due] == [slot + dt.timedelta(minutes=10)]  # D52: 10-min loop

    def test_chain_deadline_skips_later_steps_and_alerts_once(
        self, routines: RoutinesConfig
    ) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        fast = load_routines(overrides=_loop_overrides(max_runtime="1s"))
        notes = RecordingNotifier()
        disp = _disp(conn, fast, notifier=notes)

        def run_slow(at: dt.datetime) -> dict[str, Any]:
            handlers = pipeline_handlers(PipelineEnv.fixtures())
            real = handlers["research"]

            def slow_research(ctx: Any) -> Any:  # Research alone eats the budget
                time.sleep(1.1)
                return real(ctx)

            handlers["research"] = slow_research
            disp.handlers = handlers
            return {
                o.job: o
                for o in disp.run_job("research", at, reason="schedule", now=at, chain=True)
            }

        out = run_slow(SLOT0)
        assert out["research"].status == "ok"
        assert out["quant.open"].status == "skipped" and "timeout" in out["quant.open"].reason
        assert out["broker.execute"].status == "skipped"
        alerts = [t for _, t in notes.posts if "exceeded" in t]
        assert len(alerts) == 1
        # second timeout on the same day: no second notice
        run_slow(SLOT0 + dt.timedelta(minutes=5))
        assert len([t for _, t in notes.posts if "exceeded" in t]) == 1
        chain_id = out["research"].chain_run_id
        assert chain_id is not None
        summary = LoopState(conn).chain_summary(chain_id)
        assert summary is not None and summary["timeout"] is True
        # exits.mandatory is never deadline-skipped (§5.32); the LLM steps are
        assert set(summary["durations_ms"]) <= {"research", "exits.mandatory", "quant.exit",
                                                "risk.exit"}  # fmt: skip
        assert "quant.open" not in summary["durations_ms"]


# ---------------------------------------------------------------------------
# D36: one root per loop slot in #arc-investor
# ---------------------------------------------------------------------------


OWNER = _settings().approver_slack_user_ids[0]


class ThreadAwarePoster(LogCardPoster):
    """A card poster that, like ``SlackCardPoster``, threads under the loop root."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self.thread_of: list[str | None] = []

    def post(self, day: dt.date, view: Any, *, chain_run_id: str | None = None) -> PostedCard:
        super().post(day, view, chain_run_id=chain_run_id)
        ts = LoopState(self._conn).thread_ts(chain_run_id) if chain_run_id else None
        self.thread_of.append(ts)
        return PostedCard(channel="C_INV", thread_ts=ts, message_ts=f"200.{len(self.posted)}")


def _disp_with_cards(
    conn: sqlite3.Connection, routines: RoutinesConfig
) -> tuple[Dispatcher, RecordingNotifier, ThreadAwarePoster, ApprovalService]:
    """A dispatcher whose ``execute`` step publishes cards through a recording poster."""
    settings = _settings()
    poster = ThreadAwarePoster(conn)
    service = ApprovalService(conn, settings, poster)
    notes = RecordingNotifier()

    def handlers() -> dict[str, Any]:
        h = dict(pipeline_handlers(PipelineEnv.fixtures()))
        h["broker.execute"] = lambda ctx: execute_step(ctx, spawn=lambda _argv: 0, service=service)
        return h

    disp = Dispatcher(
        conn,
        routines,
        handlers=handlers(),
        notifier=notes,
        settings_factory=lambda: settings,
        run_env=RunEnv(slack=True),
    )
    disp._fresh_handlers = handlers  # type: ignore[attr-defined]
    return disp, notes, poster, service


def _slot_cards(disp: Dispatcher, at: dt.datetime) -> dict[str, Any]:
    disp.handlers = disp._fresh_handlers()  # type: ignore[attr-defined]
    outs = disp.run_job("research", at, reason="schedule", now=at, chain=True)
    return {o.job: o for o in outs}


class TestRootPerLoop:
    def test_root_then_cards_in_its_thread_then_metadata(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp, notes, poster, _ = _disp_with_cards(conn, routines)
        out = _slot_cards(disp, SLOT0)
        chain = out["research"].chain_run_id
        assert chain is not None
        ts = LoopState(conn).thread_ts(chain)
        assert ts is not None and ts in notes.roots
        # every persona post of this loop is a reply under the root, none in the day thread
        replies = notes.in_thread(ts)
        assert replies and notes.day_thread_posts() == []
        # D36 thread order: ⚡ [Scalp] context first, then the chain's persona cards,
        # the proposal card (recorded by the poster), and [Routines] last.
        labels = [" ".join(r.lstrip("`\n").split(" ", 2)[:2]) for r in replies]
        order = [
            lbl
            for lbl in labels
            if lbl in {"⚡ [Scalp]", "🧠 [Research]", "🤺 [Quant]", "🛡️ [Risk]"}
        ]
        # E13.9: quant.propose is the Quant's step (D56), so its summary is a 🤺 [Quant] line.
        assert order == ["⚡ [Scalp]", "🧠 [Research]", "🤺 [Quant]", "🛡️ [Risk]", "🤺 [Quant]"], (
            replies
        )
        assert replies[0].startswith("⚡ [Scalp] scalp ✓ ⚡ [Scalp] Context: ")
        assert "run " + slot_stamp(SLOT0) in replies[0]  # the Scalp run Research read
        scalp_blocks = notes.blocks[notes.threads.index(ts)]
        assert scalp_blocks and scalp_blocks[0]["text"]["text"].startswith("⚡ [Scalp] Context: ")
        assert any("SPY" in (b.get("text") or {}).get("text", "") for b in scalp_blocks)
        assert replies[-1].startswith("```\n[Routines] " + chain)
        assert "research=" in replies[-1] and "digest=" in replies[-1]
        # the proposal card went into the same thread
        assert poster.posted and poster.thread_of == [ts]
        # the root line: the slot stamp and the facts. Fixture proposals carry no gate
        # token, so the card is informational (not_actionable) and the loop is a HOLD.
        status, *headline = notes.roots[ts].split("\n")
        assert status == (
            f":heavy_multiplication_x: {slot_stamp(SLOT0)} • Portfolio: $100,000 • P&L: +$0"
            " • Orders: 0/200 • HOLD"
        )
        # D65: one bold-italic headline sentence under the status line
        assert headline == [
            "> _*No trade: no workable structure for NVDA and XOM; "
            "fixture Research reply (offline run).*_"
        ]
        assert LoopRoot.model_validate(LoopState(conn).root(chain) or {}).outcome.value == "hold"
        # the notifier is unbound again after the loop
        assert notes.thread_ts is None

    def test_root_updates_on_approval_and_rejection(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp, notes, poster, svc = _disp_with_cards(conn, routines)
        out = _slot_cards(disp, SLOT0)
        chain = out["research"].chain_run_id
        assert chain is not None
        ts = LoopState(conn).thread_ts(chain)
        assert ts is not None
        phash = conn.execute("SELECT proposal_hash FROM approval_requests").fetchone()[0]
        # a pending card (a tokened proposal awaiting the owner's click) → PENDING
        conn.execute(
            "UPDATE approval_requests SET status = 'pending' WHERE proposal_hash = ?", (phash,)
        )
        conn.commit()
        assert refresh_loop_root(conn, poster, chain) is not None
        assert poster.root_edits[-1][0] == ts
        assert poster.root_edits[-1][1].startswith(":hourglass_flowing_sand: ")
        assert poster.root_edits[-1][1].split("\n")[0].endswith("• PENDING: SPY")
        assert "SPY" in poster.root_edits[-1][1].split("\n")[1]  # D65 headline
        # the owner approves: the service re-renders the root → WIP (ladder running)
        res = svc.decide(phash, user=OWNER, approve=True, now=SLOT0 + dt.timedelta(minutes=1))
        assert res.outcome.value == "approved", res
        assert poster.root_edits[-1][0] == ts and "WIP: SPY" in poster.root_edits[-1][1]
        # a rejection (here: the request row as a Reject click leaves it) → HOLD again
        conn.execute(
            "UPDATE approval_requests SET status = 'rejected' WHERE proposal_hash = ?", (phash,)
        )
        conn.commit()
        svc.refresh_loop_root(phash)
        assert poster.root_edits[-1][1].split("\n")[0].endswith("• HOLD")
        assert (
            "SPY iron condor was rejected" in poster.root_edits[-1][1]
        )  # D65: the headline follows
        assert len({e[0] for e in poster.root_edits}) == 1
        # an unchanged root is not re-posted
        n = len(poster.root_edits)
        refresh_loop_root(conn, poster, chain)
        assert len(poster.root_edits) == n

    def test_no_change_and_hold_roots(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp, notes, _, _ = _disp_with_cards(conn, routines)
        _slot_cards(disp, SLOT0)
        _slot_cards(disp, SLOT0 + dt.timedelta(minutes=5))
        out = _slot_cards(disp, SLOT0 + dt.timedelta(minutes=10))
        assert out["research"].metrics["no_change"] is True
        chain = out["research"].chain_run_id
        assert chain is not None
        ts = LoopState(conn).thread_ts(chain)
        assert ts is not None
        status, headline = notes.roots[ts].split("\n")
        assert status.endswith("• HOLD (skip)")  # D65: renamed from "HOLD (no change)"
        assert headline == "> _*Nothing new since the last look; open orders stay.*_"
        assert notes.roots[ts].startswith(":heavy_multiplication_x: ")
        # a no_change loop gets only the [Routines] reply in its thread (no Scalp / Research card)
        replies = notes.in_thread(ts)
        assert len(replies) == 1 and replies[0].startswith("```\n[Routines] " + chain)
        assert "no_change" in replies[0]
        # three slots, three roots, each a distinct stamp
        stamps = [r.split(" • ")[0].split(" ", 1)[1] for r in notes.roots.values()]
        assert stamps == [slot_stamp(SLOT0 + dt.timedelta(minutes=5 * k)) for k in range(3)]

    def test_skipped_slot_posts_hold_root(self, routines: RoutinesConfig, tmp_path: Path) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        locks = LockManager(tmp_path)
        notes = RecordingNotifier()
        disp = _disp(conn, routines, locks=locks, notifier=notes)
        with locks.hold("research"):
            disp.run_job("research", SLOT0, reason="schedule", now=SLOT0, chain=True)
        assert list(notes.roots.values()) == [
            f":heavy_multiplication_x: {slot_stamp(SLOT0)} • Portfolio: n/a • P&L: n/a"
            " • Orders: n/a • HOLD (skipped: previous loop running)\n"
            "> _*Slot skipped: previous loop running.*_"
        ]
        # …unless post_hold_roots is off
        quiet = load_routines(overrides=_loop_overrides(post_hold_roots=False))
        notes2 = RecordingNotifier()
        disp2 = _disp(conn, quiet, locks=locks, notifier=notes2)
        with locks.hold("research"):
            disp2.run_job(
                "research",
                SLOT0 + dt.timedelta(minutes=5),
                reason="schedule",
                now=SLOT0 + dt.timedelta(minutes=5),
                chain=True,
            )
        assert notes2.roots == {}

    def test_day_thread_layout_is_the_rollback(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        legacy = load_routines(overrides=_loop_overrides(slack_layout="day_thread"))
        disp, notes, poster, _ = _disp_with_cards(conn, legacy)
        out = _slot_cards(disp, SLOT0)
        chain = out["research"].chain_run_id
        assert chain is not None
        assert notes.roots == {} and LoopState(conn).thread_ts(chain) is None
        assert notes.day_thread_posts() and poster.thread_of == [None]
        assert not any(t.startswith("```[Routines] chain-") for t in notes.day_thread_posts())

    def test_root_from_db_lists_fills(self, routines: RoutinesConfig) -> None:
        conn = _conn()
        _seed_scalp(conn, routines)
        disp, notes, poster, svc = _disp_with_cards(conn, routines)
        out = _slot_cards(disp, SLOT0)
        chain = out["research"].chain_run_id
        assert chain is not None
        phash = conn.execute("SELECT proposal_hash FROM approval_requests").fetchone()[0]
        conn.execute(
            "UPDATE approval_requests SET status = 'approved' WHERE proposal_hash = ?", (phash,)
        )
        conn.execute(
            """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
                   band_hi, max_steps, contracts, filled_qty, started_at)
               VALUES (?, 'open', 'filled', 'arc2', '1.00', '1.10', 3, 1, 1, ?)""",
            (phash, SLOT0.isoformat()),
        )
        conn.commit()
        root = loop_root_from_db(conn, chain, SLOT0)
        assert root.buys == ["SPY"] and root.pending == [] and root.working == []
        assert root.text().startswith(":white_check_mark: ")
        assert refresh_loop_root(conn, poster, chain) == root.text()
        status, headline = poster.root_edits[-1][1].split("\n")
        assert status.endswith("• BUY: SPY")
        # D65: one sentence; the thesis's own "; …" tail is cut to keep one point per clause
        assert headline == "> _*SPY iron condor x1 filled; FOMC hold is priced.*_"
