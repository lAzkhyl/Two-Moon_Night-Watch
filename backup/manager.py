# =====
# MODULE: backup/manager.py
# =====
# Architecture Overview:
# Handles the generation, validation, and restoration of server backups.
# Backups are serialized into JSON dicts containing config keys, shop items,
# and (optionally) full economy participant records.
#
# Checksums are generated via SHA-256 to ensure data integrity.
# =====

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone

from db.pool import get_pool
from config.manager import invalidate_guild_cache

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

CROSS_GUILD_EXCLUDED_KEYS = {
    "system.admin_role_id",
    "system.mod_role_id",
    "channel.gamenight_id",
    "channel.activity_id",
    "channel.vc_category_id",
    "owner.backup_channel_id",
    "host.elite_channel_id",
}


def generate_backup_id() -> str:
    # -----
    # Generates a predictable yet unique identifier for the backup file.
    # Format: 2M-YYYYMMDD-XXXX
    # -----
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(2).upper()
    return f"2M-{date_str}-{suffix}"


def _compute_checksum(payload: dict) -> str:
    # -----
    # Cryptographically hashes the payload JSON string to prevent tampering.
    # -----
    raw = json.dumps(payload, default=str, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _verify_checksum(payload: dict, stored: str) -> bool:
    return _compute_checksum(payload) == stored


async def create_backup(
    guild_id: str,
    initiated_by: str,
    backup_type: str = "manual",
    include_user_data: bool = False,
) -> dict:
    # -----
    # Compiles server state into a structured payload dictionary.
    # Optionally includes user point tracking arrays if requested.
    # Returns the complete backup document ready for JSON serialization.
    # -----
    logger.info(
        f"[Backup] Creating {backup_type} backup for guild {guild_id}  |  "
        f"initiated_by={initiated_by}  |  full={include_user_data}"
    )
    pool = await get_pool()
    # CVE-2M-016: REPEATABLE READ ensures all SELECTs see a consistent snapshot
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read"):
            config_rows = await conn.fetch(
                "SELECT config_key, config_value FROM bot_config WHERE guild_id=$1",
                guild_id,
            )
            shop_rows = await conn.fetch(
                "SELECT * FROM shop_items WHERE guild_id=$1 AND is_active=TRUE",
                guild_id,
            )
            audit_rows = await conn.fetch(
                """
                SELECT guild_id, changed_by, config_key, old_value, new_value,
                       changed_at::text
                FROM config_audit_log
                WHERE guild_id=$1
                ORDER BY changed_at DESC
                LIMIT 100
                """,
                guild_id,
            )

            guild_name_row = await conn.fetchval(
                "SELECT config_value FROM bot_config WHERE guild_id=$1 AND config_key='system.guild_name'",
                guild_id,
            )

            payload: dict = {
                "bot_config": [dict(r) for r in config_rows],
                "shop_items": [dict(r) for r in shop_rows],
                "config_audit_log_snapshot": [dict(r) for r in audit_rows],
            }

            if include_user_data:
                user_rows = await conn.fetch(
                    "SELECT discord_id, raw_points, last_active_at::text FROM users WHERE guild_id=$1",
                    guild_id,
                )
                inventory_rows = await conn.fetch(
                    """
                    SELECT guild_id, user_id, item_id, acquired_at::text, expires_at::text
                    FROM user_inventory WHERE guild_id=$1
                    """,
                    guild_id,
                )
                payload["users"] = [dict(r) for r in user_rows]
                payload["user_inventory"] = [dict(r) for r in inventory_rows]

    checksum = _compute_checksum(payload)
    backup_id = generate_backup_id()
    config_key_count = len(config_rows)

    backup_doc = {
        "backup_id":      backup_id,
        "guild_id":       guild_id,
        "guild_name":     guild_name_row or "",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "backup_type":    backup_type,
        "initiated_by":   initiated_by,
        "schema_version": SCHEMA_VERSION,
        "payload":        payload,
        "checksum":       checksum,
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO backup_log
                (backup_id, guild_id, backup_type, initiated_by, config_keys,
                 checksum, is_full_backup)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            backup_id,
            guild_id,
            backup_type,
            initiated_by,
            config_key_count,
            checksum,
            include_user_data,
        )

    logger.info(
        f"[Backup] ✅ Created backup {backup_id}  |  keys={config_key_count}  |  "
        f"shop_items={len(payload['shop_items'])}  |  full={include_user_data}"
    )
    return backup_doc


async def set_backup_message_id(backup_id: str, message_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE backup_log SET channel_msg_id=$1 WHERE backup_id=$2",
            message_id,
            backup_id,
        )


async def get_backup_log(guild_id: str, limit: int = 5) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT backup_id, backup_type, initiated_by, config_keys,
                   is_full_backup, created_at
            FROM backup_log
            WHERE guild_id=$1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guild_id,
            limit,
        )
    return [dict(r) for r in rows]


async def get_backup_by_id(guild_id: str, backup_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM backup_log WHERE guild_id=$1 AND backup_id=$2",
            guild_id,
            backup_id,
        )
    return dict(row) if row else None


