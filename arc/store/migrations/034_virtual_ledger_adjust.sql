-- 034_virtual_ledger_adjust.sql — compensating ledger rows (E10.2c).
--
-- virtual_ledger.kind gains 'adjust': a compensating row appended by
-- `arc experiment repair-ledger` for a broker fill the ledger booked twice
-- (E10.2c: mleg leg `filled_at` microseconds drift between Alpaca calls, so the
-- old fill ref treated a re-fetched fill as new). The ledger stays append-only:
-- nothing is deleted, the duplicate is netted out by an `adjust` row whose ref is
-- `<broker_order_id>:<symbol>:adjust:<last ledger id of the group>`.
--
-- SQLite cannot alter a CHECK, so the table is rebuilt with every row (ids,
-- refs) unchanged; DROP TABLE fires no delete trigger. The unique ref index and
-- both append-only triggers are recreated exactly as 021 wrote them.
CREATE TABLE virtual_ledger_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    arm_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
                    ('open', 'legacy_hold', 'legacy_release', 'fill', 'adjust')),
    ref         TEXT NOT NULL,
    amount      TEXT NOT NULL,          -- Decimal text, dollars
    settles_on  TEXT,                   -- YYYY-MM-DD (fill, adjust)
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
