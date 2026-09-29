-- 015_source_fairness.sql — source fairness + two-stage synthesis (E4.5, D30).
--
-- raw_docs.source_key: the registry source a doc came from (an RSS feed name such as
--   'wsj_markets', or the connector name: 'edgar', 'cboe_putcall', ...). NULL for rows
--   ingested before this migration; arc.ingest.sources derives the key from the URL.
-- raw_docs.scout_status: how the Scout closed the doc: 'scouted' (read in a digest) or
--   'skipped_budget' (never selected by the per-source budget before its context TTL
--   ran out). scout_run_id is the run that closed it. NULL = still open.
-- candidates.corroboration: distinct registry sources behind the candidate's grounded
--   URLs (D30). Only this number may raise corroboration; the LLM cannot set it.
-- scout_batches.stage: 'digest' (stage 1, per story batch) or 'scout' (stage 2).
--   Token/cost columns come from `hermes -z --usage-file` (NULL for fixtures), so the
--   D27 run manifest sums them for the Scout like it does for persona_calls.

ALTER TABLE raw_docs ADD COLUMN source_key TEXT;
ALTER TABLE raw_docs ADD COLUMN scout_status TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_docs_source_key ON raw_docs(source_key);

ALTER TABLE candidates ADD COLUMN corroboration INTEGER;

ALTER TABLE scout_batches ADD COLUMN stage TEXT NOT NULL DEFAULT 'scout';
ALTER TABLE scout_batches ADD COLUMN input_tokens INTEGER;
ALTER TABLE scout_batches ADD COLUMN output_tokens INTEGER;
ALTER TABLE scout_batches ADD COLUMN cost_usd REAL;

-- Daily options volume per underlying (E4.5 unusual options activity). One row per
-- (ticker, day); a later run the same day replaces it, so the 20-day average compares
-- today's volume with prior sessions' final volumes.
CREATE TABLE IF NOT EXISTS options_volume_daily (
    ticker       TEXT NOT NULL,
    day          TEXT NOT NULL,          -- YYYY-MM-DD (ET)
    call_volume  INTEGER NOT NULL,
    put_volume   INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (ticker, day)
);