def validate_backup_file(
    data: dict, current_guild_id: str
) -> tuple[bool, list[str]]:
    # -----
    # Deep inspects uploaded backup data to prevent corrupted schema injections.
    # Triggers warnings if a cross-server migration backup is detected.
    # -----
    warnings: list[str] = []

    required_fields = {"backup_id", "guild_id", "schema_version", "payload", "checksum"}
    if not required_fields.issubset(data.keys()):
        missing = required_fields - data.keys()
        logger.warning(f"[Backup] Validation failed: missing fields {missing}  |  backup_id={data.get('backup_id', '?')}")
        return False, ["Missing required fields in backup file."]

    if data.get("schema_version") != SCHEMA_VERSION:
        logger.warning(
            f"[Backup] Schema version mismatch: file={data.get('schema_version')} expected={SCHEMA_VERSION}  |  "
            f"backup_id={data.get('backup_id', '?')}"
        )
        return False, [
            f"Schema version mismatch: file={data.get('schema_version')}, expected={SCHEMA_VERSION}."
        ]

    payload = data.get("payload", {})
    if not _verify_checksum(payload, data["checksum"]):
        logger.warning(f"[Backup] Checksum mismatch for backup_id={data.get('backup_id', '?')} — file may be tampered.")
        return False, ["Checksum validation failed. File may be corrupted or modified."]

    if data["guild_id"] != current_guild_id:
        warnings.append(
            f"This backup is from a different server ({data.get('guild_name', data['guild_id'])})."
        )
        logger.info(
            f"[Backup] Cross-guild restore detected: backup_guild={data['guild_id']} "
            f"current_guild={current_guild_id}  |  backup_id={data.get('backup_id')}"
        )
        config_entries = payload.get("bot_config", [])
        excluded = [
            e["config_key"]
            for e in config_entries
            if e["config_key"] in CROSS_GUILD_EXCLUDED_KEYS
        ]
        if excluded:
            warnings.append(
                f"The following keys will be skipped (guild-specific): {', '.join(excluded)}"
            )

    return True, warnings


async def restore_backup(
    guild_id: str,
    backup_doc: dict,
    initiated_by: str,
) -> tuple[str, dict]:
    # -----
    # Irreversibly overwrites current server tables with the payload state.
    # Auto-creates a 'pre_sync' backup before executing to prevent data loss.
    # Excludes environment-specific hardware IDs during cross-server restores.
    # -----
    bid = backup_doc["backup_id"]
    logger.warning(
        f"[Backup] ⚠️  RESTORE initiated  |  target_guild={guild_id}  |  "
        f"source_backup={bid}  |  initiated_by={initiated_by}"
    )

    pool = await get_pool()
    pre_sync_doc = await create_backup(
        guild_id,
        initiated_by="system",
        backup_type="pre_sync",
        include_user_data=False,
    )
    pre_sync_id = pre_sync_doc["backup_id"]
    logger.info(f"[Backup] Pre-sync safety backup created: {pre_sync_id}")

    payload = backup_doc["payload"]
    is_cross_guild = backup_doc["guild_id"] != guild_id

    config_entries: list[dict] = payload.get("bot_config", [])
    shop_entries: list[dict] = payload.get("shop_items", [])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM bot_config WHERE guild_id=$1", guild_id
            )
            keys_written = 0
            keys_skipped = 0
            for entry in config_entries:
                key = entry["config_key"]
                if is_cross_guild and key in CROSS_GUILD_EXCLUDED_KEYS:
                    keys_skipped += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    """,
                    guild_id,
                    key,
                    entry["config_value"],
                )
                keys_written += 1

            for item in shop_entries:
                await conn.execute(
                    """
                    INSERT INTO shop_items
                        (item_id, guild_id, label, description, cost, item_type,
                         duration_days, role_id, is_blackmarket, stock, is_active)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (item_id, guild_id)
                    DO UPDATE SET
                        label=EXCLUDED.label, description=EXCLUDED.description,
                        cost=EXCLUDED.cost, item_type=EXCLUDED.item_type,
                        duration_days=EXCLUDED.duration_days, role_id=EXCLUDED.role_id,
                        is_blackmarket=EXCLUDED.is_blackmarket, stock=EXCLUDED.stock,
                        is_active=EXCLUDED.is_active
                    """,
                    item["item_id"],
                    guild_id,
                    item["label"],
                    item.get("description"),
                    item["cost"],
                    item["item_type"],
                    item.get("duration_days"),
                    item.get("role_id"),
                    item.get("is_blackmarket", False),
                    item.get("stock"),
                    item.get("is_active", True),
                )

            await conn.execute(
                """
                INSERT INTO config_audit_log (guild_id, changed_by, config_key, old_value, new_value)
                VALUES ($1, $2, 'system.restore', $3, $4)
                """,
                guild_id,
                initiated_by,
                pre_sync_id,
                bid,
            )

    logger.info(
        f"[Backup] ✅ Restore complete  |  guild={guild_id}  |  source={bid}  |  "
        f"keys_written={keys_written}  |  keys_skipped={keys_skipped}  |  "
        f"shop_items={len(shop_entries)}  |  cross_guild={is_cross_guild}"
    )

    invalidate_guild_cache(guild_id)

    if is_cross_guild:
        restore_state = "UNCONFIGURED"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_config (guild_id, config_key, config_value, updated_at)
                VALUES ($1, 'system.state', $2, NOW())
                ON CONFLICT (guild_id, config_key)
                DO UPDATE SET config_value=EXCLUDED.config_value, updated_at=NOW()
                """,
                guild_id,
                restore_state,
            )
        invalidate_guild_cache(guild_id)
        logger.info(f"[Backup] Cross-guild restore: system.state set to UNCONFIGURED for guild {guild_id}.")

    return pre_sync_id, pre_sync_doc