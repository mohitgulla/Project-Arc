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
from arc.universe.ingest import IngestUniverse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.config import ArcSettings
    from arc.context.ttl import Ttl

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


def _extract_tickers(text: str, universe: list[str] | IngestUniverse) -> list[str]:
    """Ticker hints: seed list + (D28 seed mode) master-validated cashtags/symbols."""
    import re

    if not isinstance(universe, list):
        return universe.tickers_in(text)
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
    *,
    source_keys: Mapping[str, str] | None = None,
    max_ages: Mapping[str, Ttl] | None = None,
    now: datetime | None = None,
) -> list[RawDoc]:
    """Fetch all configured RSS feeds and store new entries.

    *source_keys* maps a feed URL to its E4.5 registry name (``wsj_markets``); the
    name is stored on each doc so the Scout's per-source budget can group by feed.
    *max_ages* (D47) maps a feed URL to its category's freshness window: an entry
    older than that at *now* is never stored (logged per feed as
    ``ingest.skipped_stale``). Returns only newly stored documents (duplicates are
    skipped).
    """
    keys: Mapping[str, str] = source_keys or {}
    ages: Mapping[str, Ttl] = max_ages or {}
    run_now = now or datetime.now(UTC)
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    feeds = settings.ingest_rss_feeds

    if not feeds:
        log.warning("rss.no_feeds_configured")
        return []

    results: list[RawDoc] = []
    uni = IngestUniverse.from_settings(settings, now=now, conn=conn)

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
        max_age = ages.get(feed_url)
        stale = 0

        for entry in parsed.entries:
            pub_dt = _parse_published(entry)
            if pub_dt <= last_dt:
                continue
            if pub_dt > newest_dt:
                newest_dt = pub_dt
            if max_age is not None and max_age.expires_at(pub_dt) <= run_now:
                stale += 1  # D47: past its category's window; never stored
                continue

            url = _entry_url(entry)
            if not url:
                continue

            text = _entry_text(entry)
            tickers = _extract_tickers(text, uni)
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
                title=(str(entry.get("title") or "").strip() or None),
                source_key=keys.get(feed_url),
            )

            if doc_id is not None:
                results.append(doc)

        if stale:
            log.info(
                "ingest.skipped_stale",
                source=keys.get(feed_url) or feed_url,
                count=stale,
                max_age=str(max_age),
            )
        if newest_dt > last_dt:
            cursor_repo.set(cursor_key, newest_dt.isoformat())

    log.info("rss.done", new_docs=len(results))
    return results
