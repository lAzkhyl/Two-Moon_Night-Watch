CREATE TABLE IF NOT EXISTS bot_instances (
    instance_id     TEXT PRIMARY KEY,
    guild_id        TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'STANDBY',   -- LEADER | STANDBY
    hostname        TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    force_shutdown  BOOLEAN NOT NULL DEFAULT FALSE
);
