-- 007_pipeline.sql — Pipeline runner (E5.2).

-- -------------------------------------------------------------------
-- Persona calls: audit trail of every Director/Quant/Risk LLM call.
-- The verbatim prompt hash and response live here; downstream steps read
-- only the validated, filtered context entries (D16).
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_calls (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,          -- routine_runs.run_id
    persona         TEXT NOT NULL,          -- director | quant | risk
    model           TEXT NOT NULL,          -- model id or 'fixture'
    snapshot_id     TEXT,                   -- context snapshot the prompt was built from
    prompt_sha256   TEXT NOT NULL,
    raw_response    TEXT,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'llm_error', 'parse_error')),
    error           TEXT,
    dropped         TEXT NOT NULL DEFAULT '{}',  -- JSON {reason: count} of filtered items
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_calls_run ON persona_calls(run_id);

-- One proposal per (session day, ticker): the idempotency key of `arc propose`.
ALTER TABLE proposals ADD COLUMN day TEXT;      -- YYYY-MM-DD, America/New_York
ALTER TABLE proposals ADD COLUMN ticker TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_day_ticker
    ON proposals(day, ticker) WHERE day IS NOT NULL;
