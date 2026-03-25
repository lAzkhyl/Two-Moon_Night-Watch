CREATE TABLE backup_log (
    backup_id      TEXT        PRIMARY KEY,
    guild_id       TEXT        NOT NULL,
    backup_type    TEXT        NOT NULL,
    initiated_by   TEXT        NOT NULL,
    config_keys    INTEGER     NOT NULL,
    channel_msg_id TEXT,
    checksum       TEXT        NOT NULL,
    is_full_backup BOOLEAN     DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_backup_guild_time ON backup_log (guild_id, created_at DESC);