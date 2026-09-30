-- E8.7b (D35): indexes for the tower's Trades list and drill-down.
--
-- The list joins every proposal to its latest gate decision, its structure (open or
-- exit side), its orders and fills. Without these, each per-row lookup is a table
-- scan and a 100k-proposal store takes seconds per page. Indexes only: no data change.

CREATE INDEX IF NOT EXISTS idx_gate_decisions_proposal
    ON gate_decisions(proposal_hash, decided_at);
CREATE INDEX IF NOT EXISTS idx_proposals_day ON proposals(day, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_run ON proposals(run_id);
CREATE INDEX IF NOT EXISTS idx_proposals_chain ON proposals(chain_run_id);
CREATE INDEX IF NOT EXISTS idx_proposals_swap ON proposals(swap_id);
CREATE INDEX IF NOT EXISTS idx_open_structures_exit ON open_structures(exit_proposal_hash);
CREATE INDEX IF NOT EXISTS idx_executions_structure ON executions(structure_id);
CREATE INDEX IF NOT EXISTS idx_orders_proposal ON orders(proposal_hash);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_persona_calls_snapshot ON persona_calls(snapshot_id);

-- Covering index for the list's net EV / PoP / account-profile columns and filters:
-- the expressions must match arc.tower.data_trades._MC exactly, so the list reads
-- them from the index instead of parsing every market_contexts payload.
CREATE INDEX IF NOT EXISTS idx_market_contexts_trade ON market_contexts(
    proposal_hash,
    created_at,
    json_extract(payload, '$.analytics.exit_model.managed.net_ev'),
    json_extract(payload, '$.analytics.exit_model.managed.pop'),
    json_extract(payload, '$.analytics.exit_model.static.pop'),
    json_extract(payload, '$.analytics.account_profile')
);
