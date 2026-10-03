-- 019_experiments.sql — Forward A/B experiment registry (E10.1, D44).
--
-- experiments: one append-only row per spec revision of an experiment
-- (`X-<n>`). A draft may be revised (a new row, revision + 1); once the
-- experiment leaves draft (registered or queued) its spec is hash-locked and a
-- new revision is refused, here by trigger and in arc.experiments.store: a
-- changed experiment needs a new id. `spec` is the canonical JSON of the
-- validated arc.experiments.models.ExperimentSpec, `spec_hash` its SHA-256.
--
-- experiment_events: the append-only status log. The current status of an
-- experiment is its latest event: draft -> registered | queued -> running ->
-- stopped(reason) -> promoted | rejected. `detail` is JSON (running: t0,
-- t0_equity, legacy_book, control_sha, config_hashes; stopped: sigma, note).
--
-- arm_id on the trading/audit tables: which experiment arm produced the row.
-- NULL means control (every row written before E10, and every control-arm row);
-- the treatment runner (E10.2) writes '<experiment id>:treatment'-style ids.

CREATE TABLE IF NOT EXISTS experiments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,           -- X-<n>
    revision       INTEGER NOT NULL,
    spec_version   INTEGER NOT NULL,
    area           TEXT NOT NULL CHECK (area IN ('entries', 'exits', 'ranking', 'sizing', 'other')),
    kind           TEXT NOT NULL CHECK (kind IN ('aa', 'ab')),
    spec           TEXT NOT NULL,           -- canonical ExperimentSpec JSON
    spec_hash      TEXT NOT NULL,           -- sha256 hex of `spec`
    actor          TEXT NOT NULL,
    created_at     TEXT NOT NULL,           -- to_db() UTC text
    UNIQUE (experiment_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_experiments_id ON experiments(experiment_id, revision);

CREATE TRIGGER IF NOT EXISTS experiments_no_update
BEFORE UPDATE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiments is append-only');
END;

CREATE TRIGGER IF NOT EXISTS experiments_no_delete
BEFORE DELETE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiments is append-only');
END;

CREATE TABLE IF NOT EXISTS experiment_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN
                       ('draft', 'queued', 'registered', 'running', 'stopped',
                        'promoted', 'rejected')),
    reason         TEXT CHECK (reason IS NULL OR reason IN
                       ('win', 'harm', 'futility', 'owner', 'invalid')),
    spec_hash      TEXT NOT NULL,           -- the spec revision this event is about
    actor          TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '{}',
    at             TEXT NOT NULL            -- to_db() UTC text
);

CREATE INDEX IF NOT EXISTS idx_experiment_events_id ON experiment_events(experiment_id, id);

CREATE TRIGGER IF NOT EXISTS experiment_events_no_update
BEFORE UPDATE ON experiment_events
BEGIN
    SELECT RAISE(ABORT, 'experiment_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS experiment_events_no_delete
BEFORE DELETE ON experiment_events
BEGIN
    SELECT RAISE(ABORT, 'experiment_events is append-only');
END;

-- Pre-registration lock: no new spec revision once the experiment left draft.
CREATE TRIGGER IF NOT EXISTS experiments_locked_after_register
BEFORE INSERT ON experiments
WHEN EXISTS (
    SELECT 1 FROM experiment_events
    WHERE experiment_id = NEW.experiment_id AND status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'experiment spec is locked after registration (use a new id)');
END;

ALTER TABLE run_manifests ADD COLUMN arm_id TEXT;
ALTER TABLE proposals ADD COLUMN arm_id TEXT;
ALTER TABLE decisions ADD COLUMN arm_id TEXT;
ALTER TABLE outcomes ADD COLUMN arm_id TEXT;
ALTER TABLE pnl_snapshots ADD COLUMN arm_id TEXT;
ALTER TABLE executions ADD COLUMN arm_id TEXT;
