-- 004_routines.sql — Context store + routine dispatcher (E5.4, D16).

-- -------------------------------------------------------------------
-- Context entries: the shared, append-only memory between agents.
-- Timestamps are fixed-width UTC text (YYYY-MM-DDTHH:MM:SS.ffffffZ) so
-- string comparison == time comparison.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_entries (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    subject         TEXT NOT NULL,          -- ticker | 'market' | source name
    payload         TEXT NOT NULL,          -- JSON, validated by the kind's pydantic model
    schema_version  INTEGER NOT NULL,
    produced_by     TEXT NOT NULL,          -- job name
    run_id          TEXT,
    chain_run_id    TEXT,
    created_at      TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    expires_at      TEXT,                   -- NULL = never expires
    supersedes_id   TEXT REFERENCES context_entries(id),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'superseded', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_context_active
    ON context_entries(status, kind, subject, valid_from);
CREATE INDEX IF NOT EXISTS idx_context_chain ON context_entries(chain_run_id);

-- Append-only: rows are never deleted, and the only permitted update is the
-- status moving from 'active' to 'superseded' or 'expired'.
CREATE TRIGGER IF NOT EXISTS context_entries_no_delete
BEFORE DELETE ON context_entries
BEGIN
    SELECT RAISE(ABORT, 'context_entries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS context_entries_status_only
BEFORE UPDATE ON context_entries
WHEN NEW.id IS NOT OLD.id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.subject IS NOT OLD.subject
  OR NEW.payload IS NOT OLD.payload
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.produced_by IS NOT OLD.produced_by
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.chain_run_id IS NOT OLD.chain_run_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.valid_from IS NOT OLD.valid_from
  OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.supersedes_id IS NOT OLD.supersedes_id
  OR OLD.status != 'active'
  OR NEW.status = 'active'
BEGIN
    SELECT RAISE(ABORT, 'context_entries is append-only (status active -> superseded|expired only)');
END;

-- -------------------------------------------------------------------
-- Context snapshots: exactly which entries an agent read (audit/replay).
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_snapshots (
    id          TEXT PRIMARY KEY,
    as_of       TEXT NOT NULL,
    kinds       TEXT NOT NULL,              -- JSON array ([] = all kinds)
    subjects    TEXT NOT NULL,              -- JSON array ([] = all subjects)
    entry_ids   TEXT NOT NULL,              -- JSON array of context_entries.id
    run_id      TEXT,
    created_at  TEXT NOT NULL
);

-- -------------------------------------------------------------------
-- Routine runs: one row per (job, scheduled slot). The unique key is what
-- stops a double tick from running a job twice.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routine_runs (
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
    UNIQUE (job, scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_routine_runs_chain ON routine_runs(chain_run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_routine_runs_sched ON routine_runs(scheduled_for);

-- -------------------------------------------------------------------
-- Routine events: external events (approval, halt, ...) queued for the next
-- tick. In-process events (<job>.completed) never touch this table.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routine_events (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    consumed_at  TEXT,
    consumed_by  TEXT                       -- JSON array of run_ids started by it
);

CREATE INDEX IF NOT EXISTS idx_routine_events_pending ON routine_events(consumed_at);

-- -------------------------------------------------------------------
-- Dispatcher state: per-job due cursors, day-thread ts, pending source notes.
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routine_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
