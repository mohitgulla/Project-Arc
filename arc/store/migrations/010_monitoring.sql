-- 010_monitoring.sql — Heartbeats and ops alerts (E8.2).
--
-- heartbeats: one append-only row per liveness event. Components:
--   'tick'    every `arc routines tick` (outcome counts, correlation ids)
--   'health'  every `arc health check` (gateway + routine window results)
-- The correlation JSON carries tick_id, cron job, kanban task and Hermes
-- session ids so a Slack post, a log line and a routine_runs row join up.
--
-- ops_alerts: one row per distinct problem (dedupe key). An open alert is
-- posted once; it is resolved (and the resolution posted) when the check
-- passes again. A later recurrence opens a new row.

CREATE TABLE IF NOT EXISTS heartbeats (
    id           TEXT PRIMARY KEY,
    component    TEXT NOT NULL,             -- tick | health
    status       TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'failed')),
    at           TEXT NOT NULL,             -- to_db() UTC text
    correlation  TEXT NOT NULL DEFAULT '{}',-- JSON: tick_id, cron_job, kanban_task, ...
    detail       TEXT NOT NULL DEFAULT '{}' -- JSON: counts / check results
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_component ON heartbeats(component, at);

CREATE TRIGGER IF NOT EXISTS heartbeats_no_update
BEFORE UPDATE ON heartbeats
BEGIN
    SELECT RAISE(ABORT, 'heartbeats is append-only');
END;

CREATE TRIGGER IF NOT EXISTS heartbeats_no_delete
BEFORE DELETE ON heartbeats
BEGIN
    SELECT RAISE(ABORT, 'heartbeats is append-only');
END;

CREATE TABLE IF NOT EXISTS ops_alerts (
    id           TEXT PRIMARY KEY,
    key          TEXT NOT NULL,             -- e.g. missed:director:<slot>, gateway, tick_stale
    kind         TEXT NOT NULL,             -- missed_window | tick_stale | gateway_down | ...
    message      TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    resolved_at  TEXT,
    posted_ts    TEXT,                      -- Slack ts of the alert post, when posted
    correlation  TEXT NOT NULL DEFAULT '{}'
);

-- At most one open alert per key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_alerts_open
    ON ops_alerts(key) WHERE resolved_at IS NULL;
