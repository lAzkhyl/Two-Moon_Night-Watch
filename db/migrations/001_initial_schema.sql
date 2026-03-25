CREATE TABLE bot_config (
    guild_id     TEXT          NOT NULL,
    config_key   TEXT          NOT NULL,
    config_value TEXT          NOT NULL,
    updated_at   TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (guild_id, config_key)
);

CREATE TABLE config_audit_log (
    log_id     BIGSERIAL   PRIMARY KEY,
    guild_id   TEXT        NOT NULL,
    changed_by TEXT        NOT NULL,
    config_key TEXT        NOT NULL,
    old_value  TEXT,
    new_value  TEXT        NOT NULL,
    changed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_guild_time ON config_audit_log (guild_id, changed_at DESC);

CREATE TABLE users (
    discord_id     TEXT            NOT NULL,
    guild_id       TEXT            NOT NULL,
    raw_points     NUMERIC(12, 2)  DEFAULT 0,
    last_active_at TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (discord_id, guild_id)
);

CREATE TABLE events (
    event_id   TEXT        PRIMARY KEY,
    guild_id   TEXT        NOT NULL,
    host_id    TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    game       TEXT,
    thread_id  TEXT,
    vc_id      TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at   TIMESTAMPTZ,
    is_valid   SMALLINT    DEFAULT 0,
    FOREIGN KEY (host_id, guild_id) REFERENCES users (discord_id, guild_id)
);

CREATE INDEX idx_events_guild_host ON events (guild_id, host_id);

CREATE TABLE vc_sessions (
    session_id           BIGSERIAL       PRIMARY KEY,
    event_id             TEXT            NOT NULL REFERENCES events (event_id),
    user_id              TEXT            NOT NULL,
    join_time            TIMESTAMPTZ     NOT NULL,
    leave_time           TIMESTAMPTZ,
    afk_consumed_minutes NUMERIC(8, 2)   DEFAULT 0
);

CREATE INDEX idx_vc_event_user ON vc_sessions (event_id, user_id);

CREATE TABLE event_ratings (
    id                   BIGSERIAL       PRIMARY KEY,
    guild_id             TEXT            NOT NULL,
    host_id              TEXT            NOT NULL,
    event_id             TEXT            NOT NULL REFERENCES events (event_id),
    final_weighted_score NUMERIC(5, 3)   NOT NULL,
    voter_count          INTEGER         NOT NULL,
    created_at           TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX idx_ratings_host ON event_ratings (host_id, guild_id, created_at DESC);

CREATE TABLE votes (
    vote_id        BIGSERIAL       PRIMARY KEY,
    event_id       TEXT            NOT NULL REFERENCES events (event_id),
    voter_id       TEXT            NOT NULL,
    raw_score      INTEGER         NOT NULL,
    tier_weight    NUMERIC(4, 2)   NOT NULL,
    weighted_score NUMERIC(8, 3)   NOT NULL,
    voted_at       TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (event_id, voter_id)
);

CREATE TABLE shop_items (
    item_id        TEXT    NOT NULL,
    guild_id       TEXT    NOT NULL,
    label          TEXT    NOT NULL,
    description    TEXT,
    cost           INTEGER NOT NULL,
    item_type      TEXT    NOT NULL,
    duration_days  INTEGER,
    role_id        TEXT,
    is_blackmarket BOOLEAN DEFAULT FALSE,
    stock          INTEGER,
    is_active      BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (item_id, guild_id)
);

CREATE TABLE user_inventory (
    id          BIGSERIAL   PRIMARY KEY,
    guild_id    TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    item_id     TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

CREATE TABLE bet_pools (
    pool_id    TEXT            PRIMARY KEY,
    event_id   TEXT            NOT NULL,
    guild_id   TEXT            NOT NULL,
    status     TEXT            DEFAULT 'open',
    total_pot  NUMERIC(12, 2)  DEFAULT 0,
    tax_amount NUMERIC(12, 2)  DEFAULT 0,
    created_at TIMESTAMPTZ     DEFAULT NOW()
);

CREATE TABLE bet_entries (
    entry_id BIGSERIAL       PRIMARY KEY,
    pool_id  TEXT            NOT NULL,
    user_id  TEXT            NOT NULL,
    amount   NUMERIC(12, 2)  NOT NULL,
    target   TEXT            NOT NULL,
    UNIQUE (pool_id, user_id)
);

CREATE TABLE bounties (
    bounty_id   TEXT            PRIMARY KEY,
    event_id    TEXT            NOT NULL,
    guild_id    TEXT            NOT NULL,
    sponsor_id  TEXT            NOT NULL,
    amount      NUMERIC(12, 2)  NOT NULL,
    description TEXT,
    status      TEXT            DEFAULT 'active',
    winner_id   TEXT,
    created_at  TIMESTAMPTZ     DEFAULT NOW()
);