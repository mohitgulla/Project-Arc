-- 032_expiry_guard.sql — expiry guard, opens-only halts, assignment shares (E11.4, D73).
--
-- halts.scope: 'all' (today's halt: opens and closes stop) or 'opens' (new opens
-- stop, exits keep running; raised by arc:expiry). Existing rows stay 'all'.
-- open_structures.exit_attempts_day: close proposals made on exit_day (the expiry
-- guard allows up to expiry_guard.attempts_per_day inside the closing window).
-- assignment_shares: a share position Arc expects after a classified exercise or
-- assignment, until the owner unwinds it at the broker (the unwind fill is
-- attributed to the row). Append-only except the unwind columns; never deleted.

ALTER TABLE halts ADD COLUMN scope TEXT NOT NULL DEFAULT 'all'
    CHECK (scope IN ('all', 'opens'));

ALTER TABLE open_structures ADD COLUMN exit_attempts_day INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS assignment_shares (
    id             TEXT PRIMARY KEY,
    structure_id   TEXT NOT NULL REFERENCES open_structures(id),
    root           TEXT NOT NULL,
    qty            INTEGER NOT NULL CHECK (qty != 0),   -- signed shares (+ long / - short)
    via            TEXT NOT NULL CHECK (via IN ('activity', 'inferred')),
    from_occ       TEXT NOT NULL,                       -- comma-separated OCC legs
    basis          TEXT NOT NULL,                       -- per-share Decimal text (the settle)
    detected_at    TEXT NOT NULL,                       -- to_db() UTC text
    unwound_at     TEXT,
    unwind_price   TEXT,
    evidence_json  TEXT NOT NULL,
    run_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignment_shares_open ON assignment_shares(root, unwound_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_shares_structure
    ON assignment_shares(structure_id);

CREATE TRIGGER IF NOT EXISTS assignment_shares_only_unwind
BEFORE UPDATE ON assignment_shares
WHEN NEW.id IS NOT OLD.id OR NEW.structure_id IS NOT OLD.structure_id
  OR NEW.root IS NOT OLD.root OR NEW.qty IS NOT OLD.qty OR NEW.via IS NOT OLD.via
  OR NEW.from_occ IS NOT OLD.from_occ OR NEW.basis IS NOT OLD.basis
  OR NEW.detected_at IS NOT OLD.detected_at OR NEW.evidence_json IS NOT OLD.evidence_json
  OR NEW.run_id IS NOT OLD.run_id OR OLD.unwound_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'assignment_shares: only an open row''s unwind columns may change');
END;
CREATE TRIGGER IF NOT EXISTS assignment_shares_no_delete
BEFORE DELETE ON assignment_shares
BEGIN
    SELECT RAISE(ABORT, 'assignment_shares rows are never deleted');
END;
