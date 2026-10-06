-- 025_investor_to_broker.sql — D56 (E13.2): the Investor and Auditor personas are removed.
--
-- Job renames (no table or column carries an investor_* / auditor_* name, so no DDL):
--   investor -> broker, execute -> broker.execute (chain step), investor.exits ->
--   quant.exits (chain step), auditor -> broker.reconcile. The weekly scorecard keeps
--   its job name and posts as [Ops].
--
-- History stays as written: decisions.persona / context_entries.produced_by /
-- routine_runs.job = 'investor' | 'auditor' | 'execute' | 'investor.exits' are
-- append-only rows; readers map them through arc.journal.legacy.RENAME_CHAIN using the
-- cutover instant below ('investor' exit rows read as Quant, other 'investor' rows as
-- Broker, 'auditor' rows as Broker (reconcile)). decision_reviews.reviewer = 'auditor'
-- stays a valid reviewer value (CHECK constraint unchanged).

-- Dispatcher cursor: cursor:auditor -> cursor:broker.reconcile, so the 16:30 reconcile
-- does not re-fire a missed slot after deploy. The Broker job is event-driven (no cursor).
INSERT OR REPLACE INTO routine_state (key, value, updated_at)
    SELECT 'cursor:broker.reconcile', value, updated_at
    FROM routine_state
    WHERE key = 'cursor:auditor';
DELETE FROM routine_state WHERE key = 'cursor:auditor';

-- The cutover instant (fixed-width UTC, arc.context.ttl.to_db format). Written once: a
-- re-run never moves it.
INSERT OR IGNORE INTO routine_state (key, value, updated_at)
    VALUES ('rename:investor_to_broker',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');
