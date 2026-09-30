-- 018_event_runs.sql — approval events: dispatch-once semantics (E6.2d).
--
-- 1. routine_runs: an event-triggered run is unique per (job, event_id), not per
--    (job, scheduled_for). Two approvals created in the same second used to share
--    a slot, so the second was recorded 'duplicate' and never executed. Scheduled
--    slots keep their (job, scheduled_for) key. SQLite cannot drop a table-level
--    UNIQUE constraint, so the table is rebuilt (same columns + event_id).
-- 2. routine_events: dispatched_at / dispatched_by. The D34 `execute` step claims
--    each event it hands to an Investor subprocess before spawning it, so the same
--    tick's event drain never runs that ladder a second time, inline.

PRAGMA foreign_keys = OFF;

CREATE TABLE routine_runs_new (
    run_id          TEXT PRIMARY KEY,
    job             TEXT NOT NULL,
    chain_run_id    TEXT,
    step_index      INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL,          -- schedule | every | event:<name> | manual
    scheduled_for   TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT NOT NULL
                    CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
    attempts        INTEGER NOT NULL DEFAULT 1,
    inputs_snapshot TEXT,                   -- JSON array of context_snapshots.id
    outputs         TEXT NOT NULL DEFAULT '[]',  -- JSON array of context_entries.id
    summary         TEXT,
    error           TEXT,
    config_version  INTEGER,                -- 014: config_changes.id the run executed under
    event_id        TEXT                    -- routine_events.id when event-triggered
);

INSERT INTO routine_runs_new
    (run_id, job, chain_run_id, step_index, reason, scheduled_for, started_at, finished_at,
     status, attempts, inputs_snapshot, outputs, summary, error, config_version, event_id)
SELECT run_id, job, chain_run_id, step_index, reason, scheduled_for, started_at, finished_at,
       status, attempts, inputs_snapshot, outputs, summary, error, config_version, NULL
FROM routine_runs;

DROP TABLE routine_runs;
ALTER TABLE routine_runs_new RENAME TO routine_runs;

CREATE INDEX IF NOT EXISTS idx_routine_runs_chain ON routine_runs(chain_run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_routine_runs_sched ON routine_runs(scheduled_for);
-- Scheduled / manual runs: one per (job, slot), so a duplicate tick is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_runs_slot
    ON routine_runs(job, scheduled_for) WHERE event_id IS NULL;
-- Event-triggered runs: one per (job, event), whatever the event's timestamp.
CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_runs_event
    ON routine_runs(job, event_id) WHERE event_id IS NOT NULL;

ALTER TABLE routine_events ADD COLUMN dispatched_at TEXT;   -- D34 execute claimed it
ALTER TABLE routine_events ADD COLUMN dispatched_by TEXT;   -- the execute step's run_id

PRAGMA foreign_keys = ON;
