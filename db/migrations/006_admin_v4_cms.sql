-- =====
-- MIGRATION 006: Admin V4 CMS & Dynamic Point Zones
-- Purpose: In-App Embed CMS for /admin panel
--
-- This migration seeds the bot_config table with default JSON structures
-- for all admin panel embeds. Each embed's title, description, color, and
-- thumbnail URL are stored in the database and can be edited live via
-- the /owner dashboard using Discord Modals.
--
-- Also migrates "Dynamic Point Zones" decay system into JSONB config key.
-- =====

-- -----------------------------------------------------------------------------
-- SECTION 1: EMBED CONFIGURATION SEEDS
-- -----------------------------------------------------------------------------
-- Each embed is stored as a JSON object with keys:
--   { "title": "...", "description": "...", "color": 0xRRGGBB, "thumbnail": "url" }
-- Keys follow pattern: embed.admin.<category>

-- Main Menu Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.main',
    json_build_object(
        'title', 'ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ',
        'description', '**{state_indicator} {state}**\n\n### Authorization:\n**Admin:**\n> {admin_roles}\n\n**Mod:**\n> {mod_roles}\n{audit_line}',
        'color', 16711680,
        'thumbnail', 'https://i.imgur.com/THUMBNAIL_ADMIN.gif'
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.main' AND guild_id = '__DEFAULT__'
);

-- Main Menu Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.main_guide',
    json_build_object(
        'title', 'ɴᴀᴠɪɢᴀᴛɪᴏɴ ɢᴜɪᴅᴇ',
        'description', '⚙️ Economy\n> Award rates for joining & completing events.\n> Tune join bonus, completion bonus, point cap,\n> and the time window counted toward scaling.\n\n⏱️ Decay\n> Controls how quickly idle users lose points.\n> Set grace period, three zone durations,\n> and the per-day loss rate for each zone.\n\n🎮 Host\n> Rules governing who can host and how often.\n> Cooldown, min duration, voter threshold,\n> income multiplier, and reputation window.\n\n🗳️ Vote\n> Post-event host reputation system.\n> Configure the voting window and the score\n> weight assigned to each vote type.\n\n📡 Channels\n> Bind bot features to specific channels.\n> Gamenight text, activity feed,\n> and the VC category for event rooms.\n\n👁️ View All\n> Export every config key to a .txt file.\n> Useful for auditing, backup review,\n> or diagnosing unexpected bot behaviour.\n\n🔧 System\n> Master on/off switch for the bot.\n> ACTIVE enables all public commands;\n> PAUSED blocks them while admin still works.\n\n⚡ Force\n> Direct point balance manipulation. Mod+ only.\n> Award or deduct any amount from any user\n> by entering their ID or @mention.',
        'color', 16777215,
        'thumbnail', 'https://i.imgur.com/THUMBNAIL_NAV.gif'
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.main_guide' AND guild_id = '__DEFAULT__'
);

-- Economy Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.economy',
    json_build_object(
        'title', '⚙️ ᴇᴄᴏɴᴏᴍʏ',
        'description', '**Join Bonus** — `{join_bonus} pts`\n**Completion Bonus** — `{completion_bonus} pts`\n**Max Bonus** — `{max_bonus} pts`\n**Time Cap** — `{time_cap} min`',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.economy' AND guild_id = '__DEFAULT__'
);

-- Economy Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.economy_guide',
    json_build_object(
        'title', '⚙️ ᴇᴄᴏɴᴏᴍʏ ɢᴜɪᴅᴇ',
        'description', '**Join Bonus** — Points awarded to each participant at the moment they join.\n**Completion Bonus** — Extra points distributed when the host ends the event normally.\n**Max Bonus** — Hard ceiling on total points a single user can earn per event.\n**Time Cap** — Maximum event minutes counted toward time-based bonus scaling.\n\n> Adjust **Max Bonus** first if the economy feels inflationary.\n> Lower **Time Cap** to reduce the edge gained by running very long events.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.economy_guide' AND guild_id = '__DEFAULT__'
);

-- Decay Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.decay',
    json_build_object(
        'title', '⏱️ ᴅᴇᴄᴀʏ',
        'description', '**Grace Period** — `{grace_days} days`\n**Zone 1** — `{zone1_days} days` · `{rate_zone1} pts/day`\n**Zone 2** — `{zone2_days} days` · `{rate_zone2} pts/day`\n**Zone 3** — indefinite · `{rate_zone3} pts/day`',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.decay' AND guild_id = '__DEFAULT__'
);

