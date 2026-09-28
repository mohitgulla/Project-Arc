-- 004_channel_briefs.sql — per-channel processors (E4.4, PLAN D14).

-- -------------------------------------------------------------------
-- raw_docs: YouTube channel id + title so the right processor is picked.
-- -------------------------------------------------------------------
ALTER TABLE raw_docs ADD COLUMN channel_id TEXT;
ALTER TABLE raw_docs ADD COLUMN title TEXT;

-- -------------------------------------------------------------------
-- Channel briefs. A brief is ``active`` from publish until the channel's
-- next brief supersedes it, and never past ``expires_at`` (hard cap in
-- trading sessions from applies_to_session, per channel cadence).
-- At most one active brief per channel (partial unique index).
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_briefs (
    id                  TEXT PRIMARY KEY,           -- ChannelBrief.brief_id
    channel_slug        TEXT NOT NULL,
    channel_id          TEXT,
    video_id            TEXT NOT NULL,
    video_url           TEXT NOT NULL,
    raw_doc_id          TEXT,
    published_at        TEXT NOT NULL,              -- ISO-8601, America/New_York
    applies_to_session  TEXT NOT NULL,              -- YYYY-MM-DD
    expires_at          TEXT NOT NULL,              -- ISO-8601, America/New_York
    guidelines_version  TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'expired')),
    superseded_by       TEXT,
    brief_json          TEXT NOT NULL,              -- validated ChannelBrief
    kept                INTEGER NOT NULL DEFAULT 0,
    dropped             TEXT NOT NULL DEFAULT '[]', -- JSON [{section, reason, item}]
    model               TEXT NOT NULL,
    prompt_sha256       TEXT NOT NULL,
    raw_response        TEXT,                       -- verbatim LLM output (audit only)
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (channel_slug, video_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_briefs_one_active
    ON channel_briefs(channel_slug) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_channel_briefs_status ON channel_briefs(status, expires_at);
