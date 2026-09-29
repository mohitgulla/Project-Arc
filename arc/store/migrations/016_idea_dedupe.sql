-- 016_idea_dedupe.sql — Idea fingerprints replace the one-open-per-ticker-per-day key (E5.9, D33).
--
-- A 5-minute trading loop (E5.8) may legitimately propose two different ideas on
-- one underlying in one day. The blunt (day, ticker) unique index from 007/011/013
-- goes; what stops repeats is the deterministic idea fingerprint
-- (ticker, stance, structure type, expiry ISO week, short-strike bucket) checked
-- against recent proposals / executions / owner rejections within config cooldowns
-- (arc/pipeline/dedupe.py). proposal_hash stays UNIQUE (001).
--
-- Idempotency inside one chain run is kept: a retried / resumed chain never
-- writes a second open proposal for the same ticker. Manual `arc propose` runs
-- carry a synthetic `manual-<ET day>-<run_id>` chain id.
ALTER TABLE proposals ADD COLUMN chain_run_id TEXT;
ALTER TABLE proposals ADD COLUMN fingerprint TEXT;   -- IdeaFingerprint.key()
ALTER TABLE proposals ADD COLUMN spot TEXT;          -- underlying mid at proposal time (Decimal text)
ALTER TABLE proposals ADD COLUMN regime TEXT;        -- Director market_regime at proposal time

DROP INDEX IF EXISTS idx_proposals_day_ticker;
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_chain_ticker
    ON proposals(chain_run_id, ticker)
    WHERE chain_run_id IS NOT NULL AND kind = 'open' AND swap_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_proposals_fingerprint ON proposals(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_ticker_day ON proposals(ticker, day);
