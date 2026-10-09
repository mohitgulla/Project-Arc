-- 029_chain_scoped_steps.sql — chain steps shared by two chains (E13.21a).
--
-- `exits.mandatory` and `broker.execute` are steps of both the `research` loop chain
-- and the `positions.evaluate` chain (AUTO_CHAINS, D56). At a :20/:50 slot both
-- chains run in the same tick, and the single (job, scheduled_for) key made the
-- second chain's shared step a `duplicate`, which stopped that chain.
--
-- The scheduled-run key splits in two:
--   * a root run (step_index 0, i.e. a job dispatched on its own) stays unique per
--     (job, scheduled_for): a re-dispatched chain or a doubled tick is still a no-op;
--   * a chain step (step_index > 0) is unique per (job, scheduled_for, chain_run_id).
-- Whether a chain step runs once per slot across chains (exits.mandatory) or once per
-- chain (broker.execute publishes its own chain's proposals) is decided by the
-- dispatcher's claim (RoutineRunRepo.claim(scope=...)), atomically in one INSERT.
-- Existing rows already satisfy both keys (the old one was stricter).

DROP INDEX IF EXISTS idx_routine_runs_slot;

CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_runs_slot_root
    ON routine_runs(job, scheduled_for) WHERE event_id IS NULL AND step_index = 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_runs_slot_step
    ON routine_runs(job, scheduled_for, chain_run_id)
    WHERE event_id IS NULL AND step_index > 0;
