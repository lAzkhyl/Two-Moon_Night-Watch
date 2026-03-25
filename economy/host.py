# =====
# MODULE: economy/host.py
# =====
# Architecture Overview:
# This module processes and computes reputation metrics for Event Hosts. 
# While standard participants earn "Points", Hosts earn "Reputation Stars" 
# derived from post-event voting from actual participants.
#
# Developer Note:
# The `compute_event_rating` function contains an outlier-trimming mechanism
# designed to prevent vote-bombing (malicious users artificially suppressing 
# a host's rating).
# =====
import json
from db.pool import get_pool
from config.manager import get_all_config
from economy.points import award




async def compute_event_rating(pool, event_id: str) -> float | None:
    # -----
    # Rating Finalizer
    # Trims the upper and lower extremes if the vote pool is large enough to
    # prevent malicious review bombing, then averages the remaining pool.
    # -----
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT guild_id, host_id FROM events WHERE event_id=$1", event_id)
        if not row: 
            return None
            
        guild_id, host_id = row["guild_id"], row["host_id"]
        votes = await conn.fetch("SELECT vote_value FROM votes WHERE event_id=$1", event_id)
        
    config = await get_all_config(guild_id)
    min_v = int(config.get("host.min_voters", 5))
    trim_th = int(config.get("host.outlier_trim_threshold", 8))
    
    if len(votes) < min_v:
        return None
        
    vals = sorted([v["vote_value"] for v in votes])
    
    # Trim the top and bottom outlier values to smooth the distribution
    if len(vals) >= trim_th:
        vals = vals[1:-1]
        
    avg = sum(vals) / len(vals)
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            # CVE-2M-001: Include guild_id in INSERT (it is NOT NULL in schema)
            await conn.execute(
                """
                INSERT INTO event_ratings (event_id, guild_id, host_id, rating_score, voter_count)
                VALUES ($1, $2, $3, $4, $5)
                """,
                event_id, guild_id, host_id, float(avg), len(votes)
            )
            # Marks the event strictly valid, allowing points to be claimed
            await conn.execute("UPDATE events SET is_valid=1 WHERE event_id=$1", event_id)
        
    return float(avg)


async def compute_host_reputation(pool, guild_id: str, host_id: str) -> float:
    # -----
    # Evaluates the host's long-term reputation by averaging their most recent 
    # consecutive events via a rolling window.
    # -----
    config = await get_all_config(guild_id)
    win = int(config.get("host.rolling_window", 10))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.event_id, r.rating_score 
            FROM events e 
            JOIN event_ratings r ON e.event_id = r.event_id 
            WHERE e.guild_id=$1 AND e.host_id=$2 AND e.is_valid=1
            ORDER BY e.ended_at DESC LIMIT $3
            """,
            guild_id, str(host_id), win
        )
    if not rows: 
        return 0.0
    return sum(r["rating_score"] for r in rows) / len(rows)


async def get_host_tier(reputation_score: float, tier_defs: str) -> dict | None:
    # Maps a float rating value (e.g. 4.25) to a cosmetic string literal ("Gold Tier").
    try:
        tiers = json.loads(tier_defs)
    except Exception:
        return None
        
    matched = None
    for t in sorted(tiers, key=lambda x: x["min_avg"]):
        if reputation_score >= t["min_avg"]:
            matched = t
            
    return matched


async def award_host_points(pool, event_id: str):
    # Condenses all user ratings and converts them into hard currency logic for the Host.
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT guild_id, host_id FROM events WHERE event_id=$1 AND is_valid=1", event_id)
        if not row: 
            return
            
        guild_id, host_id = row["guild_id"], row["host_id"]
        votes = await conn.fetch("SELECT vote_value FROM votes WHERE event_id=$1", event_id)
        
    if not votes: 
        return
        
    config = await get_all_config(guild_id)
    mult = float(config.get("host.income_multiplier", 2.0))
    
    # Calculate payout pool mathematically using the multiplier
    total = sum(v["vote_value"] for v in votes) * mult
    await award(guild_id, host_id, total)


async def update_elite_board(bot, pool, guild_id: str):
    # Placeholder block reserved for rendering global server-side message leaderboards
    pass
