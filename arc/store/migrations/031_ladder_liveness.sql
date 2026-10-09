-- 031_ladder_liveness.sql — ladder liveness and re-attach (E11.2, D72).
--
-- A Broker ladder runs in a detached process (D34). If that process dies after a
-- submit, its DAY order kept working at the venue with no owner until the 16:30
-- reconcile. The run row now records the owning process and a heartbeat the
-- ladder refreshes on every poll; the `broker.reattach` job adopts any working
-- execution whose owner is gone (lock free or heartbeat stale).
--
-- routine_runs / executions are UPDATEd in place (not append-only): plain ALTERs.
ALTER TABLE routine_runs ADD COLUMN pid INTEGER;          -- os.getpid() of the process that ran it
ALTER TABLE routine_runs ADD COLUMN heartbeat_at TEXT;    -- to_db() UTC text, refreshed by the ladder

ALTER TABLE executions ADD COLUMN adopted_by_run_id TEXT; -- the broker.reattach run that took it over
ALTER TABLE executions ADD COLUMN adopted_at TEXT;

-- The last line of defence against a wedged-but-alive ladder and a re-attach both
-- recording the same broker fill (the ladder also checks the adoption fence first).
CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order_broker
    ON fills(order_id, broker_fill_id) WHERE broker_fill_id IS NOT NULL;
