-- 012_run_manifests.sql — Run manifests (E5.6, D27).
--
-- One append-only row per routine run attempt, for every status (ok, failed,
-- skipped), written by the dispatcher (never by handlers). The payload is a
-- versioned arc.routines.manifest.RunManifest: identity, trigger, timing,
-- session, outcome, environment flags (never secret values), git sha, config
-- hashes, the effective spec and declared I/O contract, input snapshot/digest,
-- external data (as_of + digest), outputs by kind, LLM usage and linked
-- decisions/proposals/gate decisions/Slack posts. A resumed run (attempts + 1)
-- gets a new row; earlier attempts are kept.

CREATE TABLE IF NOT EXISTS run_manifests (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES routine_runs(run_id),
    attempt         INTEGER NOT NULL,
    job             TEXT NOT NULL,
    chain_run_id    TEXT,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped')),
    schema_version  INTEGER NOT NULL,
    payload         TEXT NOT NULL,          -- RunManifest JSON
    created_at      TEXT NOT NULL,          -- to_db() UTC text
    UNIQUE (run_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_run_manifests_chain ON run_manifests(chain_run_id);
CREATE INDEX IF NOT EXISTS idx_run_manifests_job ON run_manifests(job, created_at);

CREATE TRIGGER IF NOT EXISTS run_manifests_no_update
BEFORE UPDATE ON run_manifests
BEGIN
    SELECT RAISE(ABORT, 'run_manifests is append-only');
END;

CREATE TRIGGER IF NOT EXISTS run_manifests_no_delete
BEFORE DELETE ON run_manifests
BEGIN
    SELECT RAISE(ABORT, 'run_manifests is append-only');
END;
