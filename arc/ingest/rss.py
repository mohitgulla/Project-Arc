"""RSS feed connector.

Reads a configurable list of feed URLs via ``feedparser``, extracts
entries, and yields ``RawDoc`` instances.  Incremental: persists the
latest ``published_parsed`` timestamp per feed as the cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import feedparser
import requests
import structlog

from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc

if TYPE_CHECKING:
    from arc.config import ArcSettings

log = structlog.get_logger()

CONNECTOR = "rss"
# Some publishers (e.g. Nasdaq) reset connections from non-browser user agents.
USER_AGENT = "Mozilla/5.0 (compatible; ProjectArc/0.1)"


def _download(feed_url: str, timeout: float) -> bytes:
    """GET one feed with a hard timeout (feedparser.parse(url) has none and can hang a tick)."""
    resp = requests.get(
        feed_url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    resp.raise_for_status()
    return resp.content


def _parse_published(entry: dict) -> datetime:
    """Best-effort parse of the entry's published date."""
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(UTC)
            except Exception:  # noqa: BLE001
                pass
    # Fallback: now
    return datetime.now(UTC)


def _entry_url(entry: dict) -> str:
    """Extract the canonical URL from an RSS entry."""
    return entry.get("link", entry.get("id", ""))


def _entry_text(entry: dict) -> str:
    """Extract the best text content from an RSS entry."""
    # Prefer summary, then content[0].value, then title
    if entry.get("summary"):
        return entry["summary"]
    content = entry.get("content")
    if content and isinstance(content, list) and content[0].get("value"):
        return content[0]["value"]
    return entry.get("title", "")


def _extract_tickers(text: str, universe: list[str]) -> list[str]:
    """Simple ticker mention extraction against the configured universe."""
    import re

    upper = text.upper()
    found = []
    for t in universe:
        pattern = rf"(?:^|[\s\[($])({re.escape(t)})(?:[\s\]).,;:!?]|$)"
        if re.search(pattern, upper):
            found.append(t)
    return found


def fetch_rss(
    conn: sqlite3.Connection,
    settings: ArcSettings,
) -> list[RawDoc]:
    """Fetch all configured RSS feeds and store new entries.

    Returns only newly stored documents (duplicates are skipped).
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    feeds = settings.ingest_rss_feeds

    if not feeds:
        log.warning("rss.no_feeds_configured")
        return []

    results: list[RawDoc] = []

    for feed_url in feeds:
        cursor_key = f"{CONNECTOR}:{feed_url}"
        last_cursor = cursor_repo.get(cursor_key)
        last_dt = (
            datetime.fromisoformat(last_cursor) if last_cursor else datetime.min.replace(tzinfo=UTC)
        )

        log.info("rss.fetching", feed_url=feed_url, cursor=last_cursor)
        try:
            body = _download(feed_url, settings.ingest_rss_timeout_seconds)
        except requests.RequestException as exc:
            log.warning("rss.fetch_error", feed_url=feed_url, error=str(exc))
            continue
        parsed = feedparser.parse(body)

        if parsed.bozo and not parsed.entries:
            log.warning("rss.parse_error", feed_url=feed_url, error=str(parsed.bozo_exception))
            continue

        newest_dt = last_dt

        for entry in parsed.entries:
            pub_dt = _parse_published(entry)
            if pub_dt <= last_dt:
                continue

            url = _entry_url(entry)
            if not url:
                continue

            text = _entry_text(entry)
            tickers = _extract_tickers(text, settings.universe)
            h = content_hash(CONNECTOR, url)

            doc = RawDoc(
                source=CONNECTOR,
                url=url,
                published_at=pub_dt,
                text=text,
                tickers_hint=tickers,
                content_hash=h,
            )

            doc_id = doc_repo.insert(
                source=doc.source,
                url=doc.url,
                published_at=doc.published_at.isoformat(),
                text=doc.text,
                tickers_hint=doc.tickers_hint,
                hash_val=h,
            )

            if doc_id is not None:
                results.append(doc)

            if pub_dt > newest_dt:
                newest_dt = pub_dt

        if newest_dt > last_dt:
            cursor_repo.set(cursor_key, newest_dt.isoformat())

    log.info("rss.done", new_docs=len(results))
    return results
