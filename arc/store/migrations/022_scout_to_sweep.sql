-- 022_scout_to_sweep.sql — D54 (E5.12): the 30-min doc reader "Scout" is renamed Sweep.
--
-- Mechanical rename of the DB identifiers; no row is added, dropped or rewritten except
-- the two dispatcher cursors (moved so the Sweep does not re-read every doc after deploy).
-- SQLite >= 3.25 rewrites index/trigger/view references on RENAME TABLE / RENAME COLUMN.
--
-- History stays as written: decisions.persona / context_entries.produced_by /
-- routine_runs.job = 'scout' and reason_code 'scout_candidate' are append-only rows; the
-- readers map them to Sweep through arc.journal.legacy, using the cutover instant below.

ALTER TABLE scout_batches RENAME TO sweep_batches;
DROP INDEX IF EXISTS idx_scout_batches_run;
CREATE INDEX IF NOT EXISTS idx_sweep_batches_run ON sweep_batches(run_id);
-- Stage-2 batches were written as stage 'scout'; sweep_batches is an audit table without an
-- append-only trigger, so the stage label follows the rename (stage 1 stays 'digest').
UPDATE sweep_batches SET stage = 'sweep' WHERE stage = 'scout';

ALTER TABLE raw_docs RENAME COLUMN scout_status TO sweep_status;
ALTER TABLE raw_docs RENAME COLUMN scouted_at TO swept_at;
ALTER TABLE raw_docs RENAME COLUMN scout_run_id TO sweep_run_id;
DROP INDEX IF EXISTS idx_raw_docs_unscouted;
CREATE INDEX IF NOT EXISTS idx_raw_docs_unswept ON raw_docs(swept_at);

-- Dispatcher cursors: cursor:scout -> cursor:sweep, cursor:scout.overnight -> cursor:sweep.overnight.
-- missed:scout:* alert keys are history and stay.
INSERT OR REPLACE INTO routine_state (key, value, updated_at)
    SELECT 'cursor:sweep' || substr(key, length('cursor:scout') + 1), value, updated_at
    FROM routine_state
    WHERE key IN ('cursor:scout', 'cursor:scout.overnight');
DELETE FROM routine_state WHERE key IN ('cursor:scout', 'cursor:scout.overnight');

-- The cutover instant (fixed-width UTC, arc.context.ttl.to_db format). Rows journaled
-- before it with persona/job 'scout' are the Sweep; after it 'scout' is the slow-feed
-- Scout persona (E5.13). Written once: a re-run never moves it.
INSERT OR IGNORE INTO routine_state (key, value, updated_at)
    VALUES ('rename:scout_to_sweep',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');
