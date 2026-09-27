"""Earnings calendar connector.

Fetches upcoming and recent earnings dates from Finnhub (free tier
default, configurable).  Yields one ``RawDoc`` per earnings event
with the ticker, date, and any available EPS estimate data.

Incremental: persists the last-fetched date range as the cursor.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import structlog

from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc

if TYPE_CHECKING:
    from arc.config import ArcSettings

log = structlog.get_logger()

CONNECTOR = "earnings"


def _fetch_finnhub_earnings(
    api_key: str,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """Fetch earnings calendar from Finnhub free tier."""
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings?from={from_date}&to={to_date}&token={api_key}"
    )
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("earningsCalendar", [])
    except Exception:  # noqa: BLE001
        log.warning("earnings.finnhub_failed", from_date=from_date, to_date=to_date)
        return []


def fetch_earnings(
    conn: sqlite3.Connection,
    settings: ArcSettings,
) -> list[RawDoc]:
    """Fetch earnings calendar events for the configured universe.

    Returns only newly stored documents.
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)

    api_key = settings.finnhub_api_key
    if not api_key:
        log.warning("earnings.no_api_key", hint="Set ARC_FINNHUB_API_KEY in ~/.hermes/.env")
        return []

    # Determine date range: look 7 days back and 30 days forward
    cursor_val = cursor_repo.get(CONNECTOR)
    today = datetime.now(UTC).date()

    if cursor_val:
        try:
            from_date = datetime.fromisoformat(cursor_val).date()
        except ValueError:
            from_date = today - timedelta(days=7)
    else:
        from_date = today - timedelta(days=7)

    to_date = today + timedelta(days=30)

    events = _fetch_finnhub_earnings(
        api_key,
        from_date.isoformat(),
        to_date.isoformat(),
    )

    # Filter to universe tickers
    universe_set = {t.upper() for t in settings.universe}
    results: list[RawDoc] = []

    for event in events:
        symbol = event.get("symbol", "").upper()
        if symbol not in universe_set:
            continue

        report_date = event.get("date", "")
        if not report_date:
            continue

        # Build a descriptive text from the event
        eps_estimate = event.get("epsEstimate")
        eps_actual = event.get("epsActual")
        revenue_estimate = event.get("revenueEstimate")
        hour = event.get("hour", "")

        text_parts = [
            f"Earnings report for {symbol} on {report_date}.",
            f"Reporting: {hour}" if hour else "",
            f"EPS estimate: {eps_estimate}" if eps_estimate is not None else "",
            f"EPS actual: {eps_actual}" if eps_actual is not None else "",
            f"Revenue estimate: {revenue_estimate}" if revenue_estimate is not None else "",
        ]
        text = " ".join(p for p in text_parts if p)

        url = f"https://finnhub.io/calendar/earnings/{symbol}/{report_date}"
        h = content_hash(CONNECTOR, url)

        try:
            pub_dt = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pub_dt = datetime.now(UTC)

        doc = RawDoc(
            source=CONNECTOR,
            url=url,
            published_at=pub_dt,
            text=text,
            tickers_hint=[symbol],
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

    # Update cursor to today
    cursor_repo.set(CONNECTOR, today.isoformat())

    log.info("earnings.done", new_docs=len(results))
    return results
