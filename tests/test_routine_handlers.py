"""Built-in routine handlers (E5.4): sources write raw_doc_ref, Scout writes candidates."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.ingest.scout import ScoutRunResult
from arc.ingest.store import RawDocRepo, content_hash
from arc.models import Candidate, RawDoc
from arc.routines.config import JobKind, RoutinesConfig
from arc.routines.handlers import (
    JobContext,
    earnings_source,
    edgar_source,
    rss_source,
    scout_persona,
    youtube_source,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _ctx(
    conn: sqlite3.Connection, job: str, options: dict[str, object] | None = None
) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"sources": {job: {"every": "5m", "writes": ["raw_doc_ref"], **(options or {})}}}
        if job != "scout"
        else {"personas": {"scout": {"schedule": ["12:00"], "writes": ["candidate", "note"]}}}
    )
    kind, spec = routines.step(job)
    return JobContext(
        job=job,
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines,
        settings_factory=lambda: ArcSettings(),
    )


def _stored_doc(conn: sqlite3.Connection, source: str, url: str) -> RawDoc:
    doc = RawDoc(
        source=source,
        url=url,
        published_at=NOW,
        text="hello",
        tickers_hint=["SPY"],
        content_hash=content_hash(source, url),
    )
    RawDocRepo(conn).insert(
        source=source,
        url=url,
        published_at=NOW.isoformat(),
        text="hello",
        hash_val=doc.content_hash,
    )
    return doc


@pytest.mark.parametrize(
    ("job", "fn", "target", "options", "setting", "expected"),
    [
        ("rss", rss_source, "arc.ingest.rss.fetch_rss", {"feeds": ["https://f/x"]},
         "ingest_rss_feeds", ["https://f/x"]),
        ("edgar", edgar_source, "arc.ingest.edgar.fetch_edgar", {"tickers": ["AAPL"]},
         "universe", ["AAPL"]),
        ("youtube.stockedup", youtube_source, "arc.ingest.youtube.fetch_youtube",
         {"channel": "UCabc"}, "ingest_youtube_channels",
         ["https://www.youtube.com/channel/UCabc/videos"]),
        ("earnings", earnings_source, "arc.ingest.earnings.fetch_earnings", {}, None, None),
    ],
)  # fmt: skip
def test_source_handlers_write_doc_refs(
    conn: sqlite3.Connection,
    job: str,
    fn: object,
    target: str,
    options: dict[str, object],
    setting: str | None,
    expected: list[str] | None,
) -> None:
    source = job.split(".")[0]
    docs = [_stored_doc(conn, source, f"https://x/{i}") for i in range(2)]
    ctx = _ctx(conn, job, options)
    with mock.patch(target, return_value=docs) as fetch:
        result = fn(ctx)  # type: ignore[operator]
    assert result.metrics["new_docs"] == 2
    assert result.summary.startswith("2 new docs")
    if setting is not None:
        assert getattr(fetch.call_args.args[1], setting) == expected
    refs = ContextStore(conn).query(as_of=NOW, kinds=["raw_doc_ref"])
    assert len(refs) == 2  # one subject per doc, so refs never supersede each other
    assert {r.payload["url"] for r in refs} == {"https://x/0", "https://x/1"}
    assert {r.subject for r in refs} == {r.payload["doc_id"] for r in refs}
    assert all(r.produced_by == job and r.run_id == "run-1" for r in refs)
    assert len(ctx.outputs) == 2


def test_source_without_options_uses_settings(conn: sqlite3.Connection) -> None:
    ctx = _ctx(conn, "rss")
    with mock.patch("arc.ingest.rss.fetch_rss", return_value=[]) as fetch:
        result = rss_source(ctx)
    assert fetch.call_args.args[1] is ctx.settings
    assert result.summary == "0 new docs"


def test_scout_persona_writes_candidates_and_metrics(conn: sqlite3.Connection) -> None:
    cand = Candidate.model_validate(
        {
            "ticker": "SPY",
            "stance": "bullish",
            "catalyst_type": "news",
            "catalyst_date": None,
            "confidence": 0.7,
            "sources": ["https://x/0"],
            "created_at": NOW,
        }
    )
    result = ScoutRunResult(
        run_id="run-1",
        day="2026-09-28",
        dry_run=False,
        batches=1,
        docs_scouted=4,
        accepted=1,
        candidates=[cand],
        failed_batches=1,
    )
    ctx = _ctx(conn, "scout")
    assert ctx.kind is JobKind.PERSONA
    with mock.patch("arc.ingest.scout.run_scout", return_value=result) as run:
        out = scout_persona(ctx)
    assert run.call_args.kwargs == {"now": NOW, "run_id": "run-1"}
    assert out.metrics["new_candidates"] == 1
    assert "1 failed batches" in out.summary
    entries = ContextStore(conn).query(as_of=NOW, kinds=["candidate"])
    assert [e.subject for e in entries] == ["SPY"]
    assert entries[0].produced_by == "scout"


def test_settings_default_factory(conn: sqlite3.Connection) -> None:
    ctx = _ctx(conn, "rss")
    ctx.settings_factory = None
    with mock.patch("arc.config.get_settings", return_value=ArcSettings()) as gs:
        assert ctx.settings is ctx.settings
    gs.assert_called_once()
