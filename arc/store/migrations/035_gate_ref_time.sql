-- 035_gate_ref_time.sql — which clock a gate decision aged its quotes against (E10.2d).
--
-- An experiment arm replays the paired control chain's market tape (E10.2), up to
-- experiments.runner.max_lag_seconds after control, so on the arm's wall clock its
-- quotes were always older than control's (XP-10: 7 gate:stale_data failures at
-- 61-83 s that control passed). The gate now ages a replayed quote against the
-- paired chain's clock. This column records which clock the decision used:
--   wall          every leg quote aged against the gate's now (control; live reads)
--   paired_chain  every leg replayed from the paired control chain's tape
--   mixed         some legs replayed, some read live on a tape miss
-- Existing rows predate the rule and were all aged on the wall clock.
ALTER TABLE gate_decisions ADD COLUMN ref_time TEXT NOT NULL DEFAULT 'wall'
    CHECK (ref_time IN ('wall', 'paired_chain', 'mixed'));
