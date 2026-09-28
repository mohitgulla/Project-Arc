-- 002_ingest.sql — Raw documents and incremental cursors for E4.1.

-- -------------------------------------------------------------------
-- Raw documents (deduplicated ingestion output)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_docs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,          -- rss | edgar | earnings | youtube
    url           TEXT NOT NULL,
    published_at  TEXT NOT NULL,          -- ISO-8601
    text          TEXT NOT NULL,
    tickers_hint  TEXT NOT NULL DEFAULT '[]',  -- JSON array
    content_hash  TEXT NOT NULL UNIQUE,   -- SHA-256(source+url) for dedupe
    ingested_at   TEXT NOT NULL,
    run_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_docs_source ON raw_docs(source);
CREATE INDEX IF NOT EXISTS idx_raw_docs_published ON raw_docs(published_at);

-- -------------------------------------------------------------------
-- Ingest cursors (incremental state per connector)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_cursors (
    connector   TEXT PRIMARY KEY,        -- rss | edgar | earnings | youtube
    cursor_val  TEXT NOT NULL,           -- last-seen timestamp or ID
    updated_at  TEXT NOT NULL
);
