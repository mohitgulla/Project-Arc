-- 023_iv_daily.sql — daily 30-day constant-maturity IV per underlying (E4.12, D55).
--
-- One row per (ticker, session day, source). Sources:
--   alpaca_cm30       forward: `iv.record` at 15:50 ET from the Alpaca chain (7-75 DTE),
--                     ATM IV per expiry, total-variance interpolation to 30 DTE
--   alpaca_backfill   reconstructed by `arc iv backfill` from Alpaca option daily bars
--                     (Black-Scholes inversion of the ATM call + put closes, same
--                     30-DTE interpolation)
--   optionstrategist  the McMillan weekly file (`arc iv import-optionstrategist`, ad hoc,
--                     internal use only): cur_iv + its days/percentile + hv20/50/100.
--                     Never mixed into our series (arc.iv.store.SERIES_SOURCES).
-- Append-only per key; a re-run of the same day upserts that day's row.
-- iv30 / hv* / ext_percentile are decimals (0.20 = 20 vol, 0.18 = 18th percentile).
CREATE TABLE IF NOT EXISTS iv_daily (
    ticker          TEXT NOT NULL,
    day             TEXT NOT NULL,              -- session date (ET), YYYY-MM-DD
    iv30            REAL NOT NULL CHECK (iv30 > 0),
    method          TEXT NOT NULL,              -- chain_cm30 | bars_bs_cm30 | os_cur_iv
    source          TEXT NOT NULL
                    CHECK (source IN ('alpaca_cm30', 'alpaca_backfill', 'optionstrategist')),
    spot            REAL,                       -- spot used (underlying close for backfill/OS)
    spot_basis      TEXT,                       -- mid | last_close
    n_contracts     INTEGER,                    -- contracts with an IV (forward) / legs used
    hv20            REAL,                       -- optionstrategist only
    hv50            REAL,
    hv100           REAL,
    ext_days        INTEGER,                    -- optionstrategist: IV readings behind the percentile
    ext_percentile  REAL,                       -- optionstrategist: cur_iv percentile (0..1)
    detail          TEXT NOT NULL DEFAULT '{}', -- JSON: bracketing expiries, legs, notes
    created_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, day, source)
);

CREATE INDEX IF NOT EXISTS idx_iv_daily_source_day ON iv_daily(source, day);

-- Days the backfill could not reconstruct (no traded ATM leg, failed inversion, ...),
-- kept so a re-run is resumable and the skip reasons are auditable.
CREATE TABLE IF NOT EXISTS iv_skips (
    ticker          TEXT NOT NULL,
    day             TEXT NOT NULL,
    source          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, day, source)
);
