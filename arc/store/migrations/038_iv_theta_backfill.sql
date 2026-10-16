-- 038_iv_theta_backfill.sql — iv_daily gains the `theta_backfill` source (E7.13, D84).
--
--   theta_backfill    IV30 per ticker-day from the cached ThetaData quoted chains
--                     (NBBO mids, 2020 -> now): `arc iv backfill --source theta` seeds it
--                     from data/option_stats/daily (E7.16, the live cm30 code), and the
--                     nightly `history.topup` appends each new session (same path).
-- Part of our series; same-day precedence comes from ArcSettings.iv_series_sources
-- (default alpaca_cm30 > theta_backfill > alpaca_backfill).
--
-- SQLite cannot alter a CHECK, so the table is rebuilt: rows, primary key and index
-- unchanged. iv_skips has no CHECK on source and is left alone.
CREATE TABLE iv_daily_new (
    ticker          TEXT NOT NULL,
    day             TEXT NOT NULL,
    iv30            REAL NOT NULL CHECK (iv30 > 0),
    method          TEXT NOT NULL,
    source          TEXT NOT NULL
                    CHECK (source IN ('alpaca_cm30', 'alpaca_backfill', 'optionstrategist',
                                      'theta_backfill')),
    spot            REAL,
    spot_basis      TEXT,
    n_contracts     INTEGER,
    hv20            REAL,
    hv50            REAL,
    hv100           REAL,
    ext_days        INTEGER,
    ext_percentile  REAL,
    detail          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, day, source)
);

INSERT INTO iv_daily_new
    (ticker, day, iv30, method, source, spot, spot_basis, n_contracts, hv20, hv50, hv100,
     ext_days, ext_percentile, detail, created_at)
SELECT ticker, day, iv30, method, source, spot, spot_basis, n_contracts, hv20, hv50, hv100,
       ext_days, ext_percentile, detail, created_at
FROM iv_daily;

DROP TABLE iv_daily;
ALTER TABLE iv_daily_new RENAME TO iv_daily;
CREATE INDEX IF NOT EXISTS idx_iv_daily_source_day ON iv_daily(source, day);
