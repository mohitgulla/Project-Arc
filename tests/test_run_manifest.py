"""E5.6 / D27: run manifests, one per routine run attempt, on every path."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from arc.routines.config import RoutinesConfig
from arc.routines.dispatcher import Dispatcher
from arc.routines.handlers import JobContext, JobResult, JobSkippedError
from arc.routines.heartbeat import RecordingNotifier
from arc.routines.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ExternalInput,
    ManifestRepo,
    RunManifest,
    config_hashes,
    digest,
)
from arc.routines.runs import RoutineRunRepo, RunStatus
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NOW = dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET)
SECRET = "sk-test-DO-NOT-LEAK-0123456789"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _cfg(writes: str = "[shortlist, note]") -> RoutinesConfig:
    import yaml

    text = f"""
personas:
  director: {{schedule: ['09:00'], writes: {writes}, chain: [quant]}}
steps:
  quant: {{writes: [structures]}}
"""
    return RoutinesConfig.model_validate(yaml.safe_load(text))


def _dispatch(
    conn: sqlite3.Connection,
    handlers: dict[str, Callable[[JobContext], JobResult]],
    routines: RoutinesConfig | None = None,
) -> Dispatcher:
    return Dispatcher(
        conn,
        routines or _cfg(),
        handlers=handlers,
        notifier=RecordingNotifier(),
        is_halted=lambda: False,
    )


def _director_ok(ctx: JobContext) -> JobResult:
    ctx.record_input("chain:SPY", "fixture", {"bid": 1, "ask": 2}, as_of=ctx.now, count=2)
    ctx.write(
        "shortlist",
        "session",
        {"shortlist": [], "market_regime": "risk_on", "session_notes": "flat"},
    )
    ctx.write(
        "note",
        "market",
        {"persona": "director", "topic": "regime_view", "title": "t", "body": "calm"},
    )
    return JobResult(summary="ok", metrics={"picked": 0})


def _quant_ok(ctx: JobContext) -> JobResult:
    return JobResult(summary="quant ok")


def _manifests(conn: sqlite3.Connection, job: str) -> list[RunManifest]:
    run = RoutineRunRepo(conn).history(job=job)[0]
    return ManifestRepo(conn).for_run(run.run_id)


class TestManifestWritten:
    def test_ok_run_has_full_manifest(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_API_SECRET_KEY", SECRET)
        monkeypatch.setenv("ARC_GATE_SECRET", SECRET)
        d = _dispatch(conn, {"director": _director_ok, "quant": _quant_ok})
        d.run_manual("director", now=NOW, chain=True)
        (m,) = _manifests(conn, "director")
        assert m.schema_version == MANIFEST_SCHEMA_VERSION
        assert m.status == "ok" and m.error_class is None
        assert m.job == "director" and m.job_kind == "persona" and m.attempt == 1
        assert m.declared_writes == ["shortlist", "note"]
        assert set(m.output_ids) == {"shortlist", "note"}
        assert m.metrics == {"picked": 0}
        assert m.external_inputs[0].name == "chain:SPY"
        assert m.external_inputs[0].digest == digest({"ask": 2, "bid": 1})
        assert m.external_inputs[0].count == 2
        assert m.kind_schema_versions["shortlist"] >= 1
        assert "routines.yaml" in m.config_hashes
        assert m.effective_spec["writes"] == ["shortlist", "note"]
        assert m.market_session in {"pre", "open", "post", "closed"}
        assert m.trading_day == NOW.date()
        assert m.notifications  # the heartbeat summary's ts
        assert m.tick_now.tzinfo is not None
        assert m.arc_version and m.python and m.host
        # never a secret value, anywhere in the stored row
        row = conn.execute("SELECT payload FROM run_manifests").fetchall()
        assert all(SECRET not in r["payload"] for r in row)

    def test_chain_step_links_parent_run(self, conn: sqlite3.Connection) -> None:
        d = _dispatch(conn, {"director": _director_ok, "quant": _quant_ok})
        d.run_manual("director", now=NOW, chain=True)
        (director,) = _manifests(conn, "director")
        (quant,) = _manifests(conn, "quant")
        assert quant.chain_run_id == director.chain_run_id is not None
        assert quant.parent_run_id == director.run_id
        assert quant.step_index == 1
        assert quant.declared_writes == ["structures"]

    def test_failed_run_has_manifest(self, conn: sqlite3.Connection) -> None:
        def boom(ctx: JobContext) -> JobResult:
            msg = "chain fetch timed out"
            raise TimeoutError(msg)

        _dispatch(conn, {"director": boom, "quant": _quant_ok}).run_manual("director", now=NOW)
        (m,) = _manifests(conn, "director")
        assert m.status == "failed"
        assert m.error_class == "TimeoutError"
        assert m.error is not None and "timed out" in m.error
        assert m.output_ids == {}

    def test_skipped_run_has_manifest(self, conn: sqlite3.Connection) -> None:
        def skip(ctx: JobContext) -> JobResult:
            msg = "nothing to do"
            raise JobSkippedError(msg)

        _dispatch(conn, {"director": skip, "quant": _quant_ok}).run_manual("director", now=NOW)
        (m,) = _manifests(conn, "director")
        assert m.status == "skipped" and m.error_class is None

    def test_halted_schedule_skip_has_manifest(self, conn: sqlite3.Connection) -> None:
        d = Dispatcher(
            conn,
            _cfg(),
            handlers={"director": _director_ok, "quant": _quant_ok},
            notifier=RecordingNotifier(),
            is_halted=lambda: True,
        )
        d.tick(NOW, since=NOW - dt.timedelta(minutes=5))
        runs = RoutineRunRepo(conn).history(job="director")
        assert runs and runs[0].status is RunStatus.SKIPPED
        (m,) = ManifestRepo(conn).for_run(runs[0].run_id)
        assert m.status == "skipped" and m.halted is True

    def test_manifest_failure_never_fails_job(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_a: object, **_k: object) -> RunManifest:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr("arc.routines.manifest.build_manifest", broken)
        notifier = RecordingNotifier()
        d = Dispatcher(
            conn,
            _cfg(),
            handlers={"director": _director_ok, "quant": _quant_ok},
            notifier=notifier,
            is_halted=lambda: False,
        )
        d.run_manual("director", now=NOW)
        run = RoutineRunRepo(conn).history(job="director")[0]
        assert run.status is RunStatus.OK
        assert any("run manifest not written" in text for _, text in notifier.posts)


class TestAppendOnly:
    def test_update_and_delete_rejected(self, conn: sqlite3.Connection) -> None:
        _dispatch(conn, {"director": _director_ok, "quant": _quant_ok}).run_manual(
            "director", now=NOW
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE run_manifests SET status = 'ok'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM run_manifests")

    def test_manifest_rejects_extra_fields(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExternalInput.model_validate({"name": "x", "source": "y", "secret": "z"})


class TestLLMUsage:
    def test_persona_calls_summed(self, conn: sqlite3.Connection) -> None:
        from arc.journal.models import PersonaCallMeta
        from arc.pipeline.store import PersonaCallRepo

        def director(ctx: JobContext) -> JobResult:
            repo = PersonaCallRepo(ctx.conn)
            for i in range(2):
                repo.insert(
                    run_id=ctx.run_id,
                    persona="director",
                    model="claude-test",
                    snapshot_id=None,
                    prompt=f"p{i}",
                    raw_response="{}",
                    status="ok",
                    dropped={"duplicate": 1},
                    at=ctx.now,
                    meta=PersonaCallMeta(
                        prompt_text=f"p{i}", input_tokens=10, output_tokens=5, latency_ms=100
                    ),
                )
            return JobResult(summary="ok")

        _dispatch(conn, {"director": director, "quant": _quant_ok}).run_manual("director", now=NOW)
        (m,) = _manifests(conn, "director")
        assert len(m.persona_call_ids) == 2
        assert m.models_served == ["claude-test"]
        assert (m.input_tokens, m.output_tokens, m.llm_latency_ms) == (20, 10, 200)
        assert m.dropped == {"duplicate": 2}
        assert len(m.prompt_sha256) == 2


class TestHelpers:
    def test_digest_canonical(self) -> None:
        assert digest({"a": 1, "b": [1, 2]}) == digest({"b": [1, 2], "a": 1})
        assert digest({"a": 1}) != digest({"a": 2})

    def test_config_hashes_cover_config_dir(self) -> None:
        hashes = config_hashes()
        assert "routines.yaml" in hashes
        assert all(len(v) == 64 for v in hashes.values())

    def test_manifest_json_roundtrip(self, conn: sqlite3.Connection) -> None:
        _dispatch(conn, {"director": _director_ok, "quant": _quant_ok}).run_manual(
            "director", now=NOW
        )
        raw = conn.execute("SELECT payload FROM run_manifests").fetchone()["payload"]
        assert RunManifest.model_validate(json.loads(raw)).job == "director"


class TestTraceCli:
    def test_trace_chain_json_and_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from arc.cli import main
        from arc.config import ArcSettings
        from arc.pipeline.runner import fixture_run
        from arc.routines.config import load_routines

        db = str(tmp_path / "arc.db")
        conn, report = fixture_run(ArcSettings(_env_file=None), load_routines(), db=db)  # type: ignore[call-arg]
        chain = report.outcomes[1].chain_run_id
        assert chain
        capsys.readouterr()
        assert main(["context", "trace", chain, "--db", db, "--json"]) == 0
        steps = json.loads(capsys.readouterr().out)
        assert [s["job"] for s in steps] == ["director", "quant", "risk", "propose"]
        for s in steps:
            assert set(s) >= {"job", "run_id", "status", "declared", "read", "wrote",
                              "persona_calls", "manifest"}  # fmt: skip
            assert {w["kind"] for w in s["wrote"]} <= set(s["declared"]["writes"])
            assert s["manifest"]["run_id"] == s["run_id"]
        assert steps[1]["persona_calls"] and steps[1]["read"]
        assert any(w["kind"] == "note" for w in steps[0]["wrote"])
        # every routine run in the DB has exactly one manifest
        runs = conn.execute("SELECT COUNT(*) FROM routine_runs").fetchone()[0]
        mans = conn.execute("SELECT COUNT(DISTINCT run_id) FROM run_manifests").fetchone()[0]
        assert runs == mans == 5
        assert main(["context", "trace", chain, "--db", db]) == 0
        text = capsys.readouterr().out
        assert text.count("== ") == 4 and "declared reads=" in text and "routines.yaml=" in text
        assert main(["context", "trace", steps[1]["run_id"], "--db", db, "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 1
        assert main(["context", "trace", "run-nope", "--db", db]) == 1

    def test_quant_manifest_complete(self) -> None:
        from arc.config import ArcSettings
        from arc.pipeline.runner import fixture_run
        from arc.routines.config import load_routines

        conn, report = fixture_run(ArcSettings(_env_file=None), load_routines())  # type: ignore[call-arg]
        quant = next(o for o in report.outcomes if o.job == "quant")
        m = ManifestRepo(conn).latest(quant.run_id)
        assert m is not None
        assert m.declared_writes == ["structures", "note"]
        assert m.output_ids["structures"]
        assert m.persona_call_ids
        assert len(m.input_digest) == 64 and int(m.input_digest, 16) >= 0
        assert "routines.yaml" in m.config_hashes
        assert any(x.name == "chain:SPY" for x in m.external_inputs)
        assert m.market_session
        assert m.models_served == ["fixture"]

    def test_logs_carry_run_id(self, conn: sqlite3.Connection) -> None:
        import structlog

        def director(ctx: JobContext) -> JobResult:
            structlog.get_logger("t").info("handler.event")
            return _director_ok(ctx)

        with structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars]
        ) as logs:
            _dispatch(conn, {"director": director, "quant": _quant_ok}).run_manual(
                "director", now=NOW
            )
        ev = next(e for e in logs if e["event"] == "handler.event")
        run = RoutineRunRepo(conn).history(job="director")[0]
        assert ev["run_id"] == run.run_id and ev["job"] == "director"
