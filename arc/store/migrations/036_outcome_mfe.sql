-- 036_outcome_mfe.sql — max favourable excursion next to MAE (E18.3, D78).
--
-- The best open P&L ($, >= 0) a traded position reached over its stored marks
-- (`position_review` context entries, every 30 min) and its exit fill, so the
-- profit lock / take profit can be judged after the fact. Decimal text, like
-- max_adverse_excursion. Existing rows stay NULL until
-- `arc journal backfill-exit-stats` restates them (a superseding row; outcomes
-- is append-only).
ALTER TABLE outcomes ADD COLUMN max_favourable_excursion TEXT;
