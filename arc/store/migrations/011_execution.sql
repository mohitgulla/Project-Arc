-- 011_execution.sql — Execution ladder, open structures and exits (E6.2, D24).

-- -------------------------------------------------------------------
-- Proposals now carry what they do: open a new structure or close one.
-- The (day, ticker) idempotency key of `arc propose` applies to opens only,
-- so an exit on a ticker proposed the same day is not blocked by it.
-- -------------------------------------------------------------------
ALTER TABLE proposals ADD COLUMN kind TEXT NOT NULL DEFAULT 'open'
    CHECK (kind IN ('open', 'close'));

DROP INDEX IF EXISTS idx_proposals_day_ticker;
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_day_ticker
    ON proposals(day, ticker) WHERE day IS NOT NULL AND kind = 'open';

-- -------------------------------------------------------------------
-- One execution per approved proposal: the price-band ladder run by
-- arc.execution.execute(). Attempts are `orders` rows (client_order_id =
-- <arc2 token>.s<k>), with their state events in `order_events`.
--
-- status:
--   working           the ladder is running
--   filled            filled in full (steps_used = index of the filling step)
--   partially_filled  some contracts filled, then the attempt was cancelled
--   cancelled         every attempt timed out and was cancelled (no fill)
--   rejected          refused before or by the broker (halt, token, broker error)
--   unconfirmed       a cancel was not confirmed in time: ladder stopped, reconcile
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS executions (
    proposal_hash   TEXT PRIMARY KEY REFERENCES proposals(proposal_hash),
    kind            TEXT NOT NULL CHECK (kind IN ('open', 'close')),
    structure_id    TEXT,                   -- open_structures.id (set on fill / for closes)
    status          TEXT NOT NULL CHECK (status IN
                        ('working', 'filled', 'partially_filled', 'cancelled',
                         'rejected', 'unconfirmed')),
    token_version   TEXT NOT NULL,          -- arc1 | arc2
    band_lo         TEXT NOT NULL,          -- per-share limit, Decimal text (+ debit / - credit)
    band_hi         TEXT NOT NULL,
    max_steps       INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    steps_used      INTEGER,                -- index of the filling attempt (0 = mid)
    contracts       INTEGER NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    fill_price      TEXT,
    detail          TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    run_id          TEXT
);

-- -------------------------------------------------------------------
-- Open structures: the local position model. Legs are keyed by the structure
-- that opened them (not by root), so two structures on one underlying stay
-- separate (Sentinel S-5). E6.3 reconciles these against broker positions.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS open_structures (
    id                   TEXT PRIMARY KEY,
    ticker               TEXT NOT NULL,
    open_proposal_hash   TEXT NOT NULL UNIQUE REFERENCES proposals(proposal_hash),
    candidate_id         TEXT NOT NULL,
    structure_json       TEXT NOT NULL,     -- Structure as opened (premiums = entry mids)
    contracts            INTEGER NOT NULL CHECK (contracts > 0),
    entry_net            TEXT NOT NULL,     -- per-share fill price (+ debit / - credit)
    opened_at            TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    exit_proposal_hash   TEXT,              -- the pending / working exit, if any
    exit_reason          TEXT,
    exit_day             TEXT,              -- ET day of the latest exit proposal (one per day)
    closed_at            TEXT,
    close_net            TEXT
);

CREATE INDEX IF NOT EXISTS idx_open_structures_status ON open_structures(status);
