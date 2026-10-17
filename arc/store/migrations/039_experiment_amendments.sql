-- 039_experiment_amendments.sql — owner amendment of a registered XP's analysis block
-- (E25.10, PLAN D122).
--
-- The pre-registration lock (019) refuses every new spec revision once an experiment
-- left draft. D122 opens exactly one audited path: `arc experiment amend-analysis`
-- appends a revision whose `amends_revision` names the latest revision it replaces and
-- whose spec differs from that revision ONLY in the `analysis` key. Nothing is
-- rewritten: the registration row and hash stay, the table stays append-only (the
-- no-update / no-delete triggers are untouched), and the amendment is also written to
-- experiment_events by arc.experiments.store in the same transaction. Rows inserted
-- before this migration (and every create of a draft) carry amends_revision NULL.

ALTER TABLE experiments ADD COLUMN amends_revision INTEGER;

-- 1. The lock now refuses only a plain (non-amendment) revision after registration.
DROP TRIGGER IF EXISTS experiments_locked_after_register;

CREATE TRIGGER IF NOT EXISTS experiments_locked_after_register
BEFORE INSERT ON experiments
WHEN NEW.amends_revision IS NULL AND EXISTS (
    SELECT 1 FROM experiment_events
    WHERE experiment_id = NEW.experiment_id AND status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'experiment spec is locked after registration (use a new id)');
END;

-- 2. An amendment: the experiment is registered or running (not draft, not queued, not
--    concluded), it follows the latest revision directly, and its spec equals that
--    revision's with the `analysis` key removed from both (same area, kind, version).
CREATE TRIGGER IF NOT EXISTS experiments_amendment_analysis_only
BEFORE INSERT ON experiments
WHEN NEW.amends_revision IS NOT NULL AND (
    NOT EXISTS (
        SELECT 1 FROM experiment_events
        WHERE experiment_id = NEW.experiment_id AND status <> 'draft'
    )
    OR (
        SELECT status FROM experiment_events
        WHERE experiment_id = NEW.experiment_id ORDER BY id DESC LIMIT 1
    ) NOT IN ('registered', 'running')
    OR NEW.amends_revision IS NOT (
        SELECT MAX(revision) FROM experiments WHERE experiment_id = NEW.experiment_id
    )
    OR NEW.revision IS NOT NEW.amends_revision + 1
    OR NOT EXISTS (
        SELECT 1 FROM experiments p
        WHERE p.experiment_id = NEW.experiment_id
          AND p.revision = NEW.amends_revision
          AND p.area = NEW.area
          AND p.kind = NEW.kind
          AND p.spec_version = NEW.spec_version
          AND json_remove(p.spec, '$.analysis') = json_remove(NEW.spec, '$.analysis')
    )
)
BEGIN
    SELECT RAISE(ABORT, 'experiment amendment may change only the analysis block of the latest revision of a registered, not concluded experiment');
END;
