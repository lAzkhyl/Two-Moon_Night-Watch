# =====
# MODULE: main.py
# =====
# Architecture Overview:
# This is the application entry point. It handles the Discord connection
# lifecycle, PostgreSQL connection pooling, database migrations, and dynamic
# module (Cog) loading. It also manages the cluster leader election process
# to support zero-downtime deployment migrations.
#
# Developer Note:
# Functions in this file shouldn't contain business logic. Keep this restricted
# to infrastructure, boot sequences, and shutdown cleanup.
# =====
import asyncio
import os
import platform
import secrets

from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands                     # BUG FIX: was missing — caused NameError in error handler
from discord.ext import commands, tasks

from db.pool import get_pool, close_pool
from db.migrate import run_migrations
from backup.scheduler import start_auto_backup
from utils.time import set_start_time
from utils.logger import setup_enterprise_logging
import logging

setup_enterprise_logging()
logger = logging.getLogger(__name__)

# -----
# The list of extension modules (Cogs) loaded on startup.
# Adding a new feature folder requires registering the entry path here.
# -----
COGS = [
    "cogs.owner",
    "cogs.admin",
    "cogs.gamenight",
    "cogs.public",
    "cogs.economy",
]

# -----
# Discord API Intents
# These act as permission flags for receiving socket events from Discord.
# We require member tracking, voice state updates (for AFK tracking), and
# message content (for legacy logic or specific thread reading).
# -----
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="\x00", intents=intents)

# -----
# High-Availability / Migration Globals
# We generate a unique INSTANCE_ID per application boot. If multiple bot 
# instances run against the same database, they use this ID to negotiate 
# who processes commands (LEADER) and who idles (STANDBY).
# -----
INSTANCE_ID = f"{platform.node()}-{secrets.token_hex(4)}"
HEARTBEAT_INTERVAL = 15
LEADER_TIMEOUT = 60


async def _register_instance(pool, guild_id: str):
    # -----
    # CVE-2M-011: Atomic leader election via conditional UPDATE RETURNING.
    # Registers the current process to the PostgreSQL 'bot_instances' table.
    # Instead of two separate queries (check + update), we use a single atomic
    # statement to prevent split-brain scenarios.
    # -----
    async with pool.acquire() as conn:
        # First register as STANDBY
        await conn.execute(
            """
            INSERT INTO bot_instances (instance_id, guild_id, role, hostname, started_at, last_heartbeat)
            VALUES ($1, $2, 'STANDBY', $3, NOW(), NOW())
            ON CONFLICT (instance_id)
            DO UPDATE SET role='STANDBY', hostname=$3, started_at=NOW(), last_heartbeat=NOW(), force_shutdown=FALSE
            """,
            INSTANCE_ID, guild_id, platform.node(),
        )
        # Atomically promote to LEADER only if no healthy leader exists
        promoted = await conn.fetchval(
            """
            UPDATE bot_instances SET role='LEADER'
            WHERE instance_id=$1
              AND NOT EXISTS (
                SELECT 1 FROM bot_instances
                WHERE role='LEADER'
                  AND last_heartbeat > NOW() - INTERVAL '60 seconds'
                  AND instance_id != $1
              )
            RETURNING instance_id
            """,
            INSTANCE_ID,
        )
        role = "LEADER" if promoted else "STANDBY"
    logger.info(f"[Migration] Registered as {role} (id={INSTANCE_ID})")
    return role


async def resolve_guild_id(pool) -> int:
    # -----
    # Retrieves the target Discord Server ID (Guild ID) for the bot.
    #
    # We prioritize the database configuration. If the database is completely 
    # empty (e.g. first deploy), we fall back to the GUILD_ID environment 
    # variable.
    # -----
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT config_value FROM bot_config WHERE config_key='system.guild_id' LIMIT 1"
        )
    if val:
        return int(val)
    env_val = os.environ.get("GUILD_ID")
    if env_val:
        return int(env_val)
    raise RuntimeError("Guild ID not found. Set GUILD_ID env var on first deploy.")


