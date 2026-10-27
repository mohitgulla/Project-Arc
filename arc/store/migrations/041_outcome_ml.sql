-- 041_outcome_ml.sql — the shadow ML scores at entry on every outcome row (E27.4, D126).
--
-- `ml_p_win`, `ml_e_r` and `ml_p_stop` are COPIED from the `ml_scores` context entry
-- the proposal's chain wrote (the proposal's own legs), never joined at read time, so
-- a later model version can't rewrite history. `ml_model_version` names the M2 version
-- that produced them. NULL = shadow scoring off, unavailable, or a pre-E27.4 proposal.
-- Read by `arc ml monitor` (live calibration). Context only: no gate, sizing or ranking
-- input reads these columns.
ALTER TABLE outcomes ADD COLUMN ml_p_win REAL;
ALTER TABLE outcomes ADD COLUMN ml_e_r REAL;
ALTER TABLE outcomes ADD COLUMN ml_p_stop REAL;
ALTER TABLE outcomes ADD COLUMN ml_model_version TEXT;
