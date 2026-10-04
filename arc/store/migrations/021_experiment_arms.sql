-- 021_experiment_arms.sql — experiment arm stores (E10.2, D44).
--
-- Every experiment arm except production control runs against its OWN store
-- (``config/experiments.yaml`` runner.arms.<arm>.db). Its D32 order budget, halts,
-- reconcile, positions, proposals and ladders are the arm's own because they are
-- rows in that DB, written by the unchanged trading code. These tables exist in
-- every store; in the control store arm_identity / virtual_ledger / arm_pairs
-- stay empty and market_tape holds the control loop's market reads while an arm
-- pairs with it.
--
-- arm_identity: one row (id = 1) naming the arm a store belongs to, written once
-- by ``arc experiment start``. Rows in an arm store carry no arm_id column: the
-- store is the tag, and arc.experiments.paired projects ``arm_id`` from this row
-- when it unions arm rows with control's (NULL = control). A store with no
-- identity (control, every pre-E10 DB) is control.
CREATE TABLE IF NOT EXISTS arm_identity (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    arm_id          TEXT NOT NULL,          -- '<experiment id>:<arm>'
    experiment_id   TEXT NOT NULL,
    arm             TEXT NOT NULL,          -- runner arm name (treatment, shadow_control, ...)
    spec_arm        TEXT NOT NULL,          -- the spec arm whose overlay it runs (control|treatment)
    keys_env        TEXT NOT NULL,          -- env prefix of its broker keys (ALPACA_EXP)
    control_db      TEXT NOT NULL,          -- the control store it pairs with
    overlay         TEXT NOT NULL DEFAULT '{}',  -- the spec arm's overlay (JSON)
    created_at      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS arm_identity_no_update
BEFORE UPDATE ON arm_identity
BEGIN
    SELECT RAISE(ABORT, 'arm_identity is written once at t0');
END;
CREATE TRIGGER IF NOT EXISTS arm_identity_no_delete
BEFORE DELETE ON arm_identity
BEGIN
    SELECT RAISE(ABORT, 'arm_identity is written once at t0');
END;

-- virtual_ledger: the arm's virtual account (D44, owner decision 2026-10-03),
-- append-only; arc.experiments.virtual replays it into the account state.
--   open            t0: virtual cash = control's t0 equity (amount)
--   legacy_hold     t0: control's legacy structure `ref` reserves its max loss (amount)
--   legacy_release  that legacy structure closed on control (ref)
--   fill            one broker fill of the arm: premium cash flow (amount: + received,
--                   - paid, fees included); `settles_on` = the ET day the cash settles
CREATE TABLE IF NOT EXISTS virtual_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    arm_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('open', 'legacy_hold', 'legacy_release', 'fill')),
    ref         TEXT NOT NULL,
    amount      TEXT NOT NULL,          -- Decimal text, dollars
    settles_on  TEXT,                   -- YYYY-MM-DD (fill)
    at          TEXT NOT NULL,          -- to_db() UTC text
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_virtual_ledger_ref ON virtual_ledger(arm_id, kind, ref);
CREATE TRIGGER IF NOT EXISTS virtual_ledger_no_update
BEFORE UPDATE ON virtual_ledger
BEGIN
    SELECT RAISE(ABORT, 'virtual_ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS virtual_ledger_no_delete
BEFORE DELETE ON virtual_ledger
BEGIN
    SELECT RAISE(ABORT, 'virtual_ledger is append-only');
END;

-- arm_pairs: the control loop chain each arm chain pairs with (shared inputs).
-- The arm chain reuses control's steps before `fork_step` verbatim and re-runs
-- from it. One row per (arm, control chain); status moves running -> final once.
CREATE TABLE IF NOT EXISTS arm_pairs (
    arm_id               TEXT NOT NULL,
    control_chain_run_id TEXT NOT NULL,
    arm_chain_run_id     TEXT,
    fork_step            TEXT,
    status               TEXT NOT NULL CHECK (status IN ('running', 'ok', 'skipped', 'failed')),
    reason               TEXT NOT NULL DEFAULT '',
    lag_seconds          REAL,
    at                   TEXT NOT NULL,
    PRIMARY KEY (arm_id, control_chain_run_id)
);
CREATE INDEX IF NOT EXISTS idx_arm_pairs_arm_chain ON arm_pairs(arm_chain_run_id);

-- market_tape (control store): what the control loop chain read from the market
-- provider, keyed by chain + call, so a paired arm reads the identical snapshot.
-- A cache, not an audit table: zlib JSON, pruned after runner.tape_keep.
CREATE TABLE IF NOT EXISTS market_tape (
    chain_run_id  TEXT NOT NULL,
    call          TEXT NOT NULL,        -- method + canonical args
    payload       BLOB NOT NULL,        -- zlib(JSON)
    at            TEXT NOT NULL,
    PRIMARY KEY (chain_run_id, call)
);
CREATE INDEX IF NOT EXISTS idx_market_tape_at ON market_tape(at);

-- halts / positions_snapshots need no arm_id here: an arm's halts and snapshots live
-- in its own store, and arc.experiments.paired projects arm_id at read time.
