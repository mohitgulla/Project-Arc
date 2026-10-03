-- 020_experiment_reports.sql — daily experiment evaluations (E10.3, D44).
--
-- One append-only row per evaluation of a running experiment: the full
-- arc.experiments.evaluate.ExperimentReport as canonical JSON (`payload`), its
-- SHA-256 (`report_hash`), and the provenance columns a reviewer filters on
-- (verdict, sessions, spec hash, config hash, git shas). The latest row per
-- experiment is the current report (`arc experiment report <id> --stored`).
-- A verdict other than `continue` also stopped the experiment (experiment_events).

CREATE TABLE IF NOT EXISTS experiment_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    report_version INTEGER NOT NULL,
    evaluated_at   TEXT NOT NULL,           -- to_db() UTC text
    as_of_day      TEXT,                    -- ET day of the last paired session (NULL: none yet)
    sessions       INTEGER NOT NULL,        -- paired sessions in the primary series
    verdict        TEXT NOT NULL CHECK (verdict IN
                       ('continue', 'win', 'futility', 'invalid')),
    spec_hash      TEXT NOT NULL,
    config_hash    TEXT NOT NULL,           -- sha256 of the effective experiments config
    control_sha    TEXT,                    -- git sha recorded at t0 (control arm)
    treatment_sha  TEXT,                    -- git sha of the latest treatment-arm manifest
    evaluator_sha  TEXT,                    -- git sha of the code that evaluated
    payload        TEXT NOT NULL,           -- canonical ExperimentReport JSON
    report_hash    TEXT NOT NULL,           -- sha256 of `payload`
    run_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_experiment_reports_id ON experiment_reports(experiment_id, id);

CREATE TRIGGER IF NOT EXISTS experiment_reports_no_update
BEFORE UPDATE ON experiment_reports
BEGIN
    SELECT RAISE(ABORT, 'experiment_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS experiment_reports_no_delete
BEFORE DELETE ON experiment_reports
BEGIN
    SELECT RAISE(ABORT, 'experiment_reports is append-only');
END;

-- Per-arm input the evaluation reads that E10.1 did not tag: positions_snapshots
-- (the control arm's legacy-book marks). NULL = control, as for the E10.1
-- columns; the treatment runner (E10.2) writes '<experiment id>:treatment'.
ALTER TABLE positions_snapshots ADD COLUMN arm_id TEXT;
