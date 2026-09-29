-- 013_swaps.sql — Close-to-reallocate swaps (E6.4, D19).
--
-- A swap is two linked proposals sharing one swap_id: first a close
-- (kind='close') of an open structure, then, only once that close has FILLED,
-- an open (kind='open') of the candidate that failed the gate for capacity
-- only (per-underlying budget, settled cash, max open positions). If the close
-- does not fill (approval expired/rejected, ladder cancelled, partial fill) the
-- open is cancelled and never proposed. Both proposals go through gate +
-- approval like any other; nothing here submits orders.
--
-- status:
--   vetoed         Risk (or its fail-closed fallback) declined the suggestion
--   closing        the close proposal exists; waiting for its fill
--   open_proposed  the close filled; the open proposal was written (gate + card)
--   cancelled      the close did not fill, or the open no longer qualified
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS swaps (
    id                   TEXT PRIMARY KEY,           -- swap_id (shared by both proposals)
    day                  TEXT NOT NULL,              -- ET day the swap was suggested
    status               TEXT NOT NULL CHECK (status IN
                             ('vetoed', 'closing', 'open_proposed', 'cancelled')),
    detail               TEXT NOT NULL DEFAULT '',
    close_structure_id   TEXT NOT NULL,              -- open_structures.id being closed
    close_ticker         TEXT NOT NULL,
    close_proposal_hash  TEXT,                       -- the close proposal (kind='close')
    open_ticker          TEXT NOT NULL,
    source_ref           TEXT NOT NULL,              -- capacity-rejected source: proposal hash
                                                     -- (gate) or decisions.id (sizing:budget_exhausted)
    open_proposal_hash   TEXT,                       -- the swap's open proposal, once written
    suggestion_json      TEXT NOT NULL,              -- SwapSuggestion (edge, EVs, PoPs, costs)
    run_id               TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_swaps_day ON swaps(day);
CREATE INDEX IF NOT EXISTS idx_swaps_status ON swaps(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_swaps_source ON swaps(source_ref);

-- Proposals written for a swap carry its id. A swap's open re-enters a
-- (day, ticker) slot that the capacity-rejected proposal already holds, so the
-- one-open-per-ticker-per-day key no longer applies to swap opens.
ALTER TABLE proposals ADD COLUMN swap_id TEXT;

DROP INDEX IF EXISTS idx_proposals_day_ticker;
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_day_ticker
    ON proposals(day, ticker) WHERE day IS NOT NULL AND kind = 'open' AND swap_id IS NULL;
