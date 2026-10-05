"""Built-in routine handlers (E5.4): sources write raw_doc_ref, Sweep writes candidates."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.ingest.store import RawDocRepo, content_hash
from arc.ingest.sweep import SweepRunResult
from arc.models import Candidate, RawDoc
from arc.routines.config import JobKind, RoutinesConfig
from arc.routines.handlers import (
    JobContext,
    earnings_source,
    edgar_source,
    rss_source,
    sweep_persona,
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
        {
            "sources": {
                job: {
                    "every": "5m",
                    "writes": ["raw_doc_ref"],
                    "category": "market_news",  # D47: every context source declares one
                    **(options or {}),
                }
            }
        }
        if job != "sweep"
        else {"personas": {"sweep": {"schedule": ["12:00"], "writes": ["candidate", "note"]}}}
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
        ("edgar", edgar_source, "arc.ingest.edgar.fetch_edgar", {"tickers": ["aapl"]},
         None, None),
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


def _open_structure(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,"
        " structure_json, contracts, entry_net, opened_at, status)"
        " VALUES (?, ?, ?, 'c', '{}', 1, '1.0', '2026-09-20', 'open')",
        (f"os-{ticker}", ticker, f"h-{ticker}"),
    )
    conn.commit()


def test_edgar_scope_is_active_list_plus_open_underlyings(conn: sqlite3.Connection) -> None:
    """E12.4: EDGAR = active list + open underlyings; a `tickers` option replaces it."""
    _open_structure(conn, "ZZOP")
    with mock.patch("arc.ingest.edgar.fetch_edgar", return_value=[]) as fetch:
        edgar_source(_ctx(conn, "edgar"))
    assert fetch.call_args.kwargs["tickers"] is None  # resolved inside fetch_edgar
    with mock.patch("arc.ingest.edgar.fetch_edgar", return_value=[]) as fetch:
        edgar_source(_ctx(conn, "edgar", {"tickers": ["aapl"]}))
    assert fetch.call_args.kwargs["tickers"] == ["AAPL"]

    from arc.ingest import edgar

    seen: list[str] = []
    uni = mock.Mock(seed=("NVDA", "AAPL"))
    uni.cik.side_effect = lambda t: seen.append(t)  # no CIK: the walk skips every ticker
    with (
        mock.patch.object(edgar.IngestUniverse, "from_settings", return_value=uni),
        mock.patch.object(edgar, "_company_tickers", return_value={}),
    ):
        edgar.fetch_edgar(conn, ArcSettings(), now=NOW)
    assert seen[:3] == ["NVDA", "AAPL", "ZZOP"]


def test_data_tickers_include_open_underlyings(conn: sqlite3.Connection) -> None:
    from arc.routines.handlers import _data_tickers

    _open_structure(conn, "ZZOP")
    got = _data_tickers(_ctx(conn, "unusual_options", {"tickers": ["nvda"]}))
    assert got == ["NVDA", "ZZOP"]


def test_source_without_options_uses_settings(conn: sqlite3.Connection) -> None:
    ctx = _ctx(conn, "rss")
    with mock.patch("arc.ingest.rss.fetch_rss", return_value=[]) as fetch:
        result = rss_source(ctx)
    assert fetch.call_args.args[1] is ctx.settings
    assert result.summary == "0 new docs"


def test_sweep_persona_writes_candidates_and_metrics(conn: sqlite3.Connection) -> None:
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
    result = SweepRunResult(
        run_id="run-1",
        day="2026-09-28",
        dry_run=False,
        batches=1,
        docs_swept=4,
        accepted=1,
        candidates=[cand],
        failed_batches=1,
    )
    ctx = _ctx(conn, "sweep")
    assert ctx.kind is JobKind.PERSONA
    with mock.patch("arc.ingest.sweep.run_sweep", return_value=result) as run:
        out = sweep_persona(ctx)
    kwargs = run.call_args.kwargs
    assert kwargs["now"] == NOW and kwargs["run_id"] == "run-1"
    assert kwargs["routines"] is ctx.routines  # D30: the registry comes from the same config
    assert "on_story" not in kwargs  # this sweep does not declare `writes: [story]`
    assert out.metrics["new_candidates"] == 1
    assert "1 failed batches" in out.summary
    entries = ContextStore(conn).query(as_of=NOW, kinds=["candidate"])
    assert [e.subject for e in entries] == ["SPY"]
    assert entries[0].produced_by == "sweep"


def test_settings_default_factory(conn: sqlite3.Connection) -> None:
    """No factory: the D26 effective settings for this DB, computed once per run."""
    ctx = _ctx(conn, "rss")
    ctx.settings_factory = None
    with mock.patch("arc.control.effective.effective_settings", return_value=ArcSettings()) as eff:
        assert ctx.settings is ctx.settings
    eff.assert_called_once_with(conn)
