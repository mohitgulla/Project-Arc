-- 003_scout.sql — Scout candidate pipeline (E4.2).

-- -------------------------------------------------------------------
-- Candidates: one row per ticker per trading day (ET). Rows written
-- before this migration keep day = NULL and are not merge targets.
-- -------------------------------------------------------------------
ALTER TABLE candidates ADD COLUMN day TEXT;  -- YYYY-MM-DD, America/New_York
ALTER TABLE candidates ADD COLUMN updated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_ticker_day
    ON candidates(ticker, day) WHERE day IS NOT NULL;

-- -------------------------------------------------------------------
-- Raw docs: mark which docs the Scout has already summarised.
-- -------------------------------------------------------------------
ALTER TABLE raw_docs ADD COLUMN scouted_at TEXT;
ALTER TABLE raw_docs ADD COLUMN scout_run_id TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_docs_unscouted ON raw_docs(scouted_at);

-- -------------------------------------------------------------------
-- Scout batches: audit trail of every LLM call. Unstructured persona
-- text (raw response, rationale) lives here and ONLY here — it never
-- flows downstream to the scanner.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scout_batches (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    model           TEXT NOT NULL,          -- model id or 'fixture'
    doc_ids         TEXT NOT NULL,          -- JSON array of raw_docs.id
    prompt_sha256   TEXT NOT NULL,
    raw_response    TEXT,                   -- verbatim LLM output
    status          TEXT NOT NULL CHECK (status IN ('ok', 'llm_error', 'parse_error')),
    error           TEXT,
    accepted        INTEGER NOT NULL DEFAULT 0,
    rejected        TEXT NOT NULL DEFAULT '{}',  -- JSON {reason: count}
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scout_batches_run ON scout_batches(run_id);
