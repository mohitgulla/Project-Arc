-- 040_correlations.sql — daily pairwise 60-session return correlations (E28.4, D131).
--
-- Written once a day before the open by the `betas` routine (same daily-bars fetch;
-- `arc betas refresh` too): for every pair among the day's tickers (active list ∪ open
-- underlyings ∪ today's candidates ∪ SPY/QQQ/IWM), the Pearson correlation of the last
-- `window` (60) aligned daily log returns of split/dividend-adjusted closes taken
-- strictly before `day`. A pair with fewer than 40 aligned returns (or a flat series)
-- gets no row. Stored once per pair with a < b; a re-run of the same day replaces that
-- day's rows. Read through arc.betas.corr_store (the E28.5 cluster cap's input, the
-- `arc correlations show` CLI) and synced into experiment arms as of the chain (D106).
CREATE TABLE IF NOT EXISTS correlations (
    day         TEXT NOT NULL,              -- ET date the row was computed for, YYYY-MM-DD
    a           TEXT NOT NULL,              -- ticker, a < b
    b           TEXT NOT NULL,
    corr        REAL NOT NULL,              -- Pearson correlation of daily log returns
    n_obs       INTEGER NOT NULL,           -- aligned daily returns used (40..window)
    window      INTEGER NOT NULL,           -- target window (60)
    as_of       TEXT NOT NULL,              -- last aligned close date used, YYYY-MM-DD
    source      TEXT NOT NULL,              -- 'alpaca'
    created_at  TEXT NOT NULL,
    PRIMARY KEY (day, a, b)                 -- writer stores a < b (arc.betas.corr.pair_key)
);

CREATE INDEX IF NOT EXISTS idx_correlations_a ON correlations(a, day);
CREATE INDEX IF NOT EXISTS idx_correlations_b ON correlations(b, day);
