-- 013_config_changes.sql — D26 control panel (E8.5).
--
-- config_changes: append-only log of every runtime config override. The effective
-- value of a key is its latest row (highest id); `status` says whether that row
-- set a value (applied) or undid an earlier change (reverted, `supersedes_id`
-- points at the undone row). `old` / `new` are JSON; `new` NULL + status
-- 'reverted' with no earlier override means "back to the file/env default".
-- The latest id is the config_version every run records.
--
-- config_pending: riskier-direction changes waiting for the owner's confirm
-- (one-time code, TTL). A row is resolved exactly once (confirmed | cancelled |
-- expired); nothing else about it may change and it is never deleted.
--
-- routine_runs.config_version: the config_changes.id a run executed under.
-- NOTE: open PR #48 (E6.4) also adds a 013 (013_swaps.sql): whichever merges second renumbers to 014.

CREATE TABLE IF NOT EXISTS config_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT NOT NULL,
    old            TEXT,                -- JSON (effective value before)
    new            TEXT,                -- JSON (override value after); NULL = default
    actor          TEXT NOT NULL,
    reason         TEXT,
    at             TEXT NOT NULL,       -- to_db() UTC text
    source         TEXT NOT NULL CHECK (source IN ('slack', 'cli')),
    supersedes_id  INTEGER REFERENCES config_changes(id),
    status         TEXT NOT NULL CHECK (status IN ('applied', 'reverted')),
    is_default     INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    direction      TEXT NOT NULL,       -- safer | riskier | neutral
    halted         INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0, 1)),
    pending_id     TEXT                 -- the confirm that released it, if any
);

CREATE INDEX IF NOT EXISTS idx_config_changes_key ON config_changes(key, id);

CREATE TRIGGER IF NOT EXISTS config_changes_no_update
BEFORE UPDATE ON config_changes
BEGIN
    SELECT RAISE(ABORT, 'config_changes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS config_changes_no_delete
BEFORE DELETE ON config_changes
BEGIN
    SELECT RAISE(ABORT, 'config_changes is append-only');
END;

CREATE TABLE IF NOT EXISTS config_pending (
    id           TEXT PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    key          TEXT NOT NULL,
    old          TEXT,
    new          TEXT,
    is_default   INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL CHECK (kind IN ('set', 'revert')),
    supersedes_id INTEGER,
    actor        TEXT NOT NULL,
    reason       TEXT,
    source       TEXT NOT NULL CHECK (source IN ('slack', 'cli')),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    base_version INTEGER NOT NULL,     -- config_version when requested (stale -> refuse)
    resolved_at  TEXT,
    outcome      TEXT CHECK (outcome IN ('confirmed', 'cancelled', 'expired')),
    resolved_by  TEXT
);

CREATE TRIGGER IF NOT EXISTS config_pending_resolve_once
BEFORE UPDATE ON config_pending
WHEN OLD.resolved_at IS NOT NULL
  OR NEW.id IS NOT OLD.id OR NEW.code IS NOT OLD.code OR NEW.key IS NOT OLD.key
  OR NEW.old IS NOT OLD.old OR NEW.new IS NOT OLD.new OR NEW.actor IS NOT OLD.actor
  OR NEW.kind IS NOT OLD.kind OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.base_version IS NOT OLD.base_version
BEGIN
    SELECT RAISE(ABORT, 'config_pending rows resolve exactly once');
END;

CREATE TRIGGER IF NOT EXISTS config_pending_no_delete
BEFORE DELETE ON config_pending
BEGIN
    SELECT RAISE(ABORT, 'config_pending is never deleted');
END;

ALTER TABLE routine_runs ADD COLUMN config_version INTEGER;
