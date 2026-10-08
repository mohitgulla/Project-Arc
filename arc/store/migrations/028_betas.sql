-- 028_betas.sql — daily beta vs SPY per ticker (E3.6, D62).
--
-- Written once a day before the open by the `betas` routine (and `arc betas refresh`):
-- beta = cov(r_name, r_SPY) / var(r_SPY) over the last `window` (252) aligned daily log
-- returns. `beta` is NULL when fewer than 120 aligned returns exist; readers then use
-- 1.0. The gate never reads this table: arc.betas.lookup turns the latest fresh row
-- into the floored beta (max(beta, 1.0)) that the pipeline hands the gate.
-- One row per (ticker, day); a re-run of the same day upserts that day's row.
CREATE TABLE IF NOT EXISTS betas (
    ticker      TEXT NOT NULL,
    day         TEXT NOT NULL,              -- ET date the row was computed for, YYYY-MM-DD
    beta        REAL,                       -- raw (unfloored) beta; NULL = too few days
    n_days      INTEGER NOT NULL,           -- aligned daily returns used
    window      INTEGER NOT NULL,           -- target window (252)
    benchmark   TEXT NOT NULL,              -- 'SPY'
    as_of       TEXT NOT NULL,              -- last aligned close date used, YYYY-MM-DD
    source      TEXT NOT NULL,              -- 'alpaca'
    created_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, day)
);

CREATE INDEX IF NOT EXISTS idx_betas_day ON betas(day);
