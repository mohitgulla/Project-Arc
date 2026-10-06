-- 024_sweep_to_scalp.sql — D56 (E13.1): the 30-min doc reader Sweep is renamed Scalp and
-- the ranking persona Director is renamed Research. Copy of 022 (D54).
--
-- Mechanical rename of the DB identifiers; no row is added, dropped or rewritten except
-- the dispatcher cursors (moved so the Scalp does not re-read every doc and the loop
-- does not re-fire missed slots after deploy) and the audit-only batch stage label.
-- SQLite >= 3.25 rewrites index/trigger/view references on RENAME TABLE / RENAME COLUMN.
--
-- History stays as written: decisions.persona / context_entries.produced_by /
-- routine_runs.job / persona_calls.persona = 'sweep' | 'director' and reason codes
-- 'sweep_candidate' / 'director_*' are append-only rows; the readers map them through
-- arc.journal.legacy.RENAME_CHAIN, using the cutover instant below. Stored doc status
-- values (scouted, skipped_*, brief_only, filtered, slow_feed) are unchanged.

ALTER TABLE sweep_batches RENAME TO scalp_batches;
DROP INDEX IF EXISTS idx_sweep_batches_run;
CREATE INDEX IF NOT EXISTS idx_scalp_batches_run ON scalp_batches(run_id);
-- Stage-2 batches were written as stage 'sweep'; scalp_batches is an audit table without
-- an append-only trigger, so the stage label follows the rename (stage 1 stays 'digest').
UPDATE scalp_batches SET stage = 'scalp' WHERE stage = 'sweep';

ALTER TABLE raw_docs RENAME COLUMN sweep_status TO scalp_status;
ALTER TABLE raw_docs RENAME COLUMN swept_at TO scalped_at;
ALTER TABLE raw_docs RENAME COLUMN sweep_run_id TO scalp_run_id;
DROP INDEX IF EXISTS idx_raw_docs_unswept;
CREATE INDEX IF NOT EXISTS idx_raw_docs_unscalped ON raw_docs(scalped_at);

-- Dispatcher cursors: cursor:sweep -> cursor:scalp, cursor:sweep.overnight ->
-- cursor:scalp.overnight, cursor:director -> cursor:research. missed:* alert keys are
-- history and stay.
INSERT OR REPLACE INTO routine_state (key, value, updated_at)
    SELECT 'cursor:scalp' || substr(key, length('cursor:sweep') + 1), value, updated_at
    FROM routine_state
    WHERE key IN ('cursor:sweep', 'cursor:sweep.overnight');
INSERT OR REPLACE INTO routine_state (key, value, updated_at)
    SELECT 'cursor:research', value, updated_at
    FROM routine_state
    WHERE key = 'cursor:director';
DELETE FROM routine_state
    WHERE key IN ('cursor:sweep', 'cursor:sweep.overnight', 'cursor:director');

-- The cutover instant (fixed-width UTC, arc.context.ttl.to_db format). Rows journaled
-- before it as 'sweep' are the Scalp and as 'director' are Research. One key serves both
-- renames. Written once: a re-run never moves it.
INSERT OR IGNORE INTO routine_state (key, value, updated_at)
    VALUES ('rename:sweep_to_scalp',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z',
            strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');
