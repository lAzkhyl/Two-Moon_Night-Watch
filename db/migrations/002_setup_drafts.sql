CREATE TABLE setup_drafts (
    guild_id   TEXT        PRIMARY KEY,
    draft_data JSONB       NOT NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    started_by TEXT        NOT NULL
);