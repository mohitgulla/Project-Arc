-- 001_initial.sql — Initial schema for the Arc audit store.
-- All tables required by PLAN.md §2.3 / E1.3.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- -------------------------------------------------------------------
-- Schema version tracking
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version  INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -------------------------------------------------------------------
-- Candidates (from Scout persona)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
    id            TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    stance        TEXT NOT NULL CHECK (stance IN ('bullish', 'bearish', 'neutral')),
    catalyst_type TEXT NOT NULL,
    catalyst_date TEXT,          -- ISO-8601 or NULL
    confidence    REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    sources       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    created_at    TEXT NOT NULL,
    run_id        TEXT
);

-- -------------------------------------------------------------------
-- Proposals
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proposals (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL REFERENCES candidates(id),
    proposal_hash   TEXT NOT NULL UNIQUE,
    structure_json  TEXT NOT NULL,  -- JSON: Structure model
    thesis          TEXT NOT NULL,
    quant_json      TEXT NOT NULL,  -- JSON: QuantMetrics
    risk_narrative  TEXT NOT NULL DEFAULT '',
    sizing_json     TEXT NOT NULL,  -- JSON: Sizing
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    run_id          TEXT
);

-- -------------------------------------------------------------------
-- Gate decisions
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_decisions (
    id                TEXT PRIMARY KEY,
    proposal_hash     TEXT NOT NULL REFERENCES proposals(proposal_hash),
    passed            INTEGER NOT NULL CHECK (passed IN (0, 1)),
    violations_json   TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings
    token             TEXT,       -- HMAC gate token if passed
    account_snapshot  TEXT NOT NULL DEFAULT '{}',   -- JSON
    decided_at        TEXT NOT NULL,
    run_id            TEXT
);

-- -------------------------------------------------------------------
-- Approvals (Slack human decisions)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    proposal_hash   TEXT NOT NULL REFERENCES proposals(proposal_hash),
    slack_user      TEXT NOT NULL,
    slack_ts        TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'expired')),
    decided_at      TEXT NOT NULL,
    run_id          TEXT
);

-- -------------------------------------------------------------------
-- Orders
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    proposal_hash    TEXT NOT NULL REFERENCES proposals(proposal_hash),
    client_order_id  TEXT NOT NULL UNIQUE,  -- idempotency key
    state            TEXT NOT NULL DEFAULT 'proposed',
    broker_order_id  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    run_id           TEXT
);

-- -------------------------------------------------------------------
-- Order events (event-sourced state machine)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',  -- who/what triggered the transition
    detail      TEXT NOT NULL DEFAULT '',
    event_at    TEXT NOT NULL,
    run_id      TEXT,
    -- Idempotency: same order cannot have duplicate (from_state, to_state, event_at)
    UNIQUE(order_id, from_state, to_state, event_at)
);

CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id);

-- -------------------------------------------------------------------
-- Fills
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    broker_fill_id  TEXT,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    price           TEXT NOT NULL,  -- stored as text for Decimal precision
    filled_at       TEXT NOT NULL,
    run_id          TEXT
);

-- -------------------------------------------------------------------
-- Positions snapshots (point-in-time)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions_snapshots (
    id            TEXT PRIMARY KEY,
    snapshot_at   TEXT NOT NULL,
    positions_json TEXT NOT NULL,  -- JSON array of position objects
    run_id        TEXT
);

-- -------------------------------------------------------------------
-- PnL snapshots
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id            TEXT PRIMARY KEY,
    snapshot_at   TEXT NOT NULL,
    realized      TEXT NOT NULL,   -- Decimal as text
    unrealized    TEXT NOT NULL,   -- Decimal as text
    total         TEXT NOT NULL,   -- Decimal as text
    details_json  TEXT NOT NULL DEFAULT '{}',
    run_id        TEXT
);

-- -------------------------------------------------------------------
-- Halts (kill switch state)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS halts (
    id          TEXT PRIMARY KEY,
    halted_at   TEXT NOT NULL,
    resumed_at  TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    run_id      TEXT
);

-- -------------------------------------------------------------------
-- Tax lots (for wash-sale audit)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tax_lots (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    ticker          TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('long', 'short')),
    qty             INTEGER NOT NULL,
    open_price      TEXT NOT NULL,  -- Decimal as text
    close_price     TEXT,           -- Decimal as text, NULL if open
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    realized_pnl    TEXT,           -- Decimal as text
    wash_sale       INTEGER NOT NULL DEFAULT 0 CHECK (wash_sale IN (0, 1)),
    run_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_tax_lots_ticker ON tax_lots(ticker);
CREATE INDEX IF NOT EXISTS idx_tax_lots_closed_at ON tax_lots(closed_at);
