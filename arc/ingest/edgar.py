"""SEC EDGAR connector.

Fetches 8-K, 10-Q, and Form 4 filings from the EDGAR full-text search
and company submissions APIs.  Respects the ≤10 req/s rate limit and
sends the required ``User-Agent`` header.

Incremental: persists the latest filing ``accessionNumber`` per form
type as the cursor.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

import structlog

from arc.ingest.store import IngestCursorRepo, RawDocRepo, content_hash
from arc.models import RawDoc

if TYPE_CHECKING:
    from arc.config import ArcSettings

log = structlog.get_logger()

CONNECTOR = "edgar"
EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
FORM_TYPES = ("8-K", "10-Q", "4")

# EDGAR asks ≤10 req/s; we target ~5 to be safe
_MIN_REQUEST_INTERVAL = 0.2


def _user_agent(settings: ArcSettings) -> str:
    """Construct the EDGAR-required User-Agent string."""
    return settings.edgar_user_agent


def _ticker_to_cik(ticker: str, settings: ArcSettings) -> str | None:
    """Look up CIK for a ticker via EDGAR company tickers JSON.

    Uses a simple HTTP GET with rate limiting.
    """
    import urllib.request

    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(settings)})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json

            data = json.loads(resp.read())
            for _key, entry in data.items():
                if entry.get("ticker", "").upper() == ticker.upper():
                    return str(entry["cik_str"]).zfill(10)
    except Exception:  # noqa: BLE001
        log.warning("edgar.cik_lookup_failed", ticker=ticker)
    return None


def _fetch_recent_filings(
    cik: str,
    form_type: str,
    settings: ArcSettings,
    *,
    count: int = 10,
) -> list[dict]:
    """Fetch recent filings for a CIK from EDGAR submissions API."""
    import json
    import urllib.request

    cik_padded = cik.zfill(10)
    url = f"{SUBMISSIONS_BASE}/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(settings)})

    time.sleep(_MIN_REQUEST_INTERVAL)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        log.warning("edgar.submissions_failed", cik=cik)
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    results = []
    for i, form in enumerate(forms):
        if form != form_type:
            continue
        if len(results) >= count:
            break
        results.append(
            {
                "accessionNumber": accessions[i] if i < len(accessions) else "",
                "filingDate": dates[i] if i < len(dates) else "",
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
                "form": form,
                "cik": cik,
            }
        )

    return results


def _filing_url(cik: str, accession: str, primary_doc: str) -> str:
    """Construct the URL for a filing document."""
    acc_no_dash = accession.replace("-", "")
    cik_int = str(int(cik))
    return f"{FILING_BASE}/{cik_int}/{acc_no_dash}/{primary_doc}"


def _fetch_filing_text(url: str, settings: ArcSettings) -> str:
    """Download and extract text from a filing URL."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(settings)})
    time.sleep(_MIN_REQUEST_INTERVAL)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            # Strip HTML tags for a rough text extraction
            import re

            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            # Truncate to ~50k chars to avoid huge blobs
            return text[:50_000]
    except Exception:  # noqa: BLE001
        log.warning("edgar.filing_fetch_failed", url=url)
        return ""


def fetch_edgar(
    conn: sqlite3.Connection,
    settings: ArcSettings,
) -> list[RawDoc]:
    """Fetch recent EDGAR filings for configured tickers.

    Returns only newly stored documents.
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    tickers = settings.universe

    results: list[RawDoc] = []

    for ticker in tickers:
        cik = _ticker_to_cik(ticker, settings)
        if not cik:
            continue

        for form_type in FORM_TYPES:
            cursor_key = f"{CONNECTOR}:{ticker}:{form_type}"
            last_accession = cursor_repo.get(cursor_key)

            filings = _fetch_recent_filings(cik, form_type, settings, count=5)

            newest_accession: str | None = None

            for filing in filings:
                acc = filing["accessionNumber"]
                if last_accession and acc <= last_accession:
                    continue

                url = _filing_url(
                    filing["cik"],
                    acc,
                    filing["primaryDocument"],
                )

                text = _fetch_filing_text(url, settings)
                if not text:
                    continue

                filing_date = filing.get("filingDate", "")
                try:
                    pub_dt = datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    pub_dt = datetime.now(UTC)

                h = content_hash(CONNECTOR, url)
                doc = RawDoc(
                    source=CONNECTOR,
                    url=url,
                    published_at=pub_dt,
                    text=text,
                    tickers_hint=[ticker],
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

                if newest_accession is None or acc > newest_accession:
                    newest_accession = acc

            if newest_accession:
                cursor_repo.set(cursor_key, newest_accession)

    log.info("edgar.done", new_docs=len(results))
    return results
