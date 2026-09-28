-- 009_journal.sql — Decision journal (E7.4, D22).
--
-- Every decision the pipeline makes (selected, rejected, no-trade, approved,
-- expired) is one append-only row with a stable reason_code, the context
-- snapshot it was made on and the persona call that produced it. Outcomes are
-- attributed back to proposals; reviews label decision quality separately
-- from the outcome. All four tables are append-only: a correction is a new row
-- whose supersedes_id points at the row it corrects.

-- -------------------------------------------------------------------
-- Decisions
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                  TEXT PRIMARY KEY,
    chain_run_id        TEXT,
    run_id              TEXT,
    persona             TEXT NOT NULL,      -- scout | director | quant | risk | ... | owner | system
    stage               TEXT NOT NULL,      -- candidate | shortlist | structure | risk_review | ...
    subject             TEXT NOT NULL,      -- ticker, or 'session'
    choice              TEXT NOT NULL,      -- selected | rejected | no_trade | approved | ...
    reason_code         TEXT NOT NULL,      -- arc.journal.reasons.ReasonCode
    reason_text         TEXT NOT NULL DEFAULT '',
    confidence          REAL,
    inputs_snapshot_id  TEXT,               -- context_snapshots.id
    persona_call_id     TEXT,               -- persona_calls.id
    proposal_hash       TEXT,
    payload             TEXT NOT NULL DEFAULT '{}',  -- JSON of the item decided on
    supersedes_id       TEXT REFERENCES decisions(id),
    at                  TEXT NOT NULL       -- to_db() UTC text
);

CREATE INDEX IF NOT EXISTS idx_decisions_chain ON decisions(chain_run_id, at);
CREATE INDEX IF NOT EXISTS idx_decisions_proposal ON decisions(proposal_hash);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(at);

CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only');
END;

-- -------------------------------------------------------------------
-- Market context frozen at proposal time (one per proposal; corrections supersede)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_contexts (
    id              TEXT PRIMARY KEY,
    proposal_hash   TEXT NOT NULL,
    payload         TEXT NOT NULL,          -- MarketContext JSON
    quotes_as_of    TEXT,
    supersedes_id   TEXT REFERENCES market_contexts(id),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_contexts_proposal ON market_contexts(proposal_hash);

CREATE TRIGGER IF NOT EXISTS market_contexts_no_update
BEFORE UPDATE ON market_contexts
BEGIN
    SELECT RAISE(ABORT, 'market_contexts is append-only');
END;

CREATE TRIGGER IF NOT EXISTS market_contexts_no_delete
BEFORE DELETE ON market_contexts
BEGIN
    SELECT RAISE(ABORT, 'market_contexts is append-only');
END;

-- -------------------------------------------------------------------
-- Outcomes (deterministic, from fills and marks; E6.2/E6.3 fill them in)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    id                          TEXT PRIMARY KEY,
    proposal_hash               TEXT NOT NULL,
    status                      TEXT NOT NULL CHECK (status IN
                                    ('open', 'closed', 'expired_worthless', 'never_filled',
                                     'not_traded')),
    contracts                   INTEGER,
    limit_price                 TEXT,       -- Decimal text, per share (+debit / -credit)
    entry_fill                  TEXT,
    slippage_usd                TEXT,
    slippage_bps                REAL,
    cost_bps                    REAL,
    exit_fill                   TEXT,
    realised_pnl                TEXT,
    max_adverse_excursion       TEXT,
    days_held                   INTEGER,
    exit_reason                 TEXT,
    ev_total                    TEXT,
    pnl_vs_ev                   TEXT,
    hold_to_expiry_shadow_pnl   TEXT,       -- D19
    supersedes_id               TEXT REFERENCES outcomes(id),
    at                          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_proposal ON outcomes(proposal_hash, at);

CREATE TRIGGER IF NOT EXISTS outcomes_no_update
BEFORE UPDATE ON outcomes
BEGIN
    SELECT RAISE(ABORT, 'outcomes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
BEFORE DELETE ON outcomes
BEGIN
    SELECT RAISE(ABORT, 'outcomes is append-only');
END;

-- -------------------------------------------------------------------
-- Reviews: decision quality vs. outcome, plus a root-cause category.
-- Each review cites the decisions it judges (FK-checked).
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_reviews (
    id              TEXT PRIMARY KEY,
    proposal_hash   TEXT,
    decision_id     TEXT REFERENCES decisions(id),
    label           TEXT NOT NULL CHECK (label IN
                        ('good_decision_good_outcome', 'good_decision_bad_outcome',
                         'bad_decision_good_outcome', 'bad_decision_bad_outcome')),
    root_cause      TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT '',
    reviewer        TEXT NOT NULL CHECK (reviewer IN ('auditor', 'owner')),
    supersedes_id   TEXT REFERENCES decision_reviews(id),
    at              TEXT NOT NULL,
    CHECK (proposal_hash IS NOT NULL OR decision_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS decision_review_citations (
    review_id       TEXT NOT NULL REFERENCES decision_reviews(id),
    decision_id     TEXT NOT NULL REFERENCES decisions(id),
    PRIMARY KEY (review_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_proposal ON decision_reviews(proposal_hash);

CREATE TRIGGER IF NOT EXISTS decision_reviews_no_update
BEFORE UPDATE ON decision_reviews
BEGIN
    SELECT RAISE(ABORT, 'decision_reviews is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_reviews_no_delete
BEFORE DELETE ON decision_reviews
BEGIN
    SELECT RAISE(ABORT, 'decision_reviews is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_review_citations_no_update
BEFORE UPDATE ON decision_review_citations
BEGIN
    SELECT RAISE(ABORT, 'decision_review_citations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_review_citations_no_delete
BEFORE DELETE ON decision_review_citations
BEGIN
    SELECT RAISE(ABORT, 'decision_review_citations is append-only');
END;

-- -------------------------------------------------------------------
-- Persona call metadata: full prompt (replay), its non-context inputs,
-- token counts, latency and cost (subscription = 0 with tokens counted).
-- -------------------------------------------------------------------
ALTER TABLE persona_calls ADD COLUMN prompt_text TEXT;
ALTER TABLE persona_calls ADD COLUMN prompt_inputs TEXT;   -- JSON, see arc.pipeline.steps
ALTER TABLE persona_calls ADD COLUMN input_tokens INTEGER;
ALTER TABLE persona_calls ADD COLUMN output_tokens INTEGER;
ALTER TABLE persona_calls ADD COLUMN latency_ms INTEGER;
ALTER TABLE persona_calls ADD COLUMN cost_usd REAL;
