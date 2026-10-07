-- 026_experiment_arm_plan.sql — experiments fork at any persona (E13.12, D56).
--
-- 1. arm_identity.plan: the arm's ArmPlan (JSON: fork_step, arm_personas, arm_jobs,
--    shared_kinds, own_producers), computed at t0 by arc.experiments.runner.arm_plan
--    and written once with the identity. NULL on stores created before E13.12 (e.g.
--    data/arc-exp-XP-3.db): they load with arm_personas = [] (the plan is recomputed
--    from the empty overlay). The no-update / no-delete triggers stay.
ALTER TABLE arm_identity ADD COLUMN plan TEXT;

-- 2. experiments.area gains 'universe' and 'funnel' (arc.experiments.models.Area).
--    SQLite cannot alter a CHECK, so the append-only table is rebuilt with every row
--    (ids, revisions, hashes) unchanged; DROP TABLE fires no delete trigger. Its
--    index and its three triggers are recreated exactly as 019 wrote them.
CREATE TABLE experiments_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,           -- XP-<n>
    revision       INTEGER NOT NULL,
    spec_version   INTEGER NOT NULL,
    area           TEXT NOT NULL CHECK (area IN
                       ('entries', 'exits', 'ranking', 'sizing', 'universe', 'funnel', 'other')),
    kind           TEXT NOT NULL CHECK (kind IN ('aa', 'ab')),
    spec           TEXT NOT NULL,           -- canonical ExperimentSpec JSON
    spec_hash      TEXT NOT NULL,           -- sha256 hex of `spec`
    actor          TEXT NOT NULL,
    created_at     TEXT NOT NULL,           -- to_db() UTC text
    UNIQUE (experiment_id, revision)
);

INSERT INTO experiments_new
    (id, experiment_id, revision, spec_version, area, kind, spec, spec_hash, actor, created_at)
    SELECT id, experiment_id, revision, spec_version, area, kind, spec, spec_hash, actor,
           created_at
    FROM experiments;

DROP TABLE experiments;
ALTER TABLE experiments_new RENAME TO experiments;

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

CREATE TRIGGER IF NOT EXISTS experiments_locked_after_register
BEFORE INSERT ON experiments
WHEN EXISTS (
    SELECT 1 FROM experiment_events
    WHERE experiment_id = NEW.experiment_id AND status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'experiment spec is locked after registration (use a new id)');
END;
