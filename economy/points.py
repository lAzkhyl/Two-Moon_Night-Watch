# =====
# MODULE: economy/points.py
# =====
# Architecture Overview:
# This module acts as the core mathematical engine for the bot's point economy.
# It handles the ingestion of points to users and, more importantly, implements
# the "Decay System". 
#
# Performance Details:
# To prevent database locking and excessive writes, point decay is evaluated
# 'just-in-time' (JIT) mathematically in `calculate_decay` rather than running 
# a cron-job to deduct points every single day. This makes the database immune
# to scale constraints.
# =====
import json
from datetime import datetime, timezone

from db.pool import get_pool
from config.manager import get_all_config


def calculate_decay(raw_points: float, last_active_at: datetime, config: dict[str, str]) -> float:
    # -----
    # Just-In-Time Decay Engine
    # Given a user's absolute raw points and the timestamp of their last activity,
    # projects how many points they've lost based on the tiered zone rates configured 
    # in the server settings. Returns the current 'effective' point balance.
    # -----
    now = datetime.now(timezone.utc)
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)
        
    days_inactive = (now - last_active_at).days
    if days_inactive <= 0:
        return max(0.0, raw_points)
        
    # Standard settings fallback
    grace = int(config.get("decay.grace_days", 7))
    z1_days = int(config.get("decay.zone1_days", 7))
    z2_days = int(config.get("decay.zone2_days", 7))
    r1 = float(config.get("decay.rate_zone1", 5.0))
    r2 = float(config.get("decay.rate_zone2", 15.0))
    r3 = float(config.get("decay.rate_zone3", 30.0))

    z1_end = grace + z1_days
    z2_end = z1_end + z2_days

    # Penalty evaluation based on depth of inactivity
    if days_inactive <= grace:
        decay = 0.0
    elif days_inactive <= z1_end:
        decay = (days_inactive - grace) * r1
    elif days_inactive <= z2_end:
        decay = (z1_days * r1) + (days_inactive - z1_end) * r2
    else:
        decay = (z1_days * r1) + (z2_days * r2) + (days_inactive - z2_end) * r3

    return max(0.0, raw_points - float(decay))


def _get_duration_multiplier(t_event: int, tiers_json: str) -> float:
    # Internal helper to parse duration multipliers for events that run long.
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
    base_max = float(config.get("ec.base_max_bonus", 50))
    t_cap = int(config.get("ec.t_cap", 120))
    
    # Calculate participation percentage scaled against the defined cap
    ratio = t_participant / min(max(t_event, 1), t_cap)
    ratio = min(1.0, ratio)
    
    mult = _get_duration_multiplier(t_event, config.get("ec.duration_tiers", "[]"))
    dyn_max = base_max * mult
    
    return join_bonus + (ratio * dyn_max) + comp_bonus


async def award(guild_id: str, discord_id: str, amount: float):
    if amount <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Pushes raw points to DB and resets the decay grace clock immediately
        await conn.execute(
            """
            INSERT INTO users (guild_id, discord_id, raw_points, last_active_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id, discord_id)
            DO UPDATE SET raw_points = users.raw_points + $3, last_active_at = NOW()
            """,
            guild_id, str(discord_id), float(amount)
        )


async def deduct(guild_id: str, discord_id: str, amount: float) -> bool:
    if amount <= 0:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Strict lower-bound enforcement to prevent database constraints from rejecting standard purchases
        res = await conn.execute(
            """
            UPDATE users SET raw_points = GREATEST(0, raw_points - $3)
            WHERE guild_id=$1 AND discord_id=$2
            """,
            guild_id, str(discord_id), float(amount)
        )
        return res != "UPDATE 0"


async def get_effective_points(guild_id: str, discord_id: str) -> float:
    # Retrieves the true net-worth of a user by pulling raw points and applying JIT decay.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT raw_points, last_active_at FROM users WHERE guild_id=$1 AND discord_id=$2",
            guild_id, str(discord_id)
        )
    if not row:
        return 0.0
    config = await get_all_config(guild_id)
    return calculate_decay(row["raw_points"], row["last_active_at"], config)


async def distribute_event_points(event_id: str):
    # -----
    # Executed seamlessly by the background vote_resolver in cogs/gamenight.py
    # Pulls all voice chat session fragments and condenses them into unified 
    # payout yields for every participant.
    # CVE-2M-008-B: Host is excluded from participant payout.
    # -----
    pool = await get_pool()
    async with pool.acquire() as conn:
        evt = await conn.fetchrow(
            "SELECT guild_id, host_id, started_at, ended_at FROM events WHERE event_id=$1",
            event_id
        )
        if not evt or not evt["ended_at"]:
            return
            
        sessions = await conn.fetch(
            "SELECT user_id, join_time, leave_time, afk_consumed_minutes FROM vc_sessions WHERE event_id=$1", 
            event_id
        )
        
    if not sessions:
        return

    guild_id = evt["guild_id"]
    host_id = evt["host_id"]
    t_evt = int((evt["ended_at"] - evt["started_at"]).total_seconds() / 60.0)
    config = await get_all_config(guild_id)
    
    for s in sessions:
        # CVE-2M-008-B: Skip host — they receive points via award_host_points instead
        if s["user_id"] == host_id:
            continue

        jt = s["join_time"]
        lt = s["leave_time"] or evt["ended_at"]
        dur = int((lt - jt).total_seconds() / 60.0)
        
        # Ensure AFK penalties do not cause negative duration logic
        t_part = max(0, dur - int(s["afk_consumed_minutes"] or 0)) 
        
        pts = calculate_event_points(t_part, t_evt, config)
        await award(guild_id, s["user_id"], pts)
