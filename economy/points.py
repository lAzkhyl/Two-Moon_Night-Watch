# =====
# MODULE: economy/points.py
# =====
# Architecture Overview:
# Core mathematical engine for the bot's point economy.
# Handles awarding points and implements Just-In-Time (JIT) decay —
# decay is evaluated mathematically on read rather than via a cron job,
# keeping the DB immune to scale constraints.
#
# DECAY ENGINE v2 (FIX-ECO-001):
# calculate_decay() now reads the dynamic `decay.zones_config` JSONB array
# instead of the deprecated flat keys (decay.zone1_days, decay.rate_zone1 …).
# The old flat keys are retained as a fallback for guilds that have not yet
# run migration 006.
#
# zones_config schema (each element):
#   {
#     "zone_id":      int,    -- sort key; zones are walked in ascending order
#     "label":        str,    -- human-readable name, e.g. "Zone 1"
#     "duration_days": int,   -- days this zone lasts; -1 means infinite (terminal)
#     "rate_per_day":  float  -- points lost per day while in this zone
#   }
# =====

import json
from datetime import datetime, timezone

from db.pool import get_pool
from config.manager import get_all_config


def calculate_decay(raw_points: float, last_active_at: datetime, config: dict[str, str]) -> float:
    # -----
    # Just-In-Time Decay Engine v2
    # Dynamically parses the `decay.zones_config` JSON array and walks through
    # each zone sequentially to compute cumulative point loss.
    #
    # Zone traversal logic:
    #   1. Subtract grace period from total inactive days.
    #   2. For each zone (sorted by zone_id ascending):
    #        a. If duration_days == -1 → terminal/infinite zone: consume all
    #           remaining days at this rate and stop.
    #        b. Otherwise: consume min(remaining_days, zone.duration_days) days
    #           at zone.rate_per_day, then advance to the next zone.
    #   3. Any leftover inactive days beyond the last zone are ignored (no zone
    #      to absorb them), so the last zone should typically be infinite (-1).
    # -----
    now = datetime.now(timezone.utc)
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)

    days_inactive = (now - last_active_at).days
    if days_inactive <= 0:
        return max(0.0, raw_points)

    grace = int(config.get("decay.grace_days", 7))
    remaining = days_inactive - grace
    if remaining <= 0:
        # Still inside grace window — no decay applied.
        return max(0.0, raw_points)

    # ------------------------------------------------------------------
    # Parse dynamic zones from JSONB config key (migration 006+).
    # Fall back to legacy flat keys for guilds not yet migrated.
    # ------------------------------------------------------------------
    zones_raw = config.get("decay.zones_config")
    zones: list[dict] = []

    if zones_raw:
        try:
            parsed = json.loads(zones_raw)
            if isinstance(parsed, list) and parsed:
                zones = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if not zones:
        # Legacy flat-key fallback — preserves backwards compatibility.
        zones = [
            {
                "zone_id": 1,
                "duration_days": int(config.get("decay.zone1_days", 7)),
                "rate_per_day":  float(config.get("decay.rate_zone1", 5.0)),
            },
            {
                "zone_id": 2,
                "duration_days": int(config.get("decay.zone2_days", 7)),
                "rate_per_day":  float(config.get("decay.rate_zone2", 15.0)),
            },
            {
                "zone_id": 3,
                "duration_days": -1,  # infinite terminal zone
                "rate_per_day":  float(config.get("decay.rate_zone3", 30.0)),
            },
        ]

    # Sort zones by zone_id so they are always processed in the intended order.
    zones = sorted(zones, key=lambda z: int(z.get("zone_id", 0)))

    total_decay = 0.0
    for zone in zones:
        if remaining <= 0:
            break

        dur  = int(zone.get("duration_days", -1))
        rate = float(zone.get("rate_per_day", 0.0))

        if dur == -1:
            # Terminal (infinite) zone — absorbs all remaining inactive days.
            total_decay += remaining * rate
            remaining = 0
        else:
            days_in_zone = min(remaining, dur)
            total_decay  += days_in_zone * rate
            remaining    -= days_in_zone

    return max(0.0, raw_points - total_decay)


