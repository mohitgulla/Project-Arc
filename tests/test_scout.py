"""Tests for the Scout candidate pipeline (E4.2)."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.config import ArcSettings
from arc.ingest.llm import (
    FIXTURE_MODEL,
    FixtureScoutLLM,
    HermesScoutLLM,
    LLMResult,
    ScoutLLMError,
)
from arc.ingest.scout import (
    FIXTURES_DIR,
    REJECT_SCHEMA,
    REJECT_SOURCE,
    REJECT_THRESHOLD,
    REJECT_UNIVERSE,
    _Doc,
    build_prompt,
    candidates_for_scanner,
    extract_json_object,
    load_fixture_docs,
    merge_candidates,
    normalize_ticker,
    parse_catalyst_date,
    render_doc,
    run_scout,
    store_candidate,
    validate_scout_candidate,
)
from arc.ingest.store import RawDocRepo, ScoutBatchRepo
from arc.models import Candidate, CatalystType, Stance
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 9, 28, 7, 0, tzinfo=ET)
DAY = "2026-09-28"
UNIVERSE = frozenset({"AAPL", "NVDA", "SPY", "XOM", "TSLA", "JPM", "META"})
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture()
def settings() -> ArcSettings:
    # Strict mode keeps the pre-D28 allow-list semantics these tests pin; the open
    # (seed) universe is covered by TestOpenUniverse / tests/test_universe.py.
    return ArcSettings(
        env="paper",
        universe=sorted(UNIVERSE),
        universe_mode="strict",
        scout_min_confidence=0.6,
        scout_batch_size=8,
        scout_max_doc_chars=500,
    )


def _cand(**kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "ticker": "AAPL",
        "stance": Stance.BULLISH,
        "catalyst_type": CatalystType.EARNINGS,
        "catalyst_date": None,
        "confidence": 0.7,
        "sources": [URL_A],
        "created_at": NOW,
    }
    base.update(kw)
    return Candidate(**base)


def _item(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticker": "AAPL",
        "stance": "bullish",
        "catalyst_type": "earnings",
        "catalyst_date": "2026-10-29",
        "confidence": 0.8,
        "sources": [URL_A],
        "rationale": "because",
    }
    base.update(kw)
    return base


def _validate(item: dict[str, Any]) -> Candidate | str:
    return validate_scout_candidate(
        item,
        universe=UNIVERSE,
        min_confidence=0.6,
        allowed_sources=frozenset({URL_A, URL_B}),
        created_at=NOW,
    )


def _seed(conn, n: int, *, ticker: str = "AAPL") -> list[str]:
    repo = RawDocRepo(conn)
    ids = []
    for i in range(n):
        ids.append(
            repo.insert(
                source="rss",
                url=f"https://example.com/{ticker.lower()}/{i}",
                published_at=f"2026-09-27T1{i % 10}:00:00+00:00",
                # distinct words per doc, so each seeded doc is its own story (D30)
                text=f"{ticker} news item {i}: topic{i} detail{i} angle{i}",
                tickers_hint=[ticker],
                id=f"doc-{i:03d}",
            )
        )
    return ids


def _reply(*items: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(items), "scan_summary": "s"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "want"), [("aapl", "AAPL"), (" $nvda ", "NVDA"), ("$ SPY", "SPY")]
    )
    def test_normalize_ticker(self, raw: str, want: str) -> None:
        assert normalize_ticker(raw) == want

    def test_extract_json_plain(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_extract_json_fenced_with_chatter(self) -> None:
        text = 'Sure!\n```json\n{"candidates": [{"x": {"y": 1}}]}\n```\nDone.'
        assert extract_json_object(text) == {"candidates": [{"x": {"y": 1}}]}

    @pytest.mark.parametrize("text", ["no json here", "} backwards {", "{not: json}"])
    def test_extract_json_errors(self, text: str) -> None:
        with pytest.raises(ValueError):
            extract_json_object(text)

    def test_parse_catalyst_date(self) -> None:
        assert parse_catalyst_date("2026-10-29") == dt.datetime(2026, 10, 29, tzinfo=ET)
        assert parse_catalyst_date("2026-10-29T20:00:00Z") == dt.datetime(2026, 10, 29, tzinfo=ET)
        assert parse_catalyst_date(None) is None
        assert parse_catalyst_date("") is None
        assert parse_catalyst_date("next week") is None

    def test_render_doc_truncates_and_neutralises_delimiter(self) -> None:
        doc = _Doc("d1", "rss", URL_A, "2026-09-27", "x" * 50 + "FEEDS>>>" + "y" * 50, [])
        out = render_doc(doc, max_chars=40)
        assert "FEEDS>>>" not in out
        assert out.endswith("…[truncated]")
        assert f"url={URL_A}" in out
        assert "tickers_hint=-" in out

    def test_prompt_contains_contract(self, settings: ArcSettings) -> None:
        doc = _Doc("d1", "rss", URL_A, "2026-09-27", "AAPL beats", ["AAPL"])
        prompt = build_prompt([doc], settings, DAY)
        assert "Scout" in prompt
        assert URL_A in prompt
        assert ">= 0.60" in prompt
        assert '"ScoutOutput"' in prompt  # JSON schema embedded
        assert "untrusted" in prompt
        for t in UNIVERSE:
            assert t in prompt


# ---------------------------------------------------------------------------
# Per-candidate validation
# ---------------------------------------------------------------------------


class TestValidate:
    def test_accepts_valid(self) -> None:
        c = _validate(_item(ticker="$aapl"))
        assert isinstance(c, Candidate)
        assert c.ticker == "AAPL"
        assert c.stance is Stance.BULLISH
        assert c.catalyst_date == dt.datetime(2026, 10, 29, tzinfo=ET)
        assert c.created_at == NOW

    def test_schema_reject_bad_stance(self) -> None:
        assert _validate(_item(stance="to the moon")) == REJECT_SCHEMA

    def test_schema_reject_bad_catalyst(self) -> None:
        assert _validate(_item(catalyst_type="vibes")) == REJECT_SCHEMA

    def test_schema_reject_missing_field(self) -> None:
        item = _item()
        del item["confidence"]
        assert _validate(item) == REJECT_SCHEMA

    def test_schema_reject_non_dict(self) -> None:
        assert _validate("AAPL bullish") == REJECT_SCHEMA  # type: ignore[arg-type]

    def test_schema_reject_malformed_ticker(self) -> None:
        # Passes universe check only if in universe; a weird symbol never is.
        assert _validate(_item(ticker="AAPL US")) == REJECT_UNIVERSE

    def test_universe_reject(self) -> None:
        assert _validate(_item(ticker="PLTR")) == REJECT_UNIVERSE

    def test_threshold_reject(self) -> None:
        assert _validate(_item(confidence=0.59)) == REJECT_THRESHOLD

    def test_threshold_inclusive(self) -> None:
        assert isinstance(_validate(_item(confidence=0.6)), Candidate)

    def test_ungrounded_sources_stripped(self) -> None:
        c = _validate(_item(sources=[URL_A, "https://hallucinated.example/x", URL_A]))
        assert isinstance(c, Candidate)
        assert c.sources == [URL_A]

    def test_all_sources_ungrounded(self) -> None:
        assert _validate(_item(sources=["https://hallucinated.example/x"])) == REJECT_SOURCE

    def test_bad_date_becomes_none(self) -> None:
        c = _validate(_item(catalyst_date="soon"))
        assert isinstance(c, Candidate)
        assert c.catalyst_date is None


# ---------------------------------------------------------------------------
# Merge (dedupe per ticker/day)
# ---------------------------------------------------------------------------


class TestMerge:
    def test_same_stance_max_conf_union_sources(self) -> None:
        a = _cand(confidence=0.7, sources=[URL_B])
        b = _cand(confidence=0.9, sources=[URL_A], catalyst_type=CatalystType.NEWS)
        m = merge_candidates(a, b)
        assert m.stance is Stance.BULLISH
        assert m.confidence == 0.9
        assert m.catalyst_type is CatalystType.NEWS
        assert m.sources == [URL_A, URL_B]

    def test_opposing_stance_penalised(self) -> None:
        m = merge_candidates(
            _cand(stance=Stance.BULLISH, confidence=0.8),
            _cand(stance=Stance.BEARISH, confidence=0.6),
        )
        assert m.stance is Stance.BULLISH
        assert m.confidence == pytest.approx(0.2)

    def test_opposing_equal_conf_is_neutral_zero(self) -> None:
        m = merge_candidates(
            _cand(stance=Stance.BULLISH, confidence=0.7),
            _cand(stance=Stance.BEARISH, confidence=0.7),
        )
        assert m.stance is Stance.NEUTRAL
        assert m.confidence == 0.0

    def test_keeps_known_catalyst_date_and_earliest_created(self) -> None:
        d = dt.datetime(2026, 10, 29, tzinfo=ET)
        early = NOW - dt.timedelta(hours=2)
        a = _cand(confidence=0.9, catalyst_date=None, created_at=NOW)
        b = _cand(confidence=0.7, catalyst_date=d, created_at=early, id="row1")
        m = merge_candidates(a, b)
        assert m.catalyst_date == d
        assert m.created_at == early
        assert m.id == "row1"

    def test_different_tickers_rejected(self) -> None:
        with pytest.raises(ValueError, match="different tickers"):
            merge_candidates(_cand(ticker="AAPL"), _cand(ticker="NVDA"))


_stances = st.sampled_from(list(Stance))
_cats = st.sampled_from(list(CatalystType))
_conf = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_srcs = st.lists(st.sampled_from([URL_A, URL_B, "https://example.com/c"]), min_size=1, max_size=3)
_dates = st.one_of(st.none(), st.dates(dt.date(2026, 9, 1), dt.date(2026, 12, 31)))


@st.composite
def candidates(draw: st.DrawFn) -> Candidate:
    d = draw(_dates)
    return _cand(
        stance=draw(_stances),
        catalyst_type=draw(_cats),
        confidence=draw(_conf),
        sources=draw(_srcs),
        catalyst_date=dt.datetime(d.year, d.month, d.day, tzinfo=ET) if d else None,
        created_at=NOW - dt.timedelta(minutes=draw(st.integers(0, 600))),
    )


class TestMergeProperties:
    @given(candidates(), candidates())
    def test_commutative(self, a: Candidate, b: Candidate) -> None:
        assert merge_candidates(a, b) == merge_candidates(b, a)

    @given(candidates(), candidates())
    def test_bounds_and_sources(self, a: Candidate, b: Candidate) -> None:
        m = merge_candidates(a, b)
        assert 0.0 <= m.confidence <= max(a.confidence, b.confidence)
        assert set(m.sources) == set(a.sources) | set(b.sources)
        assert m.created_at == min(a.created_at, b.created_at)
        if a.stance == b.stance:
            assert m.stance == a.stance
            assert m.confidence == max(a.confidence, b.confidence)

    @given(candidates())
    def test_idempotent(self, a: Candidate) -> None:
        m = merge_candidates(a, a)
        assert m == a.model_copy(update={"sources": sorted(set(a.sources))})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestStorage:
    def test_store_then_merge_same_row(self, conn) -> None:
        repo = CandidateRepo(conn)
        first = store_candidate(repo, _cand(confidence=0.7), day=DAY, run_id="r1")
        second = store_candidate(repo, _cand(confidence=0.9, sources=[URL_B]), day=DAY, run_id="r2")
        assert first.id == second.id
        rows = conn.execute("SELECT * FROM candidates").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["confidence"] == 0.9
        assert json.loads(row["sources"]) == [URL_A, URL_B]
        assert row["run_id"] == "r2"
        assert row["day"] == DAY

    def test_separate_rows_per_day(self, conn) -> None:
        repo = CandidateRepo(conn)
        store_candidate(repo, _cand(), day=DAY, run_id="r1")
        store_candidate(repo, _cand(), day="2026-09-29", run_id="r1")
        assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2

    def test_unique_index_enforced(self, conn) -> None:
        repo = CandidateRepo(conn)
        store_candidate(repo, _cand(), day=DAY, run_id="r1")
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, "
                "created_at, day) VALUES ('x', 'AAPL', 'bullish', 'news', 0.5, 'now', ?)",
                (DAY,),
            )

    def test_roundtrip_through_scanner_view(self, conn) -> None:
        d = dt.datetime(2026, 10, 29, tzinfo=ET)
        store_candidate(CandidateRepo(conn), _cand(catalyst_date=d), day=DAY, run_id="r")
        store_candidate(
            CandidateRepo(conn), _cand(ticker="XOM", confidence=0.61), day=DAY, run_id="r"
        )
        out = candidates_for_scanner(conn, DAY, min_confidence=0.65)
        assert [c.ticker for c in out] == ["AAPL"]
        assert out[0].catalyst_date == d
        assert out[0].created_at.tzinfo is not None
        assert out[0].id

    def test_mark_scouted(self, conn) -> None:
        ids = _seed(conn, 3)
        repo = RawDocRepo(conn)
        repo.mark_scouted(ids[:2], run_id="r")
        assert [d["id"] for d in repo.list_unscouted()] == [ids[2]]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class _RaisingLLM:
    model = "boom"

    def complete(self, prompt: str) -> LLMResult:
        raise ScoutLLMError("rate limited")


class TestRunScout:
    def test_happy_path_batches_and_audit(self, conn, settings) -> None:
        settings.scout_story_batch_size = 2
        _seed(conn, 3)
        llm = FixtureScoutLLM(
            [
                _reply(_item(sources=["https://example.com/aapl/0"], rationale="SECRET-A")),
                _reply(
                    _item(
                        stance="bullish",
                        confidence=0.9,
                        sources=["https://example.com/aapl/2", "https://nope.example/"],
                    ),
                    _item(ticker="PLTR", sources=["https://example.com/aapl/2"]),
                ),
            ]
        )
        res = run_scout(conn, settings, llm=llm, now=NOW, run_id="run-1")

        assert (res.batches, res.failed_batches, res.docs_scouted) == (2, 0, 3)
        assert res.accepted == 2
        assert res.rejected == {REJECT_UNIVERSE: 1}
        assert len(res.candidates) == 1
        c = res.candidates[0]
        assert (c.ticker, c.confidence) == ("AAPL", 0.9)
        assert c.sources == ["https://example.com/aapl/0", "https://example.com/aapl/2"]
        assert len(llm.prompts) == 2
        assert "https://example.com/aapl/0" in llm.prompts[0]
        assert "https://example.com/aapl/2" not in llm.prompts[0]

        batches = ScoutBatchRepo(conn).list_for_run("run-1")
        assert [b["status"] for b in batches] == ["ok", "ok"]
        assert json.loads(batches[1]["rejected"]) == {REJECT_UNIVERSE: 1}
        assert "SECRET-A" in batches[0]["raw_response"]  # audit keeps free text
        assert RawDocRepo(conn).list_unscouted() == []

    def test_second_run_is_noop(self, conn, settings) -> None:
        _seed(conn, 2)
        llm = FixtureScoutLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        run_scout(conn, settings, llm=llm, now=NOW)
        res = run_scout(conn, settings, llm=llm, now=NOW)
        assert res.batches == 0
        assert len(llm.prompts) == 1
        assert len(res.candidates) == 1

    def test_llm_error_leaves_docs_for_retry(self, conn, settings) -> None:
        _seed(conn, 2)
        res = run_scout(conn, settings, llm=_RaisingLLM(), now=NOW, run_id="r")
        assert (res.batches, res.failed_batches, res.docs_scouted) == (1, 1, 0)
        assert len(RawDocRepo(conn).list_unscouted()) == 2
        (b,) = ScoutBatchRepo(conn).list_for_run("r")
        assert (b["status"], b["model"], b["raw_response"]) == ("llm_error", "boom", None)
        assert "rate limited" in b["error"]

    @pytest.mark.parametrize(
        "text", ["I could not find anything.", '{"scan_summary": "x"}', '{"candidates": "AAPL"}']
    )
    def test_parse_error_leaves_docs_for_retry(self, conn, settings, text: str) -> None:
        _seed(conn, 1)
        res = run_scout(conn, settings, llm=FixtureScoutLLM([text]), now=NOW, run_id="r")
        assert res.failed_batches == 1
        assert len(RawDocRepo(conn).list_unscouted()) == 1
        (b,) = ScoutBatchRepo(conn).list_for_run("r")
        assert b["status"] == "parse_error"
        assert b["raw_response"] == text

    def test_empty_candidates_marks_scouted(self, conn, settings) -> None:
        _seed(conn, 1)
        res = run_scout(conn, settings, llm=FixtureScoutLLM([_reply()]), now=NOW)
        assert res.docs_scouted == 1
        assert res.candidates == []

    def test_day_is_et(self, conn, settings) -> None:
        _seed(conn, 1)
        late_utc = dt.datetime(2026, 9, 29, 2, 0, tzinfo=dt.UTC)  # 22:00 ET on the 28th
        llm = FixtureScoutLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        res = run_scout(conn, settings, llm=llm, now=late_utc)
        assert res.day == "2026-09-28"

    def test_dry_run_fixtures_end_to_end(self, conn, settings) -> None:
        # strict mode (fixture settings): PLTR / UFPT / ZZZQ are all not_in_universe
        assert load_fixture_docs(conn) == 11
        assert load_fixture_docs(conn) == 0  # dedupe by content hash
        res = run_scout(conn, settings, dry_run=True, now=NOW)
        assert res.dry_run
        assert res.failed_batches == 0
        assert res.docs_scouted == 11
        assert dict(res.rejected) == {
            REJECT_UNIVERSE: 3,
            REJECT_THRESHOLD: 1,
            REJECT_SOURCE: 1,
            REJECT_SCHEMA: 1,
        }
        by_ticker = {c.ticker: c for c in res.candidates}
        # AAPL: bullish 0.8 vs bearish 0.6 → 0.2, below threshold → not surfaced.
        assert set(by_ticker) == {"NVDA", "XOM", "SPY"}
        assert by_ticker["SPY"].sources == ["https://www.youtube.com/watch?v=fixture0005"]
        stored = CandidateRepo(conn).get_for_day("AAPL", res.day)
        assert stored is not None
        assert stored["confidence"] == pytest.approx(0.2)
        models = {r[0] for r in conn.execute("SELECT model FROM scout_batches")}
        assert models == {FIXTURE_MODEL}

    def test_dry_run_default_backend_is_fixture(self, conn, settings, monkeypatch) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise AssertionError("network/subprocess must not be used in dry-run")

        monkeypatch.setattr(subprocess, "run", boom)
        load_fixture_docs(conn)
        run_scout(conn, settings, dry_run=True, now=NOW)

    def test_live_default_backend_is_hermes(self, conn, settings, monkeypatch) -> None:
        seen: list[Any] = []

        class Spy:
            def complete(self, prompt: str) -> LLMResult:
                seen.append(prompt)
                return LLMResult(_reply(), "spy")

        monkeypatch.setattr(HermesScoutLLM, "from_settings", classmethod(lambda cls, s: Spy()))
        _seed(conn, 1)
        run_scout(conn, settings, now=NOW)
        # live: stage-1 digest call, then the stage-2 Scout call, both on the backend
        assert len(seen) == 2
        assert "story digest (stage 1)" in seen[0]
        assert "story digests (D30)" in seen[1]


# ---------------------------------------------------------------------------
# Hermes backend
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self, *, rc: int = 0, stdout: str = "", usage: dict | None = None, exc=None):
        self.rc, self.stdout, self.usage, self.exc = rc, stdout, usage, exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"cmd": cmd, **kw})
        if self.exc:
            raise self.exc
        if self.usage is not None:
            usage_path = Path(cmd[cmd.index("--usage-file") + 1])
            usage_path.write_text(json.dumps(self.usage))
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, "boom stderr")


def _hermes(runner: _Runner) -> HermesScoutLLM:
    return HermesScoutLLM(model="claude-opus-5", provider="anthropic", runner=runner)


class TestHermesBackend:
    def test_from_settings_uses_cheap_tier(self) -> None:
        s = ArcSettings(env="paper")
        llm = HermesScoutLLM.from_settings(s)
        assert llm.model == "anthropic/claude-opus-5"
        assert llm.provider == "anthropic"
        assert llm.timeout_seconds == s.scout_timeout_seconds

    def test_command_and_isolation(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
        monkeypatch.setenv("KEEP_ME", "1")
        runner = _Runner(stdout='{"candidates": []}', usage={"model": "claude-opus-5"})
        out = _hermes(runner).complete("PROMPT")
        assert out == LLMResult('{"candidates": []}', "claude-opus-5")
        call = runner.calls[0]
        cmd = call["cmd"]
        assert cmd[:3] == ["hermes", "-z", "PROMPT"]
        assert cmd[cmd.index("-m") + 1] == "claude-opus-5"
        assert cmd[cmd.index("--provider") + 1] == "anthropic"
        assert "--ignore-rules" in cmd
        assert cmd[cmd.index("-t") + 1] == "todo"
        assert "HERMES_KANBAN_TASK" not in call["env"]
        assert call["env"]["KEEP_ME"] == "1"
        assert call["cwd"] != str(Path.cwd())

    def test_missing_usage_falls_back_to_configured_model(self) -> None:
        out = _hermes(_Runner(stdout="{}")).complete("p")
        assert out.model == "claude-opus-5"

    def test_nonzero_exit(self) -> None:
        with pytest.raises(ScoutLLMError, match="exited 1: boom stderr"):
            _hermes(_Runner(rc=1)).complete("p")

    def test_usage_failed_flag(self) -> None:
        with pytest.raises(ScoutLLMError):
            _hermes(_Runner(usage={"failed": True})).complete("p")

    @pytest.mark.parametrize(
        "exc", [subprocess.TimeoutExpired(["hermes"], 1), FileNotFoundError("hermes")]
    )
    def test_transport_errors(self, exc: Exception) -> None:
        with pytest.raises(ScoutLLMError, match="hermes call failed"):
            _hermes(_Runner(exc=exc)).complete("p")


class TestFixtureBackend:
    def test_exhausted_returns_empty_scan(self) -> None:
        llm = FixtureScoutLLM(["A"])
        assert llm.complete("p1").text == "A"
        assert json.loads(llm.complete("p2").text)["candidates"] == []

    def test_from_dir_sorted(self) -> None:
        llm = FixtureScoutLLM.from_dir(FIXTURES_DIR / "responses")
        assert len(llm.responses) == 1  # D30: one stage-2 call reads every story digest
        assert "Fixture scan" in llm.responses[0]


# ---------------------------------------------------------------------------
# Funnel discipline
# ---------------------------------------------------------------------------


class TestFunnelDiscipline:
    def test_candidate_has_no_free_text_fields(self) -> None:
        free_text = {
            name
            for name, f in Candidate.model_fields.items()
            if f.annotation in (str, str | None) and name not in {"id", "ticker"}
        }
        assert free_text == set()

    def test_candidate_forbids_rationale(self) -> None:
        with pytest.raises(ValidationError):
            _cand(rationale="the LLM said so")

    @pytest.mark.parametrize("src", ["", "two words", "x" * 3000])
    def test_sources_must_be_reference_tokens(self, src: str) -> None:
        with pytest.raises(ValidationError):
            _cand(sources=[src])

    @pytest.mark.parametrize("ticker", ["aapl", "AAPL US", "", "TOOLONGTICKER"])
    def test_ticker_pattern(self, ticker: str) -> None:
        with pytest.raises(ValidationError):
            _cand(ticker=ticker)

    def test_scanner_view_carries_no_persona_text(self, conn, settings) -> None:
        _seed(conn, 1)
        llm = FixtureScoutLLM(
            [
                json.dumps(
                    {
                        "candidates": [
                            _item(sources=["https://example.com/aapl/0"], rationale="RAT-XYZ")
                        ],
                        "scan_summary": "SUMMARY-XYZ",
                    }
                )
            ]
        )
        res = run_scout(conn, settings, llm=llm, now=NOW)
        blob = json.dumps([c.model_dump(mode="json") for c in res.candidates])
        assert "RAT-XYZ" not in blob
        assert "SUMMARY-XYZ" not in blob
        assert "news item" not in blob  # raw doc text

    def test_downstream_packages_never_touch_unstructured_stores(self) -> None:
        root = Path(__file__).resolve().parent.parent / "arc"
        forbidden = re.compile(r"raw_docs|scout_batches|RawDoc|rationale|scan_summary|arc\.ingest")
        offenders = [
            str(p.relative_to(root))
            for pkg in ("scanner", "structures", "gate", "pricing", "execution")
            for p in (root / pkg).rglob("*.py")
            if forbidden.search(p.read_text())
        ]
        assert offenders == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_scan_dry_run() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "arc.cli", "scan", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["docs_scouted"] == 11
    # seed mode (default): PLTR is a new, liquid name; with no symbol master cached
    # (tests are hermetic) non-seed names fail closed as unknown_symbol.
    assert {c["ticker"] for c in report["candidates"]} == {"NVDA", "XOM", "SPY"}
    assert all("rationale" not in c for c in report["candidates"])