-- Decay Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.decay_guide',
    json_build_object(
        'title', '⏱️ ᴅᴇᴄᴀʏ ɢᴜɪᴅᴇ',
        'description', 'Inactivity decay runs daily. A user''s points erode once the grace period expires.\n\n**Grace Period** — Days of inactivity before any decay begins.\n**Zone 1 / Zone 2 Duration** — How many days each phase lasts before advancing.\n**Zone 3** begins after Zone 1 + Zone 2 has elapsed and runs indefinitely.\n\n**Rate Zone 1–3** — Points lost per day in each zone. Zone 3 is the steepest.\n\n> Any server activity from the user resets the clock back to the grace period.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.decay_guide' AND guild_id = '__DEFAULT__'
);

-- Host Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.host',
    json_build_object(
        'title', '🎮 ʜᴏꜱᴛ',
        'description', '**Cooldown** — `{cooldown_hours} hours`\n**Min Duration** — `{min_duration} min`\n**Min Voters** — `{min_voters}`\n**Income Multiplier** — `{income_multiplier}×`\n**Rolling Window** — `{rolling_window} events`\n**Outlier Trim** — `{outlier_trim} votes`',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.host' AND guild_id = '__DEFAULT__'
);

-- Host Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.host_guide',
    json_build_object(
        'title', '🎮 ʜᴏꜱᴛ ɢᴜɪᴅᴇ',
        'description', '**Cooldown** — Hours a host must wait before they can open another event.\n**Min Duration** — Events shorter than this threshold don''t count toward host rewards.\n**Min Voters** — Minimum vote submissions required to update host reputation.\n**Income Multiplier** — Host earns this multiple of the standard participant reward.\n**Rolling Window** — Number of recent events averaged to compute reputation score.\n**Outlier Trim** — Votes above this per-event count are discarded to prevent brigading.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.host_guide' AND guild_id = '__DEFAULT__'
);

-- Vote Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.vote',
    json_build_object(
        'title', '🗳️ ᴠᴏᴛᴇ',
        'description', '**Vote Window** — `{window_minutes} min`\n**Score Positive** — `{score_positive} pts`\n**Score Neutral** — `{score_neutral} pts`\n**Score Negative** — `{score_negative} pts`',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.vote' AND guild_id = '__DEFAULT__'
);

-- Vote Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.vote_guide',
    json_build_object(
        'title', '🗳️ ᴠᴏᴛᴇ ɢᴜɪᴅᴇ',
        'description', 'Voting opens immediately after an event ends and closes once the window expires.\n\n**Vote Window** — Minutes the vote poll stays open after event close.\n**Score Positive** — Reputation points added per 👍 vote.\n**Score Neutral** — Reputation points added per 😐 vote.\n**Score Negative** — Reputation points added per 👎 vote.\n\n> Scores feed into the host''s rolling reputation average.\n> Lower scores don''t lock a host out — they reduce priority and income multiplier.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.vote_guide' AND guild_id = '__DEFAULT__'
);

-- Channel Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.channel',
    json_build_object(
        'title', '📡 ᴄʜᴀɴɴᴇʟꜱ',
        'description', '**Gamenight** — {gamenight_channel}\n**Activity** — {activity_channel}\n**VC Category** — {vc_category}',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.channel' AND guild_id = '__DEFAULT__'
);

-- Channel Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.channel_guide',
    json_build_object(
        'title', '📡 ᴄʜᴀɴɴᴇʟ ɢᴜɪᴅᴇ',
        'description', '**Gamenight Channel** — Text channel for event announcements, join calls, and results.\n**Activity Channel** — General notification feed: milestones, leaderboards, bot events.\n**VC Category** — Voice category where temporary event rooms are created and destroyed.\n\n> All three must be assigned for events to function correctly.\n> Changes apply immediately — no restart required.\n> Use the dropdowns below to select channels.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.channel_guide' AND guild_id = '__DEFAULT__'
);

-- System Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.system',
    json_build_object(
        'title', '🔧 ꜱʏꜱᴛᴇᴍ',
        'description', 'Current state: {state_indicator}\n\n**▶️ ACTIVE** — All public-facing commands are enabled.\n**⏸️ PAUSED** — Public commands are blocked server-wide.',
        'color', 16763904,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.system' AND guild_id = '__DEFAULT__'
);

