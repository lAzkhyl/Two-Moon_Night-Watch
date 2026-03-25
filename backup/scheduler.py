# =====
# MODULE: backup/scheduler.py
# =====
# Architecture Overview:
# A background daemon loop exclusively managing automated scheduled backups.
# Binds gracefully to Discord's Event Loop natively via asyncio Tasks.
#
# CVE-2M-018: Rewritten to use last-backup-based scheduling instead of
# fixed sleep intervals. Now properly handles config changes mid-sleep
# and bot restart gap scenarios.
# =====

import asyncio
import io
import json
import logging
import discord

from backup.manager import create_backup, set_backup_message_id
from config.manager import get_config_or_none
from db.pool import get_pool
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def start_auto_backup(bot: discord.Client):
    # -----
    # Bootstraps the backup task loop safely, ensuring no duplication occurs.
    # -----
    global _task
    if _task and not _task.done():
        logger.debug("[AutoBackup] Cancelling existing backup task before restart.")
        _task.cancel()
    _task = asyncio.create_task(_auto_backup_loop(bot))
    logger.info("[AutoBackup] Auto-backup scheduler started.")


async def _auto_backup_loop(bot: discord.Client):
    # -----
    # CVE-2M-018: Last-backup-based scheduling.
    # Instead of sleeping a full interval on boot, we check when the last
    # backup was created and only sleep the remaining gap. This prevents
    # double-interval gaps after bot restarts.
    # Config changes take effect at the next check cycle (max 5 min delay).
    # -----
    await bot.wait_until_ready()
    guild_id = str(bot.guild_id)
    logger.info(f"[AutoBackup] Loop active for guild {guild_id}.")

    while not bot.is_closed():
        try:
            interval_hours = await get_config_or_none(guild_id, "owner.backup_interval_hours", int) or 24
            enabled = await get_config_or_none(guild_id, "owner.backup_auto_enabled", bool)

            if enabled is False:
                # Re-check every 5 minutes if auto-backup has been re-enabled
                await asyncio.sleep(300)
                continue

            channel_id = await get_config_or_none(guild_id, "owner.backup_channel_id")
            if not channel_id:
                logger.debug("[AutoBackup] No backup channel configured. Waiting 5 min.")
                await asyncio.sleep(300)
                continue

            channel = bot.get_channel(int(channel_id))
            if not channel:
                logger.warning(
                    f"[AutoBackup] Configured backup channel {channel_id} not found in cache. "
                    f"Check 'owner.backup_channel_id' config. Waiting 5 min."
                )
                await asyncio.sleep(300)
                continue

            # Check when last auto backup was created
            pool = await get_pool()
            async with pool.acquire() as conn:
                last_backup_at = await conn.fetchval(
                    "SELECT created_at FROM backup_log WHERE guild_id=$1 "
                    "AND backup_type='auto' ORDER BY created_at DESC LIMIT 1",
                    guild_id,
                )

            now = datetime.now(timezone.utc)
            if last_backup_at:
                if last_backup_at.tzinfo is None:
                    last_backup_at = last_backup_at.replace(tzinfo=timezone.utc)
                next_backup = last_backup_at + timedelta(hours=interval_hours)
                wait_s = max(0, (next_backup - now).total_seconds())
            else:
                # No previous backup — run immediately
                wait_s = 0

            if wait_s > 0:
                logger.debug(f"[AutoBackup] Next backup in {wait_s:.0f}s ({wait_s/3600:.2f}h).")
                # Sleep in small chunks so we can respond to cancellation
                await asyncio.sleep(min(wait_s, 300))
                if wait_s > 300:
                    continue  # Re-check interval/enabled after partial sleep

            # Perform the backup
            include_full = await get_config_or_none(guild_id, "owner.backup_full", bool) or False
            logger.info(f"[AutoBackup] Creating {'full' if include_full else 'config-only'} auto-backup for guild {guild_id}.")

            backup_doc = await create_backup(
                guild_id,
                initiated_by="system",
                backup_type="auto",
                include_user_data=include_full,
            )

            msg = await _send_backup_to_channel(channel, backup_doc)
            if msg:
                await set_backup_message_id(backup_doc["backup_id"], str(msg.id))
                logger.info(f"[AutoBackup] ✅ Backup {backup_doc['backup_id']} posted to channel {channel_id}.")
            else:
                logger.warning(f"[AutoBackup] Backup {backup_doc['backup_id']} created but failed to post to channel.")

        except asyncio.CancelledError:
            logger.info("[AutoBackup] Backup task cancelled. Exiting loop.")
            break
        except Exception:
            # BUG FIX: Previously `except Exception: pass` — errors were completely invisible.
            # Now logged with full traceback so operators know why backups are failing.
            logger.exception(
                "[AutoBackup] ❌ Unhandled error in auto-backup loop. "
                "Retrying in 5 minutes. Check DB connectivity and channel permissions."
            )
            await asyncio.sleep(300)

    logger.info("[AutoBackup] Loop exited.")


async def _send_backup_to_channel(
    channel: discord.TextChannel,
    backup_doc: dict,
) -> discord.Message | None:
    bid        = backup_doc["backup_id"]
    guild_name = backup_doc.get("guild_name", "")
    guild_id   = backup_doc["guild_id"]
    btype      = backup_doc["backup_type"].upper()
    created_at = backup_doc["created_at"]
    n_keys     = len(backup_doc["payload"].get("bot_config", []))
    n_shop     = len(backup_doc["payload"].get("shop_items", []))
    n_audit    = len(backup_doc["payload"].get("config_audit_log_snapshot", []))

    content = (
        f"📦 {btype}  │  ID: `{bid}`\n"
        f"Server  : {guild_name} (`{guild_id}`)\n"
        f"Time    : {created_at}\n"
        f"Config keys: {n_keys}  │  Shop items: {n_shop}  │  Audit entries: {n_audit}"
    )

    file_bytes = json.dumps(backup_doc, indent=2, default=str).encode()
    file_obj   = discord.File(io.BytesIO(file_bytes), filename=f"{bid}.json")

    try:
        return await channel.send(content=content, file=file_obj)
    except discord.HTTPException:
        logger.exception(f"[AutoBackup] Discord HTTP error when posting backup {bid} to channel {channel.id}.")
        return None


async def send_backup_to_channel(
    channel: discord.TextChannel,
    backup_doc: dict,
) -> discord.Message | None:
    return await _send_backup_to_channel(channel, backup_doc)