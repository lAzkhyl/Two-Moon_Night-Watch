# =====
# MODULE: config/manager.py
# =====
# Architecture Overview:
# Handles persistent bot configuration interacting with the PostgreSQL database.
# Incorporates an aggressive local memory cache (`_cache`) to prevent hammering
# the database for every single command request.
#
# When a configuration value is modified, it updates the DB, purges the local 
# cache segment for that guild, and writes the change to the `config_audit_log`.
# =====

import json
import time as _time
from db.pool import get_pool
from config.validators import CONFIG_VALIDATORS


class ConfigKeyNotFoundError(Exception):
    pass


class ConfigValidationError(Exception):
    pass


# CVE-2M-021: TTL-based cache for cross-instance consistency
_cache: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
CACHE_TTL = 60.0


def _cache_key(guild_id: str, key: str) -> str:
    return f"{guild_id}:{key}"


async def get_config(guild_id: str, key: str, cast=str):
    # -----
    # Primary getter function with TTL-based caching proxy mechanism.
    # Automatically cast returned values to matching python types.
    # CVE-2M-021: Cache entries expire after CACHE_TTL seconds.
    # -----
    ck = _cache_key(guild_id, key)
    now = _time.monotonic()

    if ck in _cache:
        val, expires_at = _cache[ck]
        if now < expires_at:
            raw = val
        else:
            del _cache[ck]
            raw = None
    else:
        raw = None

    if raw is None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT config_value FROM bot_config WHERE guild_id=$1 AND config_key=$2",
                guild_id, key,
            )
        if val is None:
            raise ConfigKeyNotFoundError(key)
        _cache[ck] = (val, now + CACHE_TTL)
        raw = val
    
    if cast in (list, dict):
        return json.loads(raw)
    if cast is bool:
        return raw.lower() in ("true", "1", "yes")
        
    return cast(raw)


async def get_config_or_none(guild_id: str, key: str, cast=str):
    try:
        return await get_config(guild_id, key, cast)
    except ConfigKeyNotFoundError:
        return None


async def set_config(guild_id: str, key: str, value, changed_by: str):
    # -----
    # CVE-2M-019: All three operations (read old, upsert, audit log) are now
    # wrapped in a single atomic transaction to prevent partial commits.
    # -----
    new_val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
    validator = CONFIG_VALIDATORS.get(key)
    
    if validator:
        try:
            if not validator(new_val):
                raise ConfigValidationError(f"Invalid value for {key}: {new_val}")
        except (ValueError, TypeError) as exc:
            raise ConfigValidationError(f"Invalid value for {key}: {new_val}") from exc
            
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_val = await conn.fetchval(
                "SELECT config_value FROM bot_config WHERE guild_id=$1 AND config_key=$2",
                guild_id, key,
            )
            await conn.execute(
                """
                INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (guild_id, config_key)
                DO UPDATE SET config_value = EXCLUDED.config_value, updated_at = NOW()
                """,
                guild_id, key, new_val,
            )
            await conn.execute(
                """
                INSERT INTO config_audit_log (guild_id, changed_by, config_key, old_value, new_value)
                VALUES ($1, $2, $3, $4, $5)
                """,
                guild_id, changed_by, key, old_val, new_val,
            )
        
    _cache.pop(_cache_key(guild_id, key), None)


async def bulk_set_config(guild_id: str, data: dict[str, str], changed_by: str):
    # CVE-2M-025: Batch all config writes in a single transaction
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in data.items():
                new_val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
                validator = CONFIG_VALIDATORS.get(key)
                if validator:
                    try:
                        if not validator(new_val):
                            raise ConfigValidationError(f"Invalid value for {key}: {new_val}")
                    except (ValueError, TypeError) as exc:
                        raise ConfigValidationError(f"Invalid value for {key}: {new_val}") from exc

                old_val = await conn.fetchval(
                    "SELECT config_value FROM bot_config WHERE guild_id=$1 AND config_key=$2",
                    guild_id, key,
                )
                await conn.execute(
                    """
                    INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (guild_id, config_key)
                    DO UPDATE SET config_value = EXCLUDED.config_value, updated_at = NOW()
                    """,
                    guild_id, key, new_val,
                )
                await conn.execute(
                    """
                    INSERT INTO config_audit_log (guild_id, changed_by, config_key, old_value, new_value)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    guild_id, changed_by, key, old_val, new_val,
                )
    invalidate_guild_cache(guild_id)


async def get_all_config(guild_id: str) -> dict[str, str]:
    # -----
    # Pre-warms the cache by fetching all key-values for a specific server.
    # Essential for intensive leaderboard processing routines.
    # -----
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT config_key, config_value FROM bot_config WHERE guild_id=$1 ORDER BY config_key",
            guild_id,
        )
        
    result = {r["config_key"]: r["config_value"] for r in rows}
    now = _time.monotonic()
    
    for row in rows:
        _cache[_cache_key(guild_id, row["config_key"])] = (row["config_value"], now + CACHE_TTL)
        
    return result


def invalidate_guild_cache(guild_id: str):
    prefix = f"{guild_id}:"
    for key in list(_cache.keys()):
        if key.startswith(prefix):
            del _cache[key]