def _get_duration_multiplier(t_event: int, tiers_json: str) -> float:
    """Internal helper: parses duration tier multipliers for long-running events."""
    try:
        tiers = json.loads(tiers_json)
    except Exception:
        return 1.0
    for t in sorted(tiers, key=lambda x: x["max"]):
        if t_event <= t["max"]:
            return float(t["mult"])
    return 1.0


def calculate_event_points(t_participant: int, t_event: int, config: dict[str, str]) -> float:
    # -----
    # Payout Yield Algorithm
    # Computes point yields by evaluating how much of the event the user
    # participated in versus the total runtime of the event.
    # -----
    join_bonus = float(config.get("ec.join_bonus", 15))
    comp_bonus = float(config.get("ec.completion_bonus", 10))
    base_max   = float(config.get("ec.base_max_bonus", 50))
    t_cap      = int(config.get("ec.t_cap", 120))

    ratio = t_participant / min(max(t_event, 1), t_cap)
    ratio = min(1.0, ratio)

    mult    = _get_duration_multiplier(t_event, config.get("ec.duration_tiers", "[]"))
    dyn_max = base_max * mult

    return join_bonus + (ratio * dyn_max) + comp_bonus


async def award(guild_id: str, discord_id: str, amount: float) -> None:
    """Adds raw points to a user's balance and resets the decay grace clock."""
    if amount <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (guild_id, discord_id, raw_points, last_active_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id, discord_id)
            DO UPDATE SET raw_points = users.raw_points + $3, last_active_at = NOW()
            """,
            guild_id, str(discord_id), float(amount),
        )


async def deduct(guild_id: str, discord_id: str, amount: float) -> bool:
    """
    Removes points from a user's balance.
    Enforces a floor of 0 — will not go negative.
    Returns False when the user row does not exist.
    """
    if amount <= 0:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE users SET raw_points = GREATEST(0, raw_points - $3)
            WHERE guild_id=$1 AND discord_id=$2
            """,
            guild_id, str(discord_id), float(amount),
        )
    return res != "UPDATE 0"


async def get_effective_points(guild_id: str, discord_id: str) -> float:
    """Retrieves a user's true net-worth by applying JIT decay to raw balance."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT raw_points, last_active_at FROM users WHERE guild_id=$1 AND discord_id=$2",
            guild_id, str(discord_id),
        )
    if not row:
        return 0.0
    config = await get_all_config(guild_id)
    return calculate_decay(row["raw_points"], row["last_active_at"], config)


async def distribute_event_points(event_id: str) -> None:
    # -----
    # Executed by the background vote_resolver in cogs/gamenight.py.
    # Pulls all VC session fragments and condenses them into unified
    # payout yields for every participant.
    # CVE-2M-008-B: Host is excluded from participant payout.
    # -----
    pool = await get_pool()
    async with pool.acquire() as conn:
        evt = await conn.fetchrow(
            "SELECT guild_id, host_id, started_at, ended_at FROM events WHERE event_id=$1",
            event_id,
        )
        if not evt or not evt["ended_at"]:
            return

        sessions = await conn.fetch(
            "SELECT user_id, join_time, leave_time, afk_consumed_minutes FROM vc_sessions WHERE event_id=$1",
            event_id,
        )

    if not sessions:
        return

    guild_id = evt["guild_id"]
    host_id  = evt["host_id"]
    t_evt    = int((evt["ended_at"] - evt["started_at"]).total_seconds() / 60.0)
    config   = await get_all_config(guild_id)

    for s in sessions:
        # CVE-2M-008-B: Host receives points via award_host_points, not here.
        if s["user_id"] == host_id:
            continue

        jt   = s["join_time"]
        lt   = s["leave_time"] or evt["ended_at"]
        dur  = int((lt - jt).total_seconds() / 60.0)
        t_part = max(0, dur - int(s["afk_consumed_minutes"] or 0))

        pts = calculate_event_points(t_part, t_evt, config)
        await award(guild_id, s["user_id"], pts)