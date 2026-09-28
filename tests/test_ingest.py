"""Tests for arc.ingest connectors (E4.1).

Tests use in-memory SQLite and mock all network calls to avoid
hitting real APIs during CI.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from unittest import mock

import pytest

from arc.config import DEFAULT_YOUTUBE_CHANNELS, ArcSettings
from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.store.migrate import migrate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory DB with all migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


@pytest.fixture()
def settings() -> ArcSettings:
    """Test settings with ingest config."""
    return ArcSettings(
        env="paper",
        ingest_rss_feeds=["https://example.com/feed.xml"],
        edgar_user_agent="TestArc/0.1 (test@example.com)",
        finnhub_api_key="test_key_123",
        ingest_youtube_channels=["https://www.youtube.com/@TestChannel"],
        universe=["AAPL", "MSFT", "NVDA"],
    )


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic(self) -> None:
        h1 = content_hash("rss", "https://example.com/1")
        h2 = content_hash("rss", "https://example.com/1")
        assert h1 == h2

    def test_differs_by_source(self) -> None:
        h1 = content_hash("rss", "https://example.com/1")
        h2 = content_hash("edgar", "https://example.com/1")
        assert h1 != h2

    def test_differs_by_url(self) -> None:
        h1 = content_hash("rss", "https://example.com/1")
        h2 = content_hash("rss", "https://example.com/2")
        assert h1 != h2


class TestRawDocRepo:
    def test_insert_and_get(self, db: sqlite3.Connection) -> None:
        repo = RawDocRepo(db)
        doc_id = repo.insert(
            source="rss",
            url="https://example.com/article",
            published_at="2026-01-01T00:00:00Z",
            text="Test article",
            tickers_hint=["AAPL"],
        )
        assert doc_id is not None
        doc = repo.get(doc_id)
        assert doc is not None
        assert doc["source"] == "rss"
        assert doc["url"] == "https://example.com/article"

    def test_dedupe_skips_duplicate(self, db: sqlite3.Connection) -> None:
        repo = RawDocRepo(db)
        id1 = repo.insert(
            source="rss",
            url="https://example.com/article",
            published_at="2026-01-01T00:00:00Z",
            text="Test",
        )
        id2 = repo.insert(
            source="rss",
            url="https://example.com/article",
            published_at="2026-01-01T00:00:00Z",
            text="Test duplicate",
        )
        assert id1 is not None
        assert id2 is None  # duplicate

    def test_list_by_source(self, db: sqlite3.Connection) -> None:
        repo = RawDocRepo(db)
        ts1 = "2026-01-01T00:00:00Z"
        ts2 = "2026-01-02T00:00:00Z"
        repo.insert(source="rss", url="https://a.com/1", published_at=ts1, text="A")
        repo.insert(source="edgar", url="https://b.com/1", published_at=ts1, text="B")
        repo.insert(source="rss", url="https://a.com/2", published_at=ts2, text="C")

        rss_docs = repo.list_by_source("rss")
        assert len(rss_docs) == 2
        edgar_docs = repo.list_by_source("edgar")
        assert len(edgar_docs) == 1


class TestIngestCursorRepo:
    def test_set_and_get(self, db: sqlite3.Connection) -> None:
        repo = IngestCursorRepo(db)
        assert repo.get("rss:feed1") is None
        repo.set("rss:feed1", "2026-01-01T00:00:00Z")
        assert repo.get("rss:feed1") == "2026-01-01T00:00:00Z"

    def test_upsert(self, db: sqlite3.Connection) -> None:
        repo = IngestCursorRepo(db)
        repo.set("rss:feed1", "2026-01-01T00:00:00Z")
        repo.set("rss:feed1", "2026-02-01T00:00:00Z")
        assert repo.get("rss:feed1") == "2026-02-01T00:00:00Z"


# ---------------------------------------------------------------------------
# RSS connector tests
# ---------------------------------------------------------------------------


def _make_rss_response(entries: list[dict]) -> str:
    """Build a minimal RSS XML string."""
    items = ""
    for e in entries:
        items += f"""
        <item>
            <title>{e.get("title", "Test")}</title>
            <link>{e["link"]}</link>
            <description>{e.get("description", "Test description")}</description>
            <pubDate>{e.get("pubDate", "Mon, 01 Jan 2026 00:00:00 GMT")}</pubDate>
        </item>"""
    return f"""<?xml version="1.0"?>
    <rss version="2.0">
        <channel>
            <title>Test Feed</title>
            {items}
        </channel>
    </rss>"""


class TestRSSConnector:
    def test_fetches_new_entries(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        import feedparser as fp

        from arc.ingest.rss import fetch_rss

        xml = _make_rss_response(
            [
                {
                    "link": "https://example.com/1",
                    "pubDate": "Wed, 01 Jan 2026 12:00:00 GMT",
                    "description": "AAPL is up today",
                },
                {
                    "link": "https://example.com/2",
                    "pubDate": "Thu, 02 Jan 2026 12:00:00 GMT",
                    "description": "Market news",
                },
            ]
        )
        parsed = fp.parse(xml)

        with mock.patch("arc.ingest.rss.feedparser") as mock_fp:
            mock_fp.parse.return_value = parsed
            docs = fetch_rss(db, settings)

        assert len(docs) == 2
        assert docs[0].source == "rss"
        # Check ticker extraction
        aapl_doc = next((d for d in docs if "AAPL" in d.tickers_hint), None)
        assert aapl_doc is not None

    def test_incremental_skips_old(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        import feedparser as fp

        from arc.ingest.rss import fetch_rss

        xml = _make_rss_response(
            [
                {"link": "https://example.com/1", "pubDate": "Wed, 01 Jan 2026 12:00:00 GMT"},
            ]
        )
        parsed = fp.parse(xml)

        with mock.patch("arc.ingest.rss.feedparser") as mock_fp:
            mock_fp.parse.return_value = parsed
            docs1 = fetch_rss(db, settings)
            docs2 = fetch_rss(db, settings)

        assert len(docs1) == 1
        assert len(docs2) == 0  # incremental: cursor advanced past this entry

    def test_no_feeds_configured(self, db: sqlite3.Connection) -> None:
        from arc.ingest.rss import fetch_rss

        empty_settings = ArcSettings(env="paper", ingest_rss_feeds=[])
        docs = fetch_rss(db, empty_settings)
        assert docs == []


# ---------------------------------------------------------------------------
# EDGAR connector tests
# ---------------------------------------------------------------------------


class TestEdgarConnector:
    def test_fetches_filings(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        from arc.ingest.edgar import fetch_edgar

        submissions_response = json.dumps(
            {
                "filings": {
                    "recent": {
                        "form": ["8-K", "10-Q", "8-K"],
                        "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003"],
                        "filingDate": ["2026-01-15", "2026-01-10", "2026-01-05"],
                        "primaryDocument": ["doc1.htm", "doc2.htm", "doc3.htm"],
                    }
                }
            }
        ).encode()

        tickers_response = json.dumps(
            {
                "0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc"},
            }
        ).encode()

        def mock_urlopen(req, **_kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            ctx = mock.MagicMock()
            if "company_tickers" in url:
                ctx.read.return_value = tickers_response
            elif "submissions" in url:
                ctx.read.return_value = submissions_response
            else:
                ctx.read.return_value = b"<html>Filing text about AAPL earnings</html>"
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = mock.Mock(return_value=False)
            return ctx

        with (
            mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
            mock.patch("time.sleep"),
        ):
            docs = fetch_edgar(db, settings)

        # Should have fetched some filings for AAPL (8-K x2, 10-Q x1)
        assert len(docs) >= 1
        assert all(d.source == "edgar" for d in docs)

    def test_cik_lookup_failure_skips_ticker(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.edgar import fetch_edgar

        with (
            mock.patch("urllib.request.urlopen", side_effect=Exception("network error")),
            mock.patch("time.sleep"),
        ):
            docs = fetch_edgar(db, settings)

        assert docs == []


# ---------------------------------------------------------------------------
# Earnings connector tests
# ---------------------------------------------------------------------------


class TestEarningsConnector:
    def test_fetches_events(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        from arc.ingest.earnings import fetch_earnings

        api_response = json.dumps(
            {
                "earningsCalendar": [
                    {
                        "symbol": "AAPL",
                        "date": "2026-01-28",
                        "epsEstimate": 2.10,
                        "hour": "amc",
                        "revenueEstimate": 124000000000,
                    },
                    {"symbol": "MSFT", "date": "2026-01-29", "epsEstimate": 3.20, "hour": "bmo"},
                    {"symbol": "UNKNOWN", "date": "2026-01-30"},  # not in universe
                ]
            }
        ).encode()

        def mock_urlopen(req, **_kwargs):
            ctx = mock.MagicMock()
            ctx.read.return_value = api_response
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = mock.Mock(return_value=False)
            return ctx

        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            docs = fetch_earnings(db, settings)

        assert len(docs) == 2
        tickers = [d.tickers_hint[0] for d in docs]
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert all(d.source == "earnings" for d in docs)

    def test_no_api_key(self, db: sqlite3.Connection) -> None:
        from arc.ingest.earnings import fetch_earnings

        no_key_settings = ArcSettings(env="paper", finnhub_api_key="")
        docs = fetch_earnings(db, no_key_settings)
        assert docs == []

    def test_incremental(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        from arc.ingest.earnings import fetch_earnings

        api_response = json.dumps(
            {
                "earningsCalendar": [
                    {"symbol": "AAPL", "date": "2026-01-28", "epsEstimate": 2.10},
                ]
            }
        ).encode()

        def mock_urlopen(req, **_kwargs):
            ctx = mock.MagicMock()
            ctx.read.return_value = api_response
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = mock.Mock(return_value=False)
            return ctx

        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            docs1 = fetch_earnings(db, settings)
            docs2 = fetch_earnings(db, settings)

        assert len(docs1) == 1
        assert len(docs2) == 0  # deduplicated


# ---------------------------------------------------------------------------
# YouTube connector tests
# ---------------------------------------------------------------------------


def _yt_runner(listing: list[dict], infos: dict[str, dict]):
    """Fake ``subprocess.run`` for yt-dlp: flat listing + per-video info JSON."""

    def run(cmd, **_kwargs):
        result = mock.MagicMock()
        result.stderr = ""
        if "--flat-playlist" in cmd:
            result.returncode = 0
            result.stdout = "\n".join(json.dumps(v) for v in listing)
        elif "--dump-single-json" in cmd:
            vid = cmd[-1].rsplit("=", 1)[-1]
            result.returncode = 0 if vid in infos else 1
            result.stdout = json.dumps(infos.get(vid, {}))
        else:
            result.returncode = 1
            result.stdout = ""
        return result

    return run


def _info(vid: str, title: str, date: str, *, captions: bool = True) -> dict:
    auto = {"en": [{"ext": "vtt", "url": f"https://captions.test/{vid}.vtt"}]} if captions else {}
    return {
        "id": vid,
        "title": title,
        "upload_date": date,
        "timestamp": int(datetime.strptime(date, "%Y%m%d").replace(tzinfo=UTC).timestamp()),
        "channel": "StockedUp",
        "subtitles": {},
        "automatic_captions": auto,
    }


class TestYouTubeConnector:
    def test_fetches_auto_captions(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        from arc.ingest.youtube import fetch_youtube

        run = _yt_runner(
            [{"id": "abc123", "title": "AAPL Analysis"}],
            {"abc123": _info("abc123", "AAPL Analysis 2026", "20260115")},
        )
        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch(
                "arc.ingest.youtube._download_subtitle",
                return_value="AAPL is testing support at 180",
            ) as dl,
        ):
            docs = fetch_youtube(db, settings)

        dl.assert_called_once_with("https://captions.test/abc123.vtt")
        assert len(docs) == 1
        assert docs[0].source == "youtube"
        assert docs[0].url == "https://www.youtube.com/watch?v=abc123"
        assert docs[0].text.startswith(
            "[transcript:captions] [StockedUp] [AAPL Analysis 2026] AAPL is testing"
        )
        assert docs[0].transcript_source == "captions"
        assert "AAPL" in docs[0].tickers_hint
        assert docs[0].published_at == datetime(2026, 1, 15, tzinfo=UTC)

    def test_manual_subs_preferred(self) -> None:
        from arc.ingest.youtube import _pick_caption_url

        info = {
            "subtitles": {"en": [{"ext": "vtt", "url": "manual"}]},
            "automatic_captions": {"en": [{"ext": "vtt", "url": "auto"}]},
        }
        assert _pick_caption_url(info) == "manual"
        assert _pick_caption_url({"automatic_captions": {"en": [{"ext": "json3"}]}}) == ""

    def test_no_channels_configured(self, db: sqlite3.Connection) -> None:
        from arc.ingest.youtube import fetch_youtube

        empty_settings = ArcSettings(env="paper", ingest_youtube_channels=[])
        docs = fetch_youtube(db, empty_settings)
        assert docs == []

    def test_dedupes_and_advances_cursor(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.youtube import fetch_youtube

        run = _yt_runner(
            [{"id": "abc123", "title": "Test Video"}],
            {"abc123": _info("abc123", "Test Video", "20260115")},
        )
        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("arc.ingest.youtube._download_subtitle", return_value="words"),
        ):
            docs1 = fetch_youtube(db, settings)
            docs2 = fetch_youtube(db, settings)

        assert len(docs1) == 1
        assert len(docs2) == 0
        cursor = IngestCursorRepo(db).get("youtube:https://www.youtube.com/@TestChannel")
        assert cursor == "20260115"

    def test_video_without_captions_is_retried(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        """Auto-captions lag uploads; a caption-less video must not be deduped forever."""
        from arc.ingest.youtube import fetch_youtube

        listing = [{"id": "new1", "title": "Fresh"}]
        pending = _yt_runner(listing, {"new1": _info("new1", "Fresh", "20260116", captions=False)})
        ready = _yt_runner(listing, {"new1": _info("new1", "Fresh", "20260116")})

        with mock.patch("subprocess.run", side_effect=pending):
            assert fetch_youtube(db, settings) == []
        with (
            mock.patch("subprocess.run", side_effect=ready),
            mock.patch("arc.ingest.youtube._download_subtitle", return_value="now captioned"),
        ):
            docs = fetch_youtube(db, settings)
        assert [d.url for d in docs] == ["https://www.youtube.com/watch?v=new1"]


# ---------------------------------------------------------------------------
# RawDoc model tests
# ---------------------------------------------------------------------------


class TestRawDocModel:
    def test_create(self) -> None:
        from arc.models import RawDoc

        doc = RawDoc(
            source="rss",
            url="https://example.com/1",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            text="Test text",
            tickers_hint=["AAPL", "MSFT"],
            content_hash="abc123",
        )
        assert doc.source == "rss"
        assert doc.tickers_hint == ["AAPL", "MSFT"]

    def test_defaults(self) -> None:
        from arc.models import RawDoc

        doc = RawDoc(
            source="edgar",
            url="https://sec.gov/filing",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            text="Filing text",
        )
        assert doc.tickers_hint == []
        assert doc.content_hash == ""


# ---------------------------------------------------------------------------
# Config ingest fields tests
# ---------------------------------------------------------------------------


class TestIngestConfig:
    def test_default_empty(self) -> None:
        s = ArcSettings(env="paper")
        assert s.ingest_rss_feeds == []
        assert s.ingest_youtube_channels == DEFAULT_YOUTUBE_CHANNELS
        assert s.finnhub_api_key == ""
        assert "ProjectArc" in s.edgar_user_agent

    def test_csv_parsing(self) -> None:
        s = ArcSettings(
            env="paper",
            ingest_rss_feeds="https://a.com/feed,https://b.com/feed",
            ingest_youtube_channels="https://youtube.com/@A,https://youtube.com/@B",
        )
        assert len(s.ingest_rss_feeds) == 2
        assert len(s.ingest_youtube_channels) == 2

    def test_custom_values(self) -> None:
        s = ArcSettings(
            env="paper",
            edgar_user_agent="MyApp/1.0 (me@me.com)",
            finnhub_api_key="key123",
        )
        assert s.edgar_user_agent == "MyApp/1.0 (me@me.com)"
        assert s.finnhub_api_key == "key123"
