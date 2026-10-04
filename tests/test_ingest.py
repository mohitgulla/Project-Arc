"""Tests for arc.ingest connectors (E4.1).

Tests use in-memory SQLite and mock all network calls to avoid
hitting real APIs during CI.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from typing import TYPE_CHECKING
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from structlog.testing import capture_logs

from arc.config import DEFAULT_YOUTUBE_CHANNELS, ArcSettings
from arc.ingest.caption_backoff import CaptionResult, CaptionStatus
from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.store.migrate import migrate

if TYPE_CHECKING:
    from pathlib import Path

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
        yt_caption_sleep_seconds=0,
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

        with (
            mock.patch("arc.ingest.rss.feedparser") as mock_fp,
            mock.patch("arc.ingest.rss._download", return_value=b""),
        ):
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

        with (
            mock.patch("arc.ingest.rss.feedparser") as mock_fp,
            mock.patch("arc.ingest.rss._download", return_value=b""),
        ):
            mock_fp.parse.return_value = parsed
            docs1 = fetch_rss(db, settings)
            docs2 = fetch_rss(db, settings)

        assert len(docs1) == 1
        assert len(docs2) == 0  # incremental: cursor advanced past this entry

    def test_dead_feed_is_skipped_not_fatal(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        import requests

        from arc.ingest.rss import fetch_rss

        good = _make_rss_response(
            [{"link": "https://example.com/ok", "pubDate": "Wed, 01 Jan 2026 12:00:00 GMT"}]
        ).encode()
        two = settings.model_copy(
            update={"ingest_rss_feeds": ["https://dead.example/feed", "https://ok.example/feed"]}
        )

        def fake_get(url: str, timeout: float, headers: dict[str, str]) -> mock.Mock:
            assert timeout == two.ingest_rss_timeout_seconds
            assert "Mozilla/5.0" in headers["User-Agent"]
            if "dead" in url:
                raise requests.ConnectionError("connection reset by peer")
            return mock.Mock(content=good, raise_for_status=lambda: None)

        with mock.patch("arc.ingest.rss.requests.get", side_effect=fake_get):
            docs = fetch_rss(db, two)

        assert [d.url for d in docs] == ["https://example.com/ok"]

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


TODAY = date(2026, 10, 5)


def _no_sleep(_s: float) -> None:
    return None


class _FakeFinnhub:
    """``get_json`` double: answers each (from, to) call from *responder*; records calls."""

    def __init__(self, responder) -> None:  # noqa: ANN001
        self.responder = responder
        self.calls: list[tuple[date, date]] = []

    def __call__(self, url: str, _timeout: float):  # noqa: ANN204
        q = parse_qs(urlparse(url).query)
        start, end = date.fromisoformat(q["from"][0]), date.fromisoformat(q["to"][0])
        assert q["token"] == ["test_key_123"]
        self.calls.append((start, end))
        return self.responder(start, end)


def _events(start: date, end: date, per_day: int = 1, prefix: str = "S") -> dict:
    rows = []
    d = start
    while d <= end:
        rows += [{"symbol": f"{prefix}{i}", "date": d.isoformat()} for i in range(per_day)]
        d += timedelta(days=1)
    return {"earningsCalendar": rows}


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://finnhub.io/x", code, "err", msg, None)


class TestEarningsConnector:
    def _fetch(self, db, settings, get_json, **kw):  # noqa: ANN001, ANN202
        from arc.ingest.earnings import fetch_earnings

        return fetch_earnings(db, settings, today=TODAY, get_json=get_json, sleep=_no_sleep, **kw)

    def test_fetches_events(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        payload = {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2026-10-28",
                    "epsEstimate": 2.10,
                    "hour": "amc",
                    "revenueEstimate": 124000000000,
                },
                {"symbol": "MSFT", "date": "2026-10-29", "epsEstimate": 3.20, "hour": "bmo"},
                {"symbol": "UNKNOWN", "date": "2026-10-30"},  # not in universe
            ]
        }
        docs = self._fetch(db, settings, _FakeFinnhub(lambda s, e: payload))
        assert len(docs) == 2
        tickers = [d.tickers_hint[0] for d in docs]
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert all(d.source == "earnings" for d in docs)

    def test_missing_key_is_skipped_not_ok(self, db: sqlite3.Connection, tmp_path: Path) -> None:
        """E4.1d: no key -> the routine run is `skipped` (no_api_key), never `ok`,
        with one day-thread notice per day."""
        from arc.ingest.earnings import EarningsNoKeyError, fetch_earnings
        from arc.routines.handlers import JobSkippedError, earnings_source

        no_key = ArcSettings(env="paper", finnhub_api_key="")
        with pytest.raises(EarningsNoKeyError, match="no_api_key"):
            fetch_earnings(db, no_key)

        ctx = _earnings_ctx(db, no_key)
        with pytest.raises(JobSkippedError, match="no_api_key") as first:
            earnings_source(ctx)
        assert "no_api_key" in first.value.notice
        with pytest.raises(JobSkippedError) as again:  # same day: no second notice
            earnings_source(ctx)
        assert again.value.notice == ""
        tomorrow = _earnings_ctx(db, no_key, now=ctx.now + timedelta(days=1))
        with pytest.raises(JobSkippedError) as next_day:
            earnings_source(tomorrow)
        assert next_day.value.notice

        # Through the dispatcher, three runs over two days: every run row is `skipped`
        # with the reason (never `ok`), and one notice per day.
        rows, posts = _dispatch(no_key, tmp_path, days=2)
        assert [r["status"] for r in rows] == ["skipped", "skipped"]
        assert all(r["summary"].startswith("no_api_key") for r in rows)
        assert sum("no_api_key" in p for p in posts) == 2
        assert not any("FAILED" in p for p in posts)

    def test_http_error_is_failed_not_ok(
        self, db: sqlite3.Connection, settings: ArcSettings, tmp_path: Path
    ) -> None:
        """E4.1d: an HTTP error propagates (run `failed`, exception class in `error`)."""

        def boom(_s: date, _e: date) -> dict:
            raise _http_error(500)

        with (
            capture_logs() as logs,
            pytest.raises(urllib.error.HTTPError),
        ):
            self._fetch(db, settings, _FakeFinnhub(boom))
        failed = [e for e in logs if e["event"] == "earnings.finnhub_failed"]
        assert failed and "500" in failed[0]["error"]
        assert failed[0]["error_class"] == "HTTPError"
        assert IngestCursorRepo(db).get("earnings") is None  # nothing advanced

        def bad_json(_s: date, _e: date) -> dict:
            raise json.JSONDecodeError("bad", "x", 0)

        with pytest.raises(json.JSONDecodeError):
            self._fetch(db, settings, _FakeFinnhub(bad_json))
        with pytest.raises(ValueError, match="unexpected Finnhub payload"):
            self._fetch(db, settings, _FakeFinnhub(lambda s, e: {"error": "bad token"}))

        with mock.patch("arc.ingest.earnings._get_json", side_effect=_http_error(503)):
            rows, posts = _dispatch(settings, tmp_path)
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"].startswith("HTTPError")
        assert any("FAILED" in p and "earnings" in p for p in posts)  # the dispatcher alert

    def test_window_is_chunked_weekly(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        fake = _FakeFinnhub(lambda s, e: {})
        fake.responder = lambda s, e: {
            "earningsCalendar": [{"symbol": "AAPL", "date": s.isoformat()}]
            + [{"symbol": "AAPL", "date": e.isoformat()}]  # overlapping duplicates
            + [{"symbol": "AAPL", "date": s.isoformat()}]
        }
        with capture_logs() as logs:
            docs = self._fetch(db, settings, fake)
        start, end = TODAY - timedelta(days=7), TODAY + timedelta(days=30)
        assert fake.calls[0] == (start, start + timedelta(days=6))
        assert fake.calls[-1][1] == end
        assert all((e - s).days <= 6 for s, e in fake.calls)
        assert len(fake.calls) == 6  # 38 days in 7-day chunks
        # contiguous, no gaps, no overlaps
        for (_, prev_end), (nxt, _) in zip(fake.calls, fake.calls[1:], strict=False):
            assert nxt == prev_end + timedelta(days=1)
        assert len(docs) == len({d.url for d in docs})  # deduped on (symbol, date)
        done = next(e for e in logs if e["event"] == "earnings.done")
        assert done["calls"] == 6 and done["chunks_split"] == 0
        assert done["min_date"] == start.isoformat() and done["max_date"] == end.isoformat()
        assert done["events"] == 12

    def test_chunk_size_and_window_are_config(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.earnings import EarningsFetchConfig

        cfg = EarningsFetchConfig.from_options(
            {"chunk_days": 3, "lookback_days": 0, "horizon_days": 8, "label": "Earnings"}
        )
        fake = _FakeFinnhub(lambda s, e: {"earningsCalendar": []})
        self._fetch(db, settings, fake, cfg=cfg)
        assert fake.calls == [
            (TODAY, TODAY + timedelta(days=2)),
            (TODAY + timedelta(days=3), TODAY + timedelta(days=5)),
            (TODAY + timedelta(days=6), TODAY + timedelta(days=8)),
        ]

    def test_capped_chunk_splits_to_days(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.earnings import EarningsFetchConfig

        cfg = EarningsFetchConfig(row_cap=5, lookback_days=0, horizon_days=13)
        capped = (TODAY + timedelta(days=7), TODAY + timedelta(days=13))

        def respond(s: date, e: date) -> dict:
            if (s, e) == capped:  # the free tier keeps only the latest rows
                return _events(e, e, per_day=5, prefix="X")
            if s == e:
                day = s.isoformat()
                return {"earningsCalendar": [{"symbol": "AAPL", "date": day},
                                             {"symbol": "MSFT", "date": day}]}  # fmt: skip
            return _events(s, e, per_day=0)

        fake = _FakeFinnhub(respond)
        with capture_logs() as logs:
            docs = self._fetch(db, settings, fake, cfg=cfg)
        assert fake.calls[:2] == [(TODAY, TODAY + timedelta(days=6)), capped]
        assert fake.calls[2:] == [
            (capped[0] + timedelta(days=i), capped[0] + timedelta(days=i)) for i in range(7)
        ]
        assert len(docs) == 14  # AAPL + MSFT on each of the 7 split days
        done = next(e for e in logs if e["event"] == "earnings.done")
        assert done["chunks_split"] == 1 and done["calls"] == 9
        assert done["max_rows"] == 5

    def test_capped_single_day_fails_truncated(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.earnings import EarningsFetchConfig, EarningsFetchError

        cfg = EarningsFetchConfig(row_cap=5, lookback_days=0, horizon_days=6)
        fake = _FakeFinnhub(lambda s, e: _events(e, e, per_day=5, prefix="AAPL"))
        with pytest.raises(EarningsFetchError, match="truncated") as exc:
            self._fetch(db, settings, fake, cfg=cfg)
        assert exc.value.reason == "truncated"
        assert fake.calls[1] == (TODAY, TODAY)  # split, then the first day is still capped
        assert db.execute("SELECT COUNT(*) FROM raw_docs").fetchone()[0] == 0  # no partial ok
        assert IngestCursorRepo(db).get("earnings") is None

        # A one-day chunk at the cap is truncated straight away.
        one_day = EarningsFetchConfig(row_cap=5, chunk_days=1, lookback_days=0, horizon_days=1)
        with pytest.raises(EarningsFetchError, match="truncated"):
            self._fetch(db, settings, _FakeFinnhub(lambda s, e: _events(s, e, 5)), cfg=one_day)

    def test_429_is_failed_rate_limited(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.earnings import EarningsFetchConfig, EarningsFetchError, fetch_earnings

        cfg = EarningsFetchConfig(lookback_days=0, horizon_days=6)
        slept: list[float] = []

        def always_429(_s: date, _e: date) -> dict:
            raise _http_error(429, {"Retry-After": "7"})

        fake = _FakeFinnhub(always_429)
        with pytest.raises(EarningsFetchError, match="rate_limited") as exc:
            fetch_earnings(db, settings, cfg=cfg, today=TODAY, get_json=fake, sleep=slept.append)
        assert exc.value.reason == "rate_limited"
        assert len(fake.calls) == 2  # one retry, then failed
        assert 7.0 in slept

        # A 429 without Retry-After waits the configured default, and a retry that
        # succeeds keeps the run going.
        state = {"n": 0}

        def once_429(s: date, e: date) -> dict:
            state["n"] += 1
            if state["n"] == 1:
                raise _http_error(429)
            return {"earningsCalendar": [{"symbol": "AAPL", "date": s.isoformat()}]}

        slept.clear()
        docs = fetch_earnings(
            db, settings, cfg=cfg, today=TODAY, get_json=_FakeFinnhub(once_429), sleep=slept.append
        )
        assert len(docs) == 1
        assert 60.0 in slept

    def test_throttle_stays_under_rate_limit(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.earnings import EarningsFetchConfig, fetch_earnings

        clock = {"t": 0.0}
        slept: list[float] = []

        def sleep(s: float) -> None:
            slept.append(s)
            clock["t"] += s

        cfg = EarningsFetchConfig(rate_limit_per_min=30, lookback_days=0, horizon_days=20)
        fake = _FakeFinnhub(lambda s, e: {"earningsCalendar": []})
        fetch_earnings(
            db, settings, cfg=cfg, today=TODAY, get_json=fake, sleep=sleep,
            clock=lambda: clock["t"],
        )  # fmt: skip
        assert len(fake.calls) == 3
        assert slept == [2.0, 2.0]  # 60 / 30 per call, never faster

    def test_incremental(self, db: sqlite3.Connection, settings: ArcSettings) -> None:
        payload = {
            "earningsCalendar": [{"symbol": "AAPL", "date": "2026-10-28", "epsEstimate": 2.1}]
        }
        fake = _FakeFinnhub(lambda s, e: payload)
        docs1 = self._fetch(db, settings, fake)
        assert IngestCursorRepo(db).get("earnings") == TODAY.isoformat()
        calls = len(fake.calls)
        docs2 = self._fetch(db, settings, fake)
        assert len(docs1) == 1
        assert len(docs2) == 0  # deduplicated
        # The second run starts at the cursor (today), not today - lookback.
        assert fake.calls[calls][0] == TODAY


def _earnings_ctx(conn: sqlite3.Connection, settings: ArcSettings, now: datetime | None = None):  # noqa: ANN202
    from arc.context import ContextStore
    from arc.routines.config import RoutinesConfig
    from arc.routines.handlers import JobContext
    from arc.utils.calendar import ET

    now = now or datetime(2026, 10, 5, 6, 0, tzinfo=ET)
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "earnings": {
                    "schedule": ["06:00"],
                    "category": "company",
                    "writes": ["raw_doc_ref"],
                }
            }
        }
    )
    kind, spec = routines.step("earnings")
    return JobContext(
        job="earnings", kind=kind, spec=spec, run_id="run-e", chain_run_id=None,
        scheduled_for=now, now=now, conn=conn, snapshot=ContextStore(conn).snapshot(now),
        routines=routines, settings_factory=lambda: settings,
    )  # fmt: skip


def _dispatch(settings: ArcSettings, tmp: Path, days: int = 1) -> tuple[list, list[str]]:
    """Run the real ``earnings`` handler through the dispatcher on *days* mornings."""
    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.heartbeat import RecordingNotifier
    from arc.routines.locks import LockManager
    from arc.store.db import connect
    from arc.utils.calendar import ET

    conn = connect(":memory:")
    migrate(conn)
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "earnings": {
                    "schedule": ["06:00"],
                    "category": "company",
                    "writes": ["raw_doc_ref"],
                }
            }
        }
    )
    notifier = RecordingNotifier()
    d = Dispatcher(
        conn, routines, notifier=notifier, settings_factory=lambda: settings,
        locks=LockManager(tmp), is_halted=lambda: False,
    )  # fmt: skip
    for i in range(days):
        now = datetime(2026, 10, 5 + i, 6, 1, tzinfo=ET)
        d.run_job("earnings", now, reason="schedule", now=now)
    rows = conn.execute(
        "SELECT status, summary, error FROM routine_runs WHERE job = 'earnings' ORDER BY rowid"
    ).fetchall()
    return rows, [t for _, t in notifier.posts]


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
                return_value=CaptionResult.ok("AAPL is testing support at 180"),
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

    def test_original_asr_track_preferred_over_translation(self) -> None:
        """The ``en`` auto track is a tlang= translation that YouTube 429s; use en-orig."""
        from arc.ingest.youtube import _pick_caption_url

        info = {
            "subtitles": {},
            "automatic_captions": {
                "en": [{"ext": "vtt", "url": "https://yt/timedtext?v=x&tlang=en"}],
                "en-orig": [{"ext": "vtt", "url": "https://yt/timedtext?v=x"}],
            },
        }
        assert _pick_caption_url(info) == "https://yt/timedtext?v=x"
        only_translated = {"automatic_captions": {"en": info["automatic_captions"]["en"]}}
        assert _pick_caption_url(only_translated) == "https://yt/timedtext?v=x&tlang=en"

    def test_channel_id_and_title_stored(
        self, db: sqlite3.Connection, settings: ArcSettings
    ) -> None:
        from arc.ingest.youtube import fetch_youtube

        info = _info("cid1", "Outlook", "20260115")
        info["channel_id"] = "UC-m6zNItyoDk5lSykDlhE4Q"
        run = _yt_runner([{"id": "cid1", "title": "Outlook"}], {"cid1": info})
        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch(
                "arc.ingest.youtube._download_subtitle", return_value=CaptionResult.ok("words")
            ),
        ):
            (doc,) = fetch_youtube(db, settings)
        assert doc.channel_id == "UC-m6zNItyoDk5lSykDlhE4Q"
        row = db.execute("SELECT channel_id, title FROM raw_docs").fetchone()
        assert tuple(row) == ("UC-m6zNItyoDk5lSykDlhE4Q", "Outlook")

    def test_vtt_header_lines_stripped(self) -> None:
        from arc.ingest.youtube import _download_subtitle

        vtt = b"WEBVTT\nKind: captions\nLanguage: en\n\n00:00.000 --> 00:01.000\nHello SPY\n"
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = vtt
        with mock.patch("urllib.request.urlopen", return_value=resp):
            res = _download_subtitle("https://captions.test/x.vtt")
        assert res.status is CaptionStatus.OK
        assert res.text == "Hello SPY"

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
            mock.patch(
                "arc.ingest.youtube._download_subtitle", return_value=CaptionResult.ok("words")
            ),
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
            mock.patch(
                "arc.ingest.youtube._download_subtitle",
                return_value=CaptionResult.ok("now captioned"),
            ),
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
    def test_default_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Hermetic: a shell that sourced ~/.hermes/.env exports the real key.
        for var in ("ARC_FINNHUB_API_KEY", "ARC_INGEST_RSS_FEEDS", "ARC_INGEST_YOUTUBE_CHANNELS"):
            monkeypatch.delenv(var, raising=False)
        s = ArcSettings(env="paper", _env_file=None)  # type: ignore[call-arg]
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
