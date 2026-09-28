-- 008_approvals.sql — Slack proposal cards + approval requests (E6.1).

-- -------------------------------------------------------------------
-- One approval request per proposal: the card posted in the #arc-investor
-- day thread and its lifecycle. The decision itself is the `approvals` row
-- (ApprovalRecord, 001) written in the same transaction that closes the
-- request, so a request resolves at most once.
--
-- status:
--   pending         card is live with Approve / Reject buttons
--   approved        an allowed approver clicked Approve before the TTL
--   rejected        an allowed approver clicked Reject
--   expired         TTL passed with no decision (expired = rejected)
--   not_actionable  posted for information only: the gate failed, or the
--                   proposal carries no gate token (dry run / fixtures)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_requests (
    proposal_hash   TEXT PRIMARY KEY REFERENCES proposals(proposal_hash),
    ticker          TEXT NOT NULL,
    day             TEXT NOT NULL,          -- YYYY-MM-DD, America/New_York
    proposal_json   TEXT NOT NULL,          -- the exact Proposal (hash re-checked on load)
    status          TEXT NOT NULL CHECK (status IN
                        ('pending', 'approved', 'rejected', 'expired', 'not_actionable')),
    reason          TEXT NOT NULL DEFAULT '',
    channel         TEXT NOT NULL,          -- Slack channel id, or 'log' when not posted
    thread_ts       TEXT,                   -- day thread root
    message_ts      TEXT,                   -- the card message
    expires_at      TEXT NOT NULL,          -- to_db() UTC text
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,                   -- Slack user id, or arc:ttl / arc:auto-approve
    approval_id     TEXT REFERENCES approvals(id),
    run_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_pending
    ON approval_requests(status, expires_at);

-- At most one decision per proposal, whatever path writes it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_proposal ON approvals(proposal_hash);
