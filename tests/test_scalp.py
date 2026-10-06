"""Tests for the Scalp candidate pipeline (E4.2)."""

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
    FixtureScalpLLM,
    HermesScalpLLM,
    LLMResult,
    ScalpLLMError,
)
from arc.ingest.scalp import (
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
    run_scalp,
    store_candidate,
    validate_scalp_candidate,
)
from arc.ingest.store import RawDocRepo, ScalpBatchRepo
from arc.models import Candidate, CatalystType, Stance
from arc.pipeline.env import FIXTURE_NOW
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
        scalp_min_confidence=0.6,
        scalp_batch_size=8,
        scalp_max_doc_chars=500,
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
    return validate_scalp_candidate(
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
                # D47: inside the 6h market_news window at NOW (newest = highest i)
                published_at=(NOW - dt.timedelta(hours=5) + dt.timedelta(minutes=i)).isoformat(),
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
        assert "Scalp" in prompt
        assert URL_A in prompt
        assert ">= 0.60" in prompt
        assert '"ScalpOutput"' in prompt  # JSON schema embedded
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

    def test_mark_scalped(self, conn) -> None:
        ids = _seed(conn, 3)
        repo = RawDocRepo(conn)
        repo.mark_scalped(ids[:2], run_id="r")
        assert [d["id"] for d in repo.list_unscalped()] == [ids[2]]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class _RaisingLLM:
    model = "boom"

    def complete(self, prompt: str) -> LLMResult:
        raise ScalpLLMError("rate limited")


class TestRunScalp:
    def test_happy_path_batches_and_audit(self, conn, settings) -> None:
        settings.scalp_story_batch_size = 2
        _seed(conn, 3)
        llm = FixtureScalpLLM(
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
        res = run_scalp(conn, settings, llm=llm, now=NOW, run_id="run-1")

        assert (res.batches, res.failed_batches, res.docs_scalped) == (2, 0, 3)
        assert res.accepted == 2
        assert res.rejected == {REJECT_UNIVERSE: 1}
        assert len(res.candidates) == 1
        c = res.candidates[0]
        assert (c.ticker, c.confidence) == ("AAPL", 0.9)
        assert c.sources == ["https://example.com/aapl/0", "https://example.com/aapl/2"]
        assert len(llm.prompts) == 2
        assert "https://example.com/aapl/0" in llm.prompts[0]
        assert "https://example.com/aapl/2" not in llm.prompts[0]

        batches = ScalpBatchRepo(conn).list_for_run("run-1")
        assert [b["status"] for b in batches] == ["ok", "ok"]
        assert json.loads(batches[1]["rejected"]) == {REJECT_UNIVERSE: 1}
        assert "SECRET-A" in batches[0]["raw_response"]  # audit keeps free text
        assert RawDocRepo(conn).list_unscalped() == []

    def test_second_run_is_noop(self, conn, settings) -> None:
        _seed(conn, 2)
        llm = FixtureScalpLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        run_scalp(conn, settings, llm=llm, now=NOW)
        res = run_scalp(conn, settings, llm=llm, now=NOW)
        assert res.batches == 0
        assert len(llm.prompts) == 1
        assert len(res.candidates) == 1

    def test_llm_error_leaves_docs_for_retry(self, conn, settings) -> None:
        _seed(conn, 2)
        res = run_scalp(conn, settings, llm=_RaisingLLM(), now=NOW, run_id="r")
        assert (res.batches, res.failed_batches, res.docs_scalped) == (1, 1, 0)
        assert len(RawDocRepo(conn).list_unscalped()) == 2
        (b,) = ScalpBatchRepo(conn).list_for_run("r")
        assert (b["status"], b["model"], b["raw_response"]) == ("llm_error", "boom", None)
        assert "rate limited" in b["error"]

    @pytest.mark.parametrize(
        "text", ["I could not find anything.", '{"scan_summary": "x"}', '{"candidates": "AAPL"}']
    )
    def test_parse_error_leaves_docs_for_retry(self, conn, settings, text: str) -> None:
        _seed(conn, 1)
        res = run_scalp(conn, settings, llm=FixtureScalpLLM([text]), now=NOW, run_id="r")
        assert res.failed_batches == 1
        assert len(RawDocRepo(conn).list_unscalped()) == 1
        (b,) = ScalpBatchRepo(conn).list_for_run("r")
        assert b["status"] == "parse_error"
        assert b["raw_response"] == text

    def test_empty_candidates_marks_scalped(self, conn, settings) -> None:
        _seed(conn, 1)
        res = run_scalp(conn, settings, llm=FixtureScalpLLM([_reply()]), now=NOW)
        assert res.docs_scalped == 1
        assert res.candidates == []

    def test_day_is_et(self, conn, settings) -> None:
        _seed(conn, 1)
        late_utc = dt.datetime(2026, 9, 29, 2, 0, tzinfo=dt.UTC)  # 22:00 ET on the 28th
        llm = FixtureScalpLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        res = run_scalp(conn, settings, llm=llm, now=late_utc)
        assert res.day == "2026-09-28"

    def test_dry_run_fixtures_end_to_end(self, conn, settings) -> None:
        # strict mode (fixture settings): PLTR / UFPT / ZZZQ are all not_in_universe
        assert load_fixture_docs(conn) == 11
        assert load_fixture_docs(conn) == 0  # dedupe by content hash
        res = run_scalp(conn, settings, dry_run=True, now=FIXTURE_NOW)  # D47: fixture clock
        assert res.dry_run
        assert res.failed_batches == 0
        # D54: the fixture's one `source: earnings` doc is the slow feed now; it is
        # closed `slow_feed`, never read, and still in raw_docs for next_earnings().
        assert res.docs_scalped == 10
        assert res.slow_feed == 1
        q = "SELECT scalp_status FROM raw_docs WHERE source = 'earnings'"
        assert [r[0] for r in conn.execute(q)] == ["slow_feed"]
        assert dict(res.rejected) == {
            REJECT_UNIVERSE: 3,
            REJECT_SOURCE: 1,
            REJECT_SCHEMA: 1,
        }
        by_ticker = {c.ticker: c for c in res.candidates}
        # E12.4: core names skip the confidence floor. JPM (0.4) and AAPL (bullish 0.8
        # vs bearish 0.6 → 0.2) are core, so both are kept below 0.6.
        assert set(by_ticker) == {"NVDA", "XOM", "SPY", "JPM", "AAPL"}
        assert res.floor_skipped == {
            "AAPL": ("core", pytest.approx(0.2)),
            "JPM": ("core", pytest.approx(0.4)),
        }
        assert by_ticker["SPY"].sources == ["https://example.com/news/fed-preview-transcript"]
        stored = CandidateRepo(conn).get_for_day("AAPL", res.day)
        assert stored is not None
        assert stored["confidence"] == pytest.approx(0.2)
        models = {r[0] for r in conn.execute("SELECT model FROM scalp_batches")}
        assert models == {FIXTURE_MODEL}

    def test_dry_run_default_backend_is_fixture(self, conn, settings, monkeypatch) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise AssertionError("network/subprocess must not be used in dry-run")

        monkeypatch.setattr(subprocess, "run", boom)
        load_fixture_docs(conn)
        run_scalp(conn, settings, dry_run=True, now=FIXTURE_NOW)

    def test_live_default_backend_is_hermes(self, conn, settings, monkeypatch) -> None:
        seen: list[Any] = []

        class Spy:
            def complete(self, prompt: str) -> LLMResult:
                seen.append(prompt)
                return LLMResult(_reply(), "spy")

        monkeypatch.setattr(HermesScalpLLM, "from_settings", classmethod(lambda cls, s: Spy()))
        _seed(conn, 1)
        run_scalp(conn, settings, now=NOW)
        # live: stage-1 digest call, then the stage-2 Scalp call, both on the backend
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


def _hermes(runner: _Runner) -> HermesScalpLLM:
    return HermesScalpLLM(model="claude-opus-5", provider="anthropic", runner=runner)


class TestHermesBackend:
    def test_from_settings_uses_cheap_tier(self) -> None:
        s = ArcSettings(env="paper")
        llm = HermesScalpLLM.from_settings(s)
        assert llm.model == "anthropic/claude-opus-5"
        assert llm.provider == "anthropic"
        assert llm.timeout_seconds == s.scalp_timeout_seconds

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
        with pytest.raises(ScalpLLMError, match="exited 1: boom stderr"):
            _hermes(_Runner(rc=1)).complete("p")

    def test_usage_failed_flag(self) -> None:
        with pytest.raises(ScalpLLMError):
            _hermes(_Runner(usage={"failed": True})).complete("p")

    @pytest.mark.parametrize(
        "exc", [subprocess.TimeoutExpired(["hermes"], 1), FileNotFoundError("hermes")]
    )
    def test_transport_errors(self, exc: Exception) -> None:
        with pytest.raises(ScalpLLMError, match="hermes call failed"):
            _hermes(_Runner(exc=exc)).complete("p")


class TestFixtureBackend:
    def test_exhausted_returns_empty_scan(self) -> None:
        llm = FixtureScalpLLM(["A"])
        assert llm.complete("p1").text == "A"
        assert json.loads(llm.complete("p2").text)["candidates"] == []

    def test_from_dir_sorted(self) -> None:
        llm = FixtureScalpLLM.from_dir(FIXTURES_DIR / "responses")
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
        llm = FixtureScalpLLM(
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
        res = run_scalp(conn, settings, llm=llm, now=NOW)
        blob = json.dumps([c.model_dump(mode="json") for c in res.candidates])
        assert "RAT-XYZ" not in blob
        assert "SUMMARY-XYZ" not in blob
        assert "news item" not in blob  # raw doc text

    def test_downstream_packages_never_touch_unstructured_stores(self) -> None:
        root = Path(__file__).resolve().parent.parent / "arc"
        forbidden = re.compile(r"raw_docs|scalp_batches|RawDoc|rationale|scan_summary|arc\.ingest")
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
    assert report["docs_scalped"] == 10  # D54: the earnings doc is the slow feed
    # seed mode (default): with no symbol master cached (tests are hermetic) non-seed
    # names fail closed as unknown_symbol. D51: PLTR is core now (admitted); SPY is
    # the market reference, not a trade name, so it fails closed like any non-seed.
    # E12.4: core JPM (0.4) and AAPL (0.2) skip the confidence floor.
    assert {c["ticker"] for c in report["candidates"]} == {"NVDA", "XOM", "PLTR", "JPM", "AAPL"}
    assert all("rationale" not in c for c in report["candidates"])


# ---------------------------------------------------------------------------
# E13.10 (D56): options tape, two-category budget, out-of-tier mentions
# ---------------------------------------------------------------------------


def _tape_routines(on: bool) -> Any:
    from arc.routines.config import load_routines

    return load_routines(overrides={("personas", "scalp_options_tape"): "on" if on else "off"})


def _store_tape(conn, at: dt.datetime, pcs: dict[str, float | None]) -> None:
    """Seed the three options_fast kinds as the E13.6 source writes them at *at*."""
    from arc.context.kinds import ChainSnapshotPayload, IndexVol, IndexVolsPayload
    from arc.context.store import ContextStore

    store = ContextStore(conn)
    fetched = at.replace(microsecond=0).isoformat()
    store.write(
        kind="index_vols",
        subject="market",
        payload=IndexVolsPayload(
            fetched_at=fetched,
            quotes=[
                IndexVol(symbol="VIX9D", value=16.5, as_of=fetched),
                IndexVol(symbol="VIX", value=17.6, as_of=fetched),
                IndexVol(symbol="VXN", value=21.0, as_of=fetched),
            ],
            ratio_9d_30d=0.9375,
        ),
        produced_by="options_fast",
        ttl="1h",
        valid_from=at,
        now=at,
    )
    for t, pc in pcs.items():
        calls = 10_000
        store.write(
            kind="chain_snapshot",
            subject=t,
            payload=ChainSnapshotPayload(
                ticker=t,
                fetched_at=fetched,
                spot=100.0,
                expiry="2026-11-20",
                call_volume_td=calls,
                put_volume_td=int(calls * pc) if pc is not None else 0,
                put_call_volume=pc,
                atm_spread_pct=0.02,
                atm_oi=1234,
                book=[],
            ),
            produced_by="options_fast",
            ttl="1h",
            valid_from=at,
            now=at,
        )


class TestOptionsTape:
    def test_direction_thresholds(self) -> None:
        from arc.ingest.cboe_fast import tape_corroborates, tape_direction

        assert tape_direction(None) == "unknown"
        assert tape_direction(0.7) == "bullish"  # inclusive
        assert tape_direction(0.71) == "neutral"
        assert tape_direction(1.29) == "neutral"
        assert tape_direction(1.3) == "bearish"  # inclusive
        assert tape_direction(0.9, bull=1.0) == "bullish"
        assert tape_corroborates("bullish", "bullish")
        assert tape_corroborates("bearish", "bearish")
        assert not tape_corroborates("bullish", "bearish")
        assert not tape_corroborates("neutral", "neutral")  # never for a neutral stance
        assert not tape_corroborates("bullish", None)

    @given(pc=st.floats(min_value=0.0, max_value=10.0, allow_nan=False))
    def test_direction_partition(self, pc: float) -> None:
        from arc.ingest.cboe_fast import tape_direction

        d = tape_direction(pc)
        assert (d == "bullish") == (pc <= 0.7)
        assert (d == "bearish") == (pc >= 1.3)

    def test_tape_block_present(self, conn) -> None:
        from arc.ingest.cboe_fast import scalp_tape_from_store

        at = NOW.replace(microsecond=594_000)  # sub-second write, like a real tick
        _store_tape(conn, at, {"AAPL": 0.5, "NVDA": 1.6, "XOM": None})
        tape = scalp_tape_from_store(conn, at, dt.timedelta(minutes=30))
        assert tape.present and (tape.vix, tape.vix9d, tape.vxn) == (17.6, 16.5, 21.0)
        assert {t: v.direction for t, v in tape.tickers.items()} == {
            "AAPL": "bullish",
            "NVDA": "bearish",
            "XOM": "unknown",
        }
        assert tape.text.startswith("VIX complex (Cboe ~15-min delayed")
        assert "AAPL 100.00" in tape.text and len(tape.text) <= 1500

    def test_tape_stale_and_absent(self, conn) -> None:
        from arc.ingest.cboe_fast import scalp_tape_from_store

        m30 = dt.timedelta(minutes=30)
        none = scalp_tape_from_store(conn, NOW, m30)
        assert none.text == "Options tape: no fresh info (age none stored)"
        assert not none.present and none.tickers == {}
        _store_tape(conn, NOW, {"AAPL": 0.5})
        late = scalp_tape_from_store(conn, NOW + dt.timedelta(minutes=31), m30)
        assert late.text == "Options tape: no fresh info (age 31m)"
        assert late.tickers == {}  # stale tape never corroborates
        evening = scalp_tape_from_store(conn, NOW + dt.timedelta(minutes=50), m30)
        assert evening.text == "Options tape: no fresh info (age 50m)"
        two_h = scalp_tape_from_store(conn, NOW + dt.timedelta(minutes=59), m30)
        assert two_h.text.endswith("(age 59m)")

    def test_age_text(self) -> None:
        from arc.ingest.cboe_fast import _age_text  # noqa: PLC2701

        assert _age_text("nope", NOW) == "unreadable"
        assert _age_text((NOW + dt.timedelta(minutes=5)).isoformat(), NOW) == "in the future"
        assert _age_text((NOW - dt.timedelta(minutes=125)).isoformat(), NOW) == "2h05m"
        assert _age_text("2026-09-28T06:00:00", NOW) == "1h00m"  # naive = ET

    def test_flag_off_prompt_unchanged_and_no_tape(self, conn, settings) -> None:
        _seed(conn, 1)
        _store_tape(conn, NOW, {"AAPL": 0.5})
        llm = FixtureScalpLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        res = run_scalp(conn, settings, llm=llm, now=NOW, routines=_tape_routines(False))
        assert res.tape is None and not res.tape_present and res.tape_tickers == 0
        assert "Options tape" not in llm.prompts[0]
        assert res.candidates[0].sources == ["https://example.com/aapl/0"]
        assert res.tape_corroborated == {}

    def test_flag_on_tape_in_prompt_and_corroboration(self, conn, settings) -> None:
        from arc.ingest.cboe_fast import TAPE_SOURCE

        _seed(conn, 2)
        RawDocRepo(conn).insert(
            source="rss",
            url="https://example.com/nvda/0",
            published_at=(NOW - dt.timedelta(hours=1)).isoformat(),
            text="NVDA guidance cut on export rules",
            tickers_hint=["NVDA"],
            id="doc-nvda",
        )
        _store_tape(conn, NOW - dt.timedelta(minutes=5), {"AAPL": 0.5, "NVDA": 0.5})
        llm = FixtureScalpLLM(
            [
                _reply(
                    _item(sources=["https://example.com/aapl/0"]),  # bullish + P/C 0.5
                    _item(
                        ticker="NVDA", stance="bearish", sources=["https://example.com/nvda/0"]
                    ),  # bearish vs a bullish tape: no corroboration
                )
            ]
        )
        res = run_scalp(
            conn, settings, llm=llm, now=NOW, run_id="r-tape", routines=_tape_routines(True)
        )
        prompt = llm.prompts[0]
        assert "## Options tape (Cboe, code-built)" in prompt
        assert "never cite it in `sources`" in prompt
        assert res.tape_present and res.tape_tickers == 2
        assert res.tape_corroborated == {"AAPL": 0.5}
        by = {c.ticker: c for c in res.candidates}
        assert by["AAPL"].sources == ["https://example.com/aapl/0", TAPE_SOURCE]
        assert by["AAPL"].corroboration == 2  # one doc source + the tape
        assert TAPE_SOURCE not in by["NVDA"].sources and by["NVDA"].corroboration == 1
        assert "AAPL" in res.candidate_ttls  # the tape token never sets a freshness TTL
        # the tape read is recorded as a context snapshot for audit
        kinds = conn.execute(
            "SELECT kinds FROM context_snapshots WHERE run_id = 'r-tape'"
        ).fetchall()
        assert any("index_vols" in r[0] for r in kinds)
        # the stored candidate row carries the tape token once
        stored = CandidateRepo(conn).get_for_day("AAPL", DAY)
        assert json.loads(stored["sources"]).count(TAPE_SOURCE) == 1

    def test_flag_on_stale_tape_line(self, conn, settings) -> None:
        _seed(conn, 1)
        _store_tape(conn, NOW - dt.timedelta(minutes=45), {"AAPL": 0.5})
        llm = FixtureScalpLLM([_reply(_item(sources=["https://example.com/aapl/0"]))])
        res = run_scalp(conn, settings, llm=llm, now=NOW, routines=_tape_routines(True))
        assert "Options tape: no fresh info (age 45m)" in llm.prompts[0]
        assert not res.tape_present and res.tape_corroborated == {}
        assert res.candidates[0].corroboration == 1

    def test_tape_never_creates_or_removes(self, conn, settings) -> None:
        _seed(conn, 1)
        _store_tape(conn, NOW, {"AAPL": 0.5, "NVDA": 0.2, "TSLA": 2.0})
        llm = FixtureScalpLLM([_reply()])  # the LLM proposes nothing
        res = run_scalp(conn, settings, llm=llm, now=NOW, routines=_tape_routines(True))
        assert res.candidates == [] and res.accepted == 0 and res.tape_corroborated == {}

    def test_two_category_budget_tape_costs_no_docs(self, conn, settings) -> None:
        """D56: the doc budget splits over market_news + company_data only; the tape
        adds no docs and draws nothing from the budget."""
        settings.scalp_doc_budget = 4
        repo = RawDocRepo(conn)
        for i in range(6):
            repo.insert(
                source="rss",
                url=f"https://www.cnbc.com/b/{i}",  # cnbc_business: market_news
                published_at=(NOW - dt.timedelta(minutes=10 + i)).isoformat(),
                text=f"market story {i} alpha{i} beta{i}",
                tickers_hint=["AAPL"],
                id=f"mn-{i}",
                source_key="rss:cnbc_business",
            )
            repo.insert(
                source="rss",
                url=f"https://www.cnbc.com/e/{i}",  # cnbc_earnings: company_data
                published_at=(NOW - dt.timedelta(minutes=10 + i)).isoformat(),
                text=f"earnings story {i} gamma{i} delta{i}",
                tickers_hint=["AAPL"],
                id=f"cd-{i}",
                source_key="rss:cnbc_earnings",
            )
        _store_tape(conn, NOW, {"AAPL": 0.5})
        off = run_scalp(
            conn,
            settings,
            llm=FixtureScalpLLM([_reply()]),
            now=NOW,
            routines=_tape_routines(False),
            run_id="off",
        )
        cats = {str(m.category) for m in off.category_mix}
        assert cats <= {"market_news", "company_data"}  # no options category drawn
        assert off.docs_scalped == 4 and off.over_budget == 8
        on = run_scalp(
            conn,
            settings,
            llm=FixtureScalpLLM([_reply()]),
            now=NOW,
            routines=_tape_routines(True),
            run_id="on",
        )
        assert on.tape_present and on.docs_scalped == 4  # same doc budget with the tape

    def test_out_of_tier_mentions(self, tmp_path) -> None:
        """D56: an out-of-tier idea -> not_in_tier journal row + a mention, never a
        candidate; the note and the card list it under Outside the universe."""
        from arc.context.store import ContextStore
        from arc.routines.config import RoutinesConfig
        from arc.routines.handlers import JobContext, scalp_persona
        from arc.universe.config import universe_config
        from arc.universe.guard import UniverseGuard, UniverseMode
        from arc.universe.master import SymbolInfo, SymbolMaster
        from arc.universe.tiers import Tier

        db = connect(tmp_path / "arc.db")
        migrate(db)
        repo = RawDocRepo(db)
        repo.insert(
            source="rss",
            url=URL_A,
            published_at=(NOW - dt.timedelta(minutes=30)).isoformat(),
            text="AAPL and OUTX and ZZZ news",
            tickers_hint=["AAPL", "OUTX"],
            id="doc-a",
            title="OUTX soars on a buyout rumour",
        )
        s = ArcSettings(env="paper", scalp_min_confidence=0.6)  # type: ignore[call-arg]
        syms = ["AAPL", "OUTX", *(f"OT{i}" for i in range(12))]
        guard = UniverseGuard(
            mode=UniverseMode.SEED,
            seed=frozenset({"AAPL"}),
            config=universe_config(s),
            master=SymbolMaster(
                fetched_at=NOW,
                symbols={
                    x: SymbolInfo(symbol=x, sources=["sec"], options=True, tradable=True)
                    for x in syms
                },
            ),
            max_new=3,
            today=NOW.date(),
            dte_window=(30, 60),
            tiers={"AAPL": Tier.CORE},
            model="d56",
            floors={Tier.CORE: 0.4},
        )
        items = [
            _item(sources=[URL_A]),
            _item(ticker="OUTX", stance="bullish", catalyst_type="news", sources=[URL_A]),
            _item(ticker="OUTX", stance="bearish", sources=[URL_A]),  # dedupe by ticker
            *(_item(ticker=f"OT{i}", stance="bearish", sources=[URL_A]) for i in range(12)),
        ]

        class _LLM:
            model = "test"

            def complete(self, _prompt: str) -> LLMResult:
                return LLMResult(text=_reply(*items), model="test")

        routines = RoutinesConfig.model_validate(
            {"personas": {"scalp": {"schedule": ["07:00"], "writes": ["candidate", "note"]}}}
        )
        kind, spec = routines.step("scalp")
        ctx = JobContext(
            job="scalp",
            kind=kind,
            spec=spec,
            run_id="run-m",
            chain_run_id="chain-m",
            scheduled_for=NOW,
            now=NOW,
            conn=db,
            snapshot=ContextStore(db).snapshot(NOW),
            routines=routines,
            settings_factory=lambda: s,
        )
        res = scalp_persona(ctx, llm=_LLM(), guard=guard)

        # never a candidate
        stored = {r[0] for r in db.execute("SELECT ticker FROM candidates").fetchall()}
        assert stored == {"AAPL"}
        cands = db.execute(
            "SELECT subject FROM context_entries WHERE kind = 'candidate'"
        ).fetchall()
        assert [r[0] for r in cands] == ["AAPL"]
        # journaled not_in_tier (every rejected name, once per run)
        rows = db.execute(
            "SELECT subject FROM decisions WHERE reason_code = 'universe:not_in_tier'"
        ).fetchall()
        assert {r[0] for r in rows} == {"OUTX", *(f"OT{i}" for i in range(12))}
        # mentions: first idea per ticker, at most 10, the story headline kept
        assert res.metrics["mentions"] == 10
        note = db.execute("SELECT payload FROM context_entries WHERE kind = 'note'").fetchone()
        payload = json.loads(note[0])
        assert payload["facts"] == {"mentions": 10}
        line = payload["body"].splitlines()[-1]
        assert line.startswith("Outside the universe: OUTX (bullish, news), OT0 (bearish, ")
        assert len(line) <= 300
        assert "Options tape" not in payload["body"]  # flag off: no tape line
        card = json.dumps(res.card.blocks if res.card else [])
        assert "Outside the universe (10): mentioned, not admitted" in card
        assert "OUTX (bullish)" in card
        assert "Options tape" not in card
        db.close()

    def test_mention_headline_and_line_cap(self) -> None:
        from arc.ingest.scalp import ScalpMention, ScalpRunResult, _mention
        from arc.routines.handlers import scalp_mentions_line

        assert _mention({"ticker": "X"}, []) is None  # schema-invalid idea: no mention
        res = ScalpRunResult(run_id="r", day=DAY, dry_run=False)
        assert scalp_mentions_line(res) == ""
        res.mentions = [
            ScalpMention(
                ticker=f"LONGNAME{i}",
                stance=Stance.BULLISH,
                catalyst_type=CatalystType.EARNINGS,
                headline="h",
            )
            for i in range(10)
        ]
        line = scalp_mentions_line(res)
        assert len(line) <= 300 and line.endswith("…")
        assert "LONGNAME0 (bullish, earnings)" in line and line.endswith("), …")

    def test_tape_line_and_card(self) -> None:
        from arc.ingest.cboe_fast import ScalpTape, TapeTicker
        from arc.ingest.scalp import ScalpRunResult
        from arc.routines.handlers import scalp_tape_line
        from arc.slack.digests import scalp_card

        res = ScalpRunResult(run_id="r", day=DAY, dry_run=False)
        assert scalp_tape_line(res) == ""
        res.tape = ScalpTape(
            as_of=NOW.isoformat(),
            vix=17.6,
            vix9d=16.5,
            vxn=None,
            flags=[],
            tickers={
                "AAPL": TapeTicker(
                    pc_volume=0.5, atm_spread_pct=None, atm_oi=None, direction="bullish"
                )
            },
            text="VIX complex ...",
        )
        res.tape_corroborated = {"AAPL": 0.5}
        line = scalp_tape_line(res)
        assert line == "Options tape: VIX 17.6 · 9D/30D 0.94 · 1 ticker · corroborated AAPL"
        res.tape = res.tape.model_copy(update={"vix9d": None, "vix": None})
        assert scalp_tape_line(res) == res.tape.text  # not present: the stale line
        card = scalp_card(
            docs=1, accepted=0, candidates=[], rejected={}, tape_line=line, mentions=["OUTX"]
        )
        text = json.dumps(card.blocks)
        assert "Options tape: VIX 17.6" in text and "Outside the universe (1)" in text
