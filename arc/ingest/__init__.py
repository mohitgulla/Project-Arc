"""Source connectors: RSS, EDGAR, earnings calendar, YouTube transcripts.

Each connector implements ``fetch(conn, cursor_repo, doc_repo, settings)``
returning a list of ``RawDoc`` objects that were newly stored (dedupe'd).
"""

from arc.ingest.earnings import fetch_earnings
from arc.ingest.edgar import fetch_edgar
from arc.ingest.rss import fetch_rss
from arc.ingest.youtube import fetch_youtube

__all__ = ["fetch_rss", "fetch_edgar", "fetch_earnings", "fetch_youtube"]
