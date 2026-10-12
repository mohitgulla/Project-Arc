-- 037_arm_code.sql — shared-account attribution (E15.2, D69).
--
-- arm_identity gains:
--   arm_code         the arm's 1-char base62 code (``[0-9A-Za-z]``), allocated at t0.
--                    A shared-mode arm sends ``arc2.<code>.<token rest>.s<k>``; the
--                    code is unique among the arms on the same account whose
--                    attribution windows [t0, stop] overlap, and is reused only once
--                    its arm is stopped and flat (arc.experiments.attribution).
--                    NULL on a dedicated arm and on every store started before E15.2.
--   account_cash_t0  the broker account's cash at the arm's t0 (Decimal text): the
--                    book invariant's cash baseline is the earliest live arm's value.
-- Written once with the identity (the no-update / no-delete triggers stay).
ALTER TABLE arm_identity ADD COLUMN arm_code TEXT
    CHECK (arm_code IS NULL OR (length(arm_code) = 1 AND arm_code GLOB '[0-9A-Za-z]'));
ALTER TABLE arm_identity ADD COLUMN account_cash_t0 TEXT;

-- virtual_ledger.kind gains the shared-mode rows (E15.2). Fill rows of a shared
-- arm keep kind 'fill' with ref ``act:<FILL activity id>`` (never colliding with the
-- dedicated ``<order>:<symbol>:<cum qty>`` refs); new kinds:
--   fee      the arm's share of an Alpaca FEE activity (ref ``fee:<activity id>``);
--            detail.method = 'execution_id' (joined to a FILL) or 'pro_rata'
--   event    an option NTA (OPEXP / OPASN / OPEXC / OPTRD) attributed by holdings
--            (ref ``evt:<activity id>:<symbol>``)
-- Rebuilt like 034: ids and refs unchanged, index and append-only triggers recreated.
CREATE TABLE virtual_ledger_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    arm_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
                    ('open', 'legacy_hold', 'legacy_release', 'fill', 'adjust',
                     'fee', 'event')),
    ref         TEXT NOT NULL,
    amount      TEXT NOT NULL,          -- Decimal text, dollars
    settles_on  TEXT,                   -- YYYY-MM-DD (fill, adjust, fee, event)
    at          TEXT NOT NULL,          -- to_db() UTC text
    detail      TEXT NOT NULL DEFAULT '{}'
);

INSERT INTO virtual_ledger_new (id, arm_id, kind, ref, amount, settles_on, at, detail)
    SELECT id, arm_id, kind, ref, amount, settles_on, at, detail FROM virtual_ledger;

DROP TABLE virtual_ledger;
ALTER TABLE virtual_ledger_new RENAME TO virtual_ledger;

CREATE UNIQUE INDEX IF NOT EXISTS idx_virtual_ledger_ref ON virtual_ledger(arm_id, kind, ref);

CREATE TRIGGER IF NOT EXISTS virtual_ledger_no_update
BEFORE UPDATE ON virtual_ledger
BEGIN
    SELECT RAISE(ABORT, 'virtual_ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS virtual_ledger_no_delete
BEFORE DELETE ON virtual_ledger
BEGIN
    SELECT RAISE(ABORT, 'virtual_ledger is append-only');
END;
