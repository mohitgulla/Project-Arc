-- 030_store_identity.sql — per-env store identity (E11.3, D70).
--
-- Paper and live never share a store. store_identity holds one row (id = 1)
-- naming the environment the store belongs to, stamped once by the first
-- read-write open (arc.store.identity.open_store, with the running ARC_ENV).
-- A process refuses a store stamped for the other env before any broker or LLM
-- call, so live evidence (closed trades for the E7.5a scorecard gate, D18 sizing
-- history) can only ever come from live rows. Same append-only pattern as
-- arm_identity (021): the row can never be changed or removed.
CREATE TABLE IF NOT EXISTS store_identity (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    env         TEXT NOT NULL CHECK (env IN ('paper', 'live')),
    created_at  TEXT NOT NULL           -- to_db() UTC text
);
CREATE TRIGGER IF NOT EXISTS store_identity_no_update
BEFORE UPDATE ON store_identity
BEGIN
    SELECT RAISE(ABORT, 'store_identity is written once');
END;
CREATE TRIGGER IF NOT EXISTS store_identity_no_delete
BEFORE DELETE ON store_identity
BEGIN
    SELECT RAISE(ABORT, 'store_identity is written once');
END;