-- System Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.system_guide',
    json_build_object(
        'title', '🔧 ꜱʏꜱᴛᴇᴍ ɢᴜɪᴅᴇ',
        'description', '**▶️ ACTIVE** — Users can join events, check balances, use the shop, and vote. Full functionality.\n**⏸️ PAUSED** — Public commands are blocked. Admins can still configure settings, run backups, and manage the economy.\n\n> Switching to PAUSED is recommended during maintenance or unexpected issues.\n> No data is lost — the bot simply stops accepting public input.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.system_guide' AND guild_id = '__DEFAULT__'
);

-- Force Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.force',
    json_build_object(
        'title', '⚡ ꜰᴏʀᴄᴇ',
        'description', 'Direct point manipulation for moderators.\n\n**Current Action:** {action_type}\n**Target User:** {target_user}\n**Amount:** {amount} pts\n\n> Use this tool sparingly. All actions are logged.',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.force' AND guild_id = '__DEFAULT__'
);

-- Force Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.force_guide',
    json_build_object(
        'title', '⚡ ꜰʀᴄᴇ ɢᴜɪᴅᴇ',
        'description', '**Award Points** — Grant points to a user for special contributions.\n**Deduct Points** — Remove points due to rule violations or corrections.\n\n> Requires Mod role or higher.\n> Enter user ID or @mention in the modal.\n> All force actions are recorded in the audit log.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.force_guide' AND guild_id = '__DEFAULT__'
);

-- -----------------------------------------------------------------------------
-- SECTION 2: DYNAMIC POINT ZONES CONFIGURATION
-- -----------------------------------------------------------------------------
-- Migrates decay zones from individual keys to a unified JSONB structure
-- for more flexible zone management.

INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'decay.zones_config',
    json_build_array(
        json_build_object('zone_id', 1, 'label', 'Zone 1', 'duration_days', 7, 'rate_per_day', 5.0),
        json_build_object('zone_id', 2, 'label', 'Zone 2', 'duration_days', 7, 'rate_per_day', 15.0),
        json_build_object('zone_id', 3, 'label', 'Zone 3', 'duration_days', -1, 'rate_per_day', 30.0)
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'decay.zones_config' AND guild_id = '__DEFAULT__'
);

-- -----------------------------------------------------------------------------
-- SECTION 3: SHOP EMBED SEEDS
-- -----------------------------------------------------------------------------

-- Shop Management Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.shop',
    json_build_object(
        'title', '🛒 ꜱʜᴏᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ',
        'description', '**Active Items:** `{item_count}`\n**Black Market Slots:** `{blackmarket_slots}`\n\n> Manage shop items, pricing, and availability.',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.shop' AND guild_id = '__DEFAULT__'
);

-- Shop Guide Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.shop_guide',
    json_build_object(
        'title', '🛒 ꜱʜᴏᴘ ɢᴜɪᴅᴇ',
        'description', '**Add New Item** — Create a new shop item with custom name, price, and type.\n**Manage Item** — Edit existing items, adjust pricing, set rarity.\n**Black Market** — Toggle black market status for items (coming soon).\n\n> Item types: consumable, role, rental, permanent\n> Black market items appear randomly in the shop rotation.',
        'color', 16777215,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.shop_guide' AND guild_id = '__DEFAULT__'
);

-- Item Control Panel Embed
INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
SELECT
    '__DEFAULT__',
    'embed.admin.item_control',
    json_build_object(
        'title', '📦 ɪᴛᴇᴍ ᴄᴏɴᴛʀᴏʟ',
        'description', '**Item:** `{item_name}`\n**Price:** `{cost} pts`\n**Type:** `{item_type}`\n**Rarity:** `{rarity}`\n**Black Market:** `{blackmarket_status}`\n\n**Description:**\n{description}',
        'color', 3447003,
        'thumbnail', NULL
    )::text,
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM bot_config WHERE config_key = 'embed.admin.item_control' AND guild_id = '__DEFAULT__'
);

-- -----------------------------------------------------------------------------
-- SECTION 4: CREATE INDEX FOR EMBED LOOKUPS
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_bot_config_embed_keys
ON bot_config (config_key)
WHERE config_key LIKE 'embed.admin.%';