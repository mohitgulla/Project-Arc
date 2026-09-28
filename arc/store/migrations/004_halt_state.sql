-- 004_halt_state.sql — Kill switch + daily halt state (E3.3).
--
-- Reshapes `halts` to the E3.3 contract: (reason, actor, at, cleared_at),
-- plus who cleared it, what kind of halt it is, and the trading session it
-- was raised in (daily-loss auto-halt fires at most once per session).
-- A row with cleared_at IS NULL is an active halt.

ALTER TABLE halts RENAME COLUMN halted_at TO at;
ALTER TABLE halts RENAME COLUMN resumed_at TO cleared_at;
ALTER TABLE halts ADD COLUMN cleared_by TEXT;
ALTER TABLE halts ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'
    CHECK (kind IN ('manual', 'daily_loss'));
ALTER TABLE halts ADD COLUMN session_date TEXT;  -- ISO date (ET) of the session it was raised in

CREATE INDEX IF NOT EXISTS idx_halts_active ON halts(cleared_at);
CREATE INDEX IF NOT EXISTS idx_halts_kind_session ON halts(kind, session_date);
