# =====
# MODULE: db/pool.py
# =====
# Architecture Overview:
# Manages the global singleton instance of the PostgreSQL connection pool
# using the 'asyncpg' library. This pool guarantees safe concurrency
# for thousands of asynchronous database requests.
#
# Note: SSL is explicitly set to "require" for secure connections to Railway.
# =====

import asyncio
import asyncpg
import os

_pool: asyncpg.Pool | None = None
# BUG FIX (Race Condition): Without this lock, two coroutines that both see
# _pool is None can concurrently enter create_pool(), resulting in two pools
# being created and one leaking permanently. The lock serializes initialization.
_pool_lock: asyncio.Lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    # -----
    # Retrieves or initializes the singleton database pool lazily.
    # Connections are capped between 2 and 10 to optimize memory.
    # BUG FIX: Double-checked locking pattern prevents race condition on startup
    # when multiple coroutines all call get_pool() before the pool exists.
    # -----
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:  # Re-check inside lock (double-checked locking)
            _pool = await asyncpg.create_pool(
                dsn=os.environ["DATABASE_URL"],
                min_size=2,
                max_size=10,
                command_timeout=30,
                ssl="require",
            )
    return _pool


async def close_pool():
    # -----
    # Safely dismantles the postgres pool during bot shutdown
    # to prevent orphaned connections.
    # -----
    global _pool
    if _pool:
        await _pool.close()
        _pool = None