@tasks.loop(seconds=HEARTBEAT_INTERVAL)
async def leader_watchdog():
    # -----
    # A persistent asynchronous loop managing instance health and failovers.
    #
    # Responsibilities:
    # 1. Heartbeat: Pings the database to prove this instance is alive.
    # 2. Shutdown Signal: Reads 'force_shutdown' flags manipulated by the 
    #    Owner panel to gracefully exit.
    # 3. Failover Promotion: If this instance is STANDBY and the LEADER times 
    #    out, this instance promotes itself to ensure service continuity.
    # 4. Demotion: If an admin transfers leadership away, this instance 
    #    detects the change, steps down, and initiates suicide (shutdown).
    # -----
    try:
        pool = await get_pool()
        gid = str(bot.guild_id)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE bot_instances SET last_heartbeat=NOW() WHERE instance_id=$1",
                INSTANCE_ID,
            )

            row = await conn.fetchrow(
                "SELECT role, force_shutdown FROM bot_instances WHERE instance_id=$1",
                INSTANCE_ID,
            )

        if not row:
            logger.warning(f"[Watchdog] Instance {INSTANCE_ID} not found in registry. Re-registering.")
            return

        # Check for manual termination signal
        if row["force_shutdown"]:
            logger.warning(f"[Watchdog] Force shutdown received. Shutting down instance {INSTANCE_ID}...")
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM bot_instances WHERE instance_id=$1", INSTANCE_ID)
            await bot.close()
            return

        # Standby or demoted leader logic
        if row["role"] != "LEADER":
            async with pool.acquire() as conn:
                # CVE-2M-011: Atomic promotion via conditional UPDATE RETURNING
                promoted = await conn.fetchval(
                    """
                    UPDATE bot_instances SET role='LEADER'
                    WHERE instance_id=$1
                      AND NOT EXISTS (
                        SELECT 1 FROM bot_instances
                        WHERE guild_id=$2 AND role='LEADER'
                          AND last_heartbeat > NOW() - INTERVAL '60 seconds'
                      )
                    RETURNING instance_id
                    """,
                    INSTANCE_ID, gid,
                )
                
            if promoted:
                logger.info(f"[Watchdog] Leader timed out. Promoting self ({INSTANCE_ID}) to LEADER.")
                bot._was_leader = True
            else:
                # If we were previously LEADER but the database says we are STANDBY,
                # it means the admin manually assigned leadership to another instance.
                # CVE-2M-012: Default to False to prevent accidental self-destruct
                was_leader = getattr(bot, '_was_leader', False)
                if was_leader:
                    logger.warning(f"[Watchdog] Leadership transferred away from {INSTANCE_ID}. Graceful shutdown in 5s...")
                    bot._was_leader = False
                    await asyncio.sleep(5)
                    async with pool.acquire() as conn:
                        await conn.execute("DELETE FROM bot_instances WHERE instance_id=$1", INSTANCE_ID)
                    await bot.close()
                    return

    except Exception:
        logger.exception("[Watchdog] Unhandled error in leader_watchdog tick.")


@leader_watchdog.before_loop
async def before_watchdog():
    # Defers the watchdog execution until the Discord websocket connection is stable.
    await bot.wait_until_ready()


# CVE-2M-015: Idempotency guard prevents re-initialization on Discord reconnect
_initialized = False


@bot.event
async def on_ready():
    # -----
    # Called sequentially once the Discord websocket is fully connected.
    # We use this as our initialization block.
    # CVE-2M-015: Only runs full init once; reconnects are silently skipped.
    # -----
    global _initialized

    if _initialized:
        logger.info(f"[Reconnect] Discord reconnected as {bot.user}. Skipping re-initialization.")
        return

    logger.info(f"[Boot] on_ready fired. Bot user: {bot.user}")
    set_start_time()
    pool = await get_pool()

    # BUG FIX: Wrap migrations in try/except so a single bad migration file
    # does not abort the entire startup sequence. The bot can start in a
    # degraded state and the operator is clearly alerted.
    try:
        await run_migrations(pool)
        logger.info("[Boot] Database migrations completed successfully.")
    except Exception:
        logger.exception(
            "[Boot] CRITICAL: Database migration failed. The schema may be incomplete. "
            "Inspect db/migrations/ for errors. Bot will continue in degraded mode."
        )

    try:
        guild_id = await resolve_guild_id(pool)
    except RuntimeError:
        logger.exception("[Boot] Cannot resolve Guild ID. Shutting down.")
        await bot.close()
        return

    bot.guild_id = guild_id
    bot.instance_id = INSTANCE_ID
    guild_obj = discord.Object(id=guild_id)

    # Boot registration for migration cluster
    try:
        role = await _register_instance(pool, str(guild_id))
        bot._was_leader = (role == "LEADER")
    except Exception:
        logger.exception("[Boot] Failed to register instance in bot_instances table. Defaulting to LEADER.")
        bot._was_leader = True
        role = "LEADER (fallback)"

    # Start automated background backups safely
    start_auto_backup(bot)

    # Load feature modules dynamically
    cog_failures = []
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            logger.debug(f"[Boot] Loaded cog: {cog}")
        except Exception:
            logger.exception(f"[Boot] Failed to load cog: {cog}")
            cog_failures.append(cog)

    if cog_failures:
        logger.error(f"[Boot] {len(cog_failures)} cog(s) failed to load: {cog_failures}")

    # Sync slash commands specific to the working guild for immediate propagation
    try:
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        logger.info(f"[Boot] Synced {len(synced)} slash command(s) to guild {guild_id}.")
    except Exception:
        logger.exception("[Boot] Failed to sync slash commands.")

    # Spin up the background health thread
    if not leader_watchdog.is_running():
        leader_watchdog.start()

    _initialized = True
    logger.info(
        f"[Boot] ✅ Ready  |  guild={guild_id}  |  instance={INSTANCE_ID}  |  role={role}  |  "
        f"user={bot.user}  |  cog_failures={len(cog_failures)}"
    )


@bot.event
async def on_close():
    # -----
    # Teardown logic. Un-registers the instance from the cluster to allow fast 
    # failovers if another STANDBY is waiting.
    # -----
    logger.info(f"[Shutdown] on_close fired for instance {INSTANCE_ID}.")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_instances WHERE instance_id=$1", INSTANCE_ID)
        logger.info(f"[Shutdown] Instance {INSTANCE_ID} de-registered from cluster.")
    except Exception:
        logger.exception("[Shutdown] Failed to de-register instance during shutdown.")
    finally:
        await close_pool()
        logger.info("[Shutdown] Connection pool closed.")


# -----
# GLOBAL SLASH COMMAND ERROR HANDLER
# Defined at module level (not inside main()) to avoid NameError on app_commands
# annotations caused by nested function scope + eager annotation evaluation.
# BUG FIX: was defined inside main() without importing app_commands, causing
#           the handler to crash before logging — which is why no logs appeared.
# -----
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Extract the original exception if it's wrapped by the command invoker
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error

    cmd_name = interaction.command.name if interaction.command else "unknown"
    guild_id = interaction.guild_id or "DM"
    user_id  = interaction.user.id

    logger.error(
        f"[SlashCmd] Exception in /{cmd_name}  |  guild={guild_id}  |  user={user_id}  |  "
        f"error_type={type(original).__name__}  |  {original}",
        exc_info=original,
    )

    try:
        msg = "⚠️ An internal error occurred while processing this command. The issue has been logged."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        logger.exception(f"[SlashCmd] Failed to send error response for /{cmd_name} to user {user_id}.")


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        logger.critical("[Boot] DISCORD_TOKEN environment variable is not set. Exiting.")
        return

    try:
        async with bot:
            await bot.start(token)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[Shutdown] KeyboardInterrupt received. Exiting cleanly.")