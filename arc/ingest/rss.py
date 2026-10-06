"""RSS feed connector.

Reads a configurable list of feed URLs via ``feedparser``, extracts
entries, and yields ``RawDoc`` instances.  Incremental: persists the
latest ``published_parsed`` timestamp per feed as the cursor.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import feedparser
import requests
import structlog

from arc.ingest.store import FILTERED_STATUS, IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc
from arc.universe.ingest import IngestUniverse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.config import ArcSettings
    from arc.context.ttl import Ttl
    from arc.ingest.sources import FeedSpec

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


@dataclass
class RssFetch:
    """One ``rss`` run: Sweep-readable new docs plus per-feed accounting.

    ``new`` / ``filtered`` are keyed by the feed's registry key (else its URL).
    ``filtered`` docs (D55) are stored closed ``sweep_status='filtered'`` and are
    not in ``docs`` (no ``raw_doc_ref`` context, never Sweep-read).
    """

    docs: list[RawDoc] = field(default_factory=list)
    new: Counter[str] = field(default_factory=Counter)
    filtered: Counter[str] = field(default_factory=Counter)


def fetch_rss(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    source_keys: Mapping[str, str] | None = None,
    max_ages: Mapping[str, Ttl] | None = None,
    feed_specs: Mapping[str, FeedSpec] | None = None,
    now: datetime | None = None,
) -> list[RawDoc]:
    """Fetch all configured RSS feeds; returns the new Sweep-readable docs.

    See :func:`fetch_rss_feeds` for the arguments and per-feed accounting.
    """
    return fetch_rss_feeds(
        conn, settings, source_keys=source_keys, max_ages=max_ages, feed_specs=feed_specs, now=now
    ).docs


def fetch_rss_feeds(  # noqa: PLR0912, PLR0915 - one pass per feed and entry
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    source_keys: Mapping[str, str] | None = None,
    max_ages: Mapping[str, Ttl] | None = None,
    feed_specs: Mapping[str, FeedSpec] | None = None,
    now: datetime | None = None,
) -> RssFetch:
    """Fetch all configured RSS feeds and store new entries.

    *source_keys* maps a feed URL to its E4.5 registry name (``wsj_markets``); the
    name is stored on each doc so the Sweep's per-source budget can group by feed.
    *max_ages* (D47) maps a feed URL to its category's freshness window: an entry
    older than that at *now* is never stored (logged per feed as
    ``ingest.skipped_stale``). *feed_specs* (D55) maps a feed URL to its
    :class:`~arc.ingest.sources.FeedSpec`; an entry its ``title_exclude`` /
    ``title_include`` filters out is stored closed ``sweep_status='filtered'``
    (audited, never silently dropped, never read). Duplicates are skipped.
    """
    keys: Mapping[str, str] = source_keys or {}
    ages: Mapping[str, Ttl] = max_ages or {}
    specs: Mapping[str, FeedSpec] = feed_specs or {}
    run_now = now or datetime.now(UTC)
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    feeds = settings.ingest_rss_feeds
    out = RssFetch()

    if not feeds:
        log.warning("rss.no_feeds_configured")
        return out

    results = out.docs
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
        spec = specs.get(feed_url)
        label = keys.get(feed_url) or feed_url
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
            title = str(entry.get("title") or "").strip() or None
            filtered = spec is not None and spec.title_filtered(title)

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
                title=title,
                source_key=keys.get(feed_url),
                closed_status=FILTERED_STATUS if filtered else None,
            )

            if doc_id is None:
                continue
            if filtered:  # D55: stored + audited, never read by the Sweep
                out.filtered[label] += 1
            else:
                out.new[label] += 1
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
        log.info("rss.feed_done", source=label, new=out.new[label], filtered=out.filtered[label])

    log.info("rss.done", new_docs=len(results), filtered=sum(out.filtered.values()))
    return out
