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
from arc.universe.ingest import IngestUniverse

if TYPE_CHECKING:
    from arc.config import ArcSettings
    from arc.context.ttl import Ttl

log = structlog.get_logger()

CONNECTOR = "edgar"
EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
FORM_TYPES = ("8-K", "10-Q", "4")
_HINT_SCAN_CHARS = 20_000  # ticker hints: scan the head of the filing only

# EDGAR asks ≤10 req/s; we target ~5 to be safe
_MIN_REQUEST_INTERVAL = 0.2


def _user_agent(settings: ArcSettings) -> str:
    """Construct the EDGAR-required User-Agent string."""
    return settings.edgar_user_agent


def _company_tickers(settings: ArcSettings) -> dict[str, str]:
    """``{TICKER: zero-padded CIK}`` from EDGAR's company tickers JSON (one download).

    E5.10: fetched at most once per run (it is ~0.8 MB); an empty map on failure.
    """
    import json
    import urllib.request

    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(settings)})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        log.warning("edgar.company_tickers_failed")
        return {}
    return {
        str(entry.get("ticker", "")).upper(): str(entry["cik_str"]).zfill(10)
        for entry in data.values()
        if entry.get("ticker") and entry.get("cik_str") is not None
    }


def _ticker_to_cik(ticker: str, settings: ArcSettings) -> str | None:
    """Look up CIK for a ticker via EDGAR company tickers JSON.

    Uses a simple HTTP GET with rate limiting.
    """
    cik = _company_tickers(settings).get(ticker.upper())
    if cik is None:
        log.warning("edgar.cik_lookup_failed", ticker=ticker)
    return cik


def _fetch_submissions(cik: str, settings: ArcSettings) -> dict | None:
    """One ``submissions/CIK##########.json`` document (``None`` on failure)."""
    import json
    import urllib.request

    cik_padded = cik.zfill(10)
    url = f"{SUBMISSIONS_BASE}/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(settings)})

    time.sleep(_MIN_REQUEST_INTERVAL)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001
        log.warning("edgar.submissions_failed", cik=cik)
        return None


def _filings_of_form(data: dict | None, cik: str, form_type: str, *, count: int) -> list[dict]:
    """The *count* most recent filings of *form_type* in a submissions document.

    Newest first (EDGAR's ``recent`` arrays are newest first). ``acceptanceDateTime``
    is carried when present (D47: the filing's real publish time).
    """
    if not data:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    accepted = recent.get("acceptanceDateTime", [])
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
                "acceptanceDateTime": accepted[i] if i < len(accepted) else "",
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
                "form": form,
                "cik": cik,
            }
        )

    return results


def filing_published_at(filing: dict) -> datetime | None:
    """D47: the filing's accepted time (UTC), else its filing date (00:00 UTC).

    ``None`` when neither parses (the caller then has no age to judge by).
    """
    raw = str(filing.get("acceptanceDateTime") or "")
    if raw:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime.strptime(str(filing.get("filingDate") or ""), "%Y-%m-%d").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def _fetch_recent_filings(
    cik: str,
    form_type: str,
    settings: ArcSettings,
    *,
    count: int = 10,
) -> list[dict]:
    """Fetch recent filings for a CIK from EDGAR submissions API."""
    return _filings_of_form(_fetch_submissions(cik, settings), cik, form_type, count=count)


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
    *,
    now: datetime | None = None,
    max_age: Ttl | None = None,
) -> list[RawDoc]:
    """Fetch recent EDGAR filings for configured tickers.

    Returns only newly stored documents.

    D47 (E4.7): ``published_at`` is the filing's accepted time. With *max_age*
    (the ``company`` category window), a filing older than that at *now* is
    skipped before its text is downloaded and never stored (logged as
    ``ingest.skipped_stale``): a ticker new to the universe, or an old filing
    re-listed in the submissions feed, no longer floods the queue with months-old
    10-Qs. The per-form cursor is the newest accession seen; the walk stops at it
    in EDGAR's newest-first order (accession numbers are prefixed by the filer
    agent's CIK, so comparing them as strings is not chronological).
    """
    cursor_repo = IngestCursorRepo(conn)
    doc_repo = RawDocRepo(conn)
    # D51: filings for today's active list (core until the first resolve of the day).
    # D28: CIKs come from the cached symbol master when it has them (no per-ticker
    # download of the SEC file); ticker hints add master-validated symbols in the text.
    uni = IngestUniverse.from_settings(settings, now=now, conn=conn)
    tickers = list(uni.seed)

    results: list[RawDoc] = []
    # E5.10: tickers missing from the symbol master share ONE company_tickers.json
    # download per run (it was one ~0.8 MB download per such ticker per run).
    fallback_ciks: dict[str, str] | None = None
    requests = 0
    stale = 0
    run_now = now or datetime.now(UTC)

    for ticker in tickers:
        cik = uni.cik(ticker)
        if not cik:
            if fallback_ciks is None:
                fallback_ciks = _company_tickers(settings)
                requests += 1
            cik = fallback_ciks.get(ticker.upper())
        if not cik:
            log.warning("edgar.cik_lookup_failed", ticker=ticker)
            continue

        # E5.10: one submissions request per company covers every form type (it was
        # one identical request per form type, i.e. 3x the requests and bytes).
        submissions = _fetch_submissions(cik, settings)
        requests += 1

        for form_type in FORM_TYPES:
            cursor_key = f"{CONNECTOR}:{ticker}:{form_type}"
            last_accession = cursor_repo.get(cursor_key)

            filings = _filings_of_form(submissions, cik, form_type, count=5)

            newest_accession: str | None = None

            for filing in filings:  # newest first
                acc = filing["accessionNumber"]
                if last_accession and acc == last_accession:
                    break  # everything from here on was seen by an earlier run

                pub_dt = filing_published_at(filing)
                if (
                    max_age is not None
                    and pub_dt is not None
                    and max_age.expires_at(pub_dt) <= run_now
                ):
                    stale += 1
                    newest_accession = newest_accession or acc
                    continue

                url = _filing_url(
                    filing["cik"],
                    acc,
                    filing["primaryDocument"],
                )

                text = _fetch_filing_text(url, settings)
                requests += 1
                if not text:
                    continue

                h = content_hash(CONNECTOR, url)
                doc = RawDoc(
                    source=CONNECTOR,
                    url=url,
                    published_at=pub_dt or run_now,
                    text=text,
                    tickers_hint=list(
                        dict.fromkeys([ticker, *uni.tickers_in(text[:_HINT_SCAN_CHARS])])
                    ),
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

                newest_accession = newest_accession or acc

            if newest_accession:
                cursor_repo.set(cursor_key, newest_accession)

    if stale:
        log.info("ingest.skipped_stale", source=CONNECTOR, count=stale, max_age=str(max_age))
    log.info(
        "edgar.done",
        new_docs=len(results),
        skipped_stale=stale,
        requests=requests,
        tickers=len(tickers),
    )
    return results
