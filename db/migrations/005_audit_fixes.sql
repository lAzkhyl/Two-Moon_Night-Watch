-- =====
-- MIGRATION 005: Audit Remediation
-- Fixes: CVE-2M-001 (schema mismatch), CVE-2M-002 (user_inventory UNIQUE),
--        CVE-2M-003 (FK events.host_id), CVE-2M-014 (vc_session duplication)
-- All statements are idempotent (safe to run multiple times).
-- =====

-- CVE-2M-001: Simplify 'votes' table to use single vote_value column
ALTER TABLE votes DROP COLUMN IF EXISTS raw_score;
ALTER TABLE votes DROP COLUMN IF EXISTS tier_weight;
ALTER TABLE votes DROP COLUMN IF EXISTS weighted_score;
ALTER TABLE votes ADD COLUMN IF NOT EXISTS vote_value INTEGER NOT NULL DEFAULT 3;

-- CVE-2M-001: Rename event_ratings.final_weighted_score -> rating_score
-- (Idempotent: only renames if the old column still exists)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'event_ratings' AND column_name = 'final_weighted_score'
    ) THEN
        ALTER TABLE event_ratings RENAME COLUMN final_weighted_score TO rating_score;
    END IF;
END $$;

-- CVE-2M-002: Clean up any duplicate rows before adding UNIQUE constraint
DELETE FROM user_inventory a USING user_inventory b
WHERE a.id < b.id
  AND a.guild_id = b.guild_id
  AND a.user_id  = b.user_id
  AND a.item_id  = b.item_id;

-- CVE-2M-002: Add UNIQUE constraint on user_inventory (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_inventory_user_item'
    ) THEN
        ALTER TABLE user_inventory
            ADD CONSTRAINT uq_inventory_user_item UNIQUE (guild_id, user_id, item_id);
    END IF;
END $$;

-- CVE-2M-003: Drop FK constraint on events.host_id (idempotent via IF EXISTS)
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_host_id_fkey;

-- CVE-2M-014: Partial unique index to prevent duplicate open VC sessions
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_vc_session
    ON vc_sessions (event_id, user_id) WHERE leave_time IS NULL;

-- BUG FIX: Add UNIQUE constraint on event_ratings.event_id (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_event_ratings_event_id'
    ) THEN
        ALTER TABLE event_ratings
            ADD CONSTRAINT uq_event_ratings_event_id UNIQUE (event_id);
    END IF;
END $$;