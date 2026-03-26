--- cogs/owner.py (原始)
# =====
# MODULE: cogs/owner.py
# =====
# Architecture Overview:
# Provides the Server Owner Control Panel, accessible only to the guild owner.
# Handles backups, statistics, deployment migration, and access control setup.
#
# BUG FIX (CVE-2M-020):
# /owner and all DB-heavy panel builders now call i.response.defer() before
# making async DB queries. Discord's 3-second response window is easily exceeded
# by cold DB pool connections; deferring extends the window to 15 minutes.
# =====
import asyncio
import io
import json
import logging
import time
# BUG FIX: 'datetime' and 'timezone' were missing — caused NameError crash
# every time _build_migration_panel() was called to render the panel.
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from backup.manager import (
    create_backup,
    get_backup_by_id,
    get_backup_log,
    restore_backup,
    set_backup_message_id,
    validate_backup_file,
)
from backup.scheduler import send_backup_to_channel
from config.manager import get_config_or_none, set_config
from db.pool import get_pool
from guards.checks import is_server_owner
from utils.embeds import PRIMARY, confirm_embed, error_embed, success_embed
from utils.time import format_relative, format_uptime

logger = logging.getLogger(__name__)

# Active threshold: heartbeat fires every 15s, so 90s = 6 missed beats before
# we consider an instance offline. More forgiving than raw LEADER_TIMEOUT (60s).
_INSTANCE_ACTIVE_THRESHOLD_S = 90


# -----
# HELPER FUNCTIONS
# -----
async def _db_latency_ms() -> int:
    pool = await get_pool()
    t0 = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return int((time.perf_counter() - t0) * 1000)


def _format_role_list(guild: discord.Guild, raw_val: str | None) -> str:
    # -----
    # BUG FIX: Replaces the old _role_display() which only handled a single
    # role ID string. Now correctly parses JSON arrays stored by the multi-role
    # selects, and gracefully falls back for legacy single-ID strings.
    # -----
    if not raw_val or raw_val in ("null", "[]"):
        return "*(not set)*"
    try:
        ids = json.loads(raw_val)
        if not isinstance(ids, list):
            ids = [ids]
    except (json.JSONDecodeError, TypeError):
        ids = [raw_val]

    mentions = []
    for rid in ids:
        role = guild.get_role(int(rid)) if str(rid).isdigit() else None
        mentions.append(role.mention if role else f"*(unknown: {rid})*")
    return ", ".join(mentions) if mentions else "*(not set)*"


def _sync_confirm_embed(backup_doc: dict, warnings: list[str]) -> discord.Embed:
    e = discord.Embed(title="🔄  CONFIRM SYNC RESTORE", color=0xFEE75C)
    e.add_field(name="Backup ID",   value=f"`{backup_doc['backup_id']}`",                         inline=True)
    e.add_field(name="Created",     value=str(backup_doc.get("created_at", "?"))[:19],             inline=True)
    e.add_field(name="Type",        value=backup_doc.get("backup_type", "?"),                      inline=True)
    e.add_field(name="Config Keys", value=str(len(backup_doc["payload"].get("bot_config", []))),   inline=True)
    e.add_field(name="Checksum",    value="✅ Valid",                                              inline=True)
    e.add_field(name="\u200b",      value="\u200b",                                                inline=True)
    e.add_field(
        name="⚠️  This will overwrite the active configuration.",
        value="User data (points, inventory) is **not** affected unless the backup includes it.",
        inline=False,
    )
    if warnings:
        e.add_field(name="Warnings", value="\n".join(f"• {w}" for w in warnings), inline=False)
    return e


# -----
# PANEL BUILDERS
# -----
async def _build_main_panel(i: discord.Interaction):
    g     = i.guild
    gid   = str(g.id)
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")
    icon  = {"ACTIVE": "🟢", "PAUSED": "🟡", "UNCONFIGURED": "🔴"}.get(state, "⚪")

    # BUG FIX: Use _format_role_list() instead of old _role_display() which
    # only accepted a single ID and broke silently with JSON array values.
    e = discord.Embed(title="👑  TWO MOON — OWNER PANEL", color=PRIMARY)
    e.add_field(name="Server",      value=g.name,                            inline=True)
    e.add_field(name="Uptime",      value=format_uptime(),                   inline=True)
    e.add_field(name="State",       value=f"{icon} {state}",                 inline=True)
    e.add_field(name="Admin Roles", value=_format_role_list(g, ar_raw),      inline=True)
    e.add_field(name="Mod Roles",   value=_format_role_list(g, mr_raw),      inline=True)
    e.add_field(name="\u200b",      value="\u200b",                          inline=True)
    return e, OwnerMainView(gid)


async def _build_stats_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    pool = await get_pool()
    t0   = time.perf_counter()
    async with pool.acquire() as conn:
        s = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*)                       FROM users          WHERE guild_id=$1)::int AS total_users,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1)::int AS total_events,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1 AND is_valid=1)::int AS valid_events,
                (SELECT COUNT(*)                       FROM user_inventory WHERE guild_id=$1
                    AND (expires_at IS NULL OR expires_at > NOW()))::int                          AS active_rentals,
                (SELECT COUNT(*)                       FROM bet_pools      WHERE guild_id=$1 AND status='open')::int AS open_bets,
                (SELECT COALESCE(SUM(raw_points),  0)  FROM users          WHERE guild_id=$1)     AS total_points,
                (SELECT COALESCE(SUM(tax_amount),  0)  FROM bet_pools      WHERE guild_id=$1)     AS burned_points,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1
                    AND started_at > NOW() - INTERVAL '7 days')::int                              AS events_7d
            """,
            gid,
        )
    db_ms = int((time.perf_counter() - t0) * 1000)

    try:
        pool_cur, pool_max = pool.get_size(), pool.get_max_size()
    except AttributeError:
        pool_cur, pool_max = "?", 10

    last_bk = (await get_backup_log(gid, limit=1) or [None])[0]

    e = discord.Embed(title="📊  BOT STATISTICS", color=PRIMARY)
    e.add_field(name="🖥️ System", value=(
        f"**Uptime:** {format_uptime()}\n"
        f"**Discord Latency:** {round(i.client.latency * 1000)}ms\n"
        f"**DB Latency:** {db_ms}ms\n"
        f"**DB Pool:** {pool_cur} / {pool_max} connections"
    ), inline=False)
    e.add_field(name="📦 Data", value=(
        f"**Users:** {s['total_users']:,}\n"
        f"**Events:** {s['total_events']:,}  │  Valid: {s['valid_events']:,}\n"
        f"**Active Rentals:** {s['active_rentals']}\n"
        f"**Open Bet Pools:** {s['open_bets']}"
    ), inline=False)
    e.add_field(name="💰 Economy", value=(
        f"**Points in Circulation:** {float(s['total_points']):,.0f} pts\n"
        f"**Points Burned (Bet Tax):** {float(s['burned_points']):,.0f} pts\n"
        f"**Events (last 7 days):** {s['events_7d']}"
    ), inline=False)

    if last_bk:
        e.add_field(name="💾 Last Backup", value=(
            f"**ID:** `{last_bk['backup_id']}`\n"
            f"**When:** {format_relative(last_bk['created_at'])}\n"
            f"**Type:** {last_bk['backup_type']}"
        ), inline=False)
    else:
        e.add_field(name="💾 Backup", value="No backups recorded yet.", inline=False)
    return e, OwnerStatsView(gid)


async def _build_backup_panel(i: discord.Interaction):
    gid    = str(i.guild_id)
    ch_id  = await get_config_or_none(gid, "owner.backup_channel_id")
    intv   = await get_config_or_none(gid, "owner.backup_interval_hours", int) or 24
    en_raw = await get_config_or_none(gid, "owner.backup_auto_enabled")
    en     = en_raw != "false" if en_raw is not None else True
    logs   = await get_backup_log(gid, limit=5)
    ch     = i.guild.get_channel(int(ch_id)) if ch_id else None

    e = discord.Embed(title="💾  BACKUP CONFIGURATION", color=PRIMARY)
    e.add_field(name="Auto-Backup", value="✅ Enabled"  if en  else "⏸️ Disabled",     inline=True)
    e.add_field(name="Frequency",   value=f"Every {intv}h",                             inline=True)
    e.add_field(name="Channel",     value=ch.mention if ch else "*(not set)*",          inline=True)
    if logs:
        lines = [
            f"`{r['backup_id']}`  │  {str(r['created_at'])[:16].replace('T',' ')}  │  {r['config_keys']} keys  │  {r['backup_type']}"
            for r in logs
        ]
        e.add_field(name="Recent Backups", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="Recent Backups", value="No backups yet.", inline=False)
    return e, OwnerBackupView(gid)


async def _build_backup_config_panel(i: discord.Interaction):
    gid    = str(i.guild_id)
    ch_id  = await get_config_or_none(gid, "owner.backup_channel_id")
    intv   = await get_config_or_none(gid, "owner.backup_interval_hours", int) or 24
    en_raw = await get_config_or_none(gid, "owner.backup_auto_enabled")
    en     = en_raw != "false" if en_raw is not None else True
    fl_raw = await get_config_or_none(gid, "owner.backup_full")
    full   = fl_raw == "true" if fl_raw else False
    ch     = i.guild.get_channel(int(ch_id)) if ch_id else None

    e = discord.Embed(title="⚙️  BACKUP SETTINGS", color=PRIMARY)
    e.add_field(name="Auto-Backup", value="✅ Enabled"  if en   else "⏸️ Disabled",            inline=True)
    e.add_field(name="Frequency",   value=f"Every {intv}h",                                     inline=True)
    e.add_field(name="Channel",     value=ch.mention if ch else "*(not set)*",                  inline=True)
    e.add_field(name="Coverage",    value="Full (config + user data)" if full else "Config only", inline=True)
    return e, OwnerBackupConfigView(gid, intv, full, en)


async def _build_sync_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        n_keys   = await conn.fetchval("SELECT COUNT(*) FROM bot_config WHERE guild_id=$1", gid)
        last_mod = await conn.fetchval(
            "SELECT changed_at FROM config_audit_log WHERE guild_id=$1 ORDER BY changed_at DESC LIMIT 1", gid
        )
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"

    e = discord.Embed(title="🔄  SYNC BACKUP — Restore Configuration", color=PRIMARY)
    e.add_field(name="Active Config", value=(
        f"**State:** {state}  │  **Keys:** {n_keys}\n"
        f"**Last Modified:** {format_relative(last_mod) if last_mod else 'never'}"
    ), inline=False)
    e.add_field(name="Use this panel to:", value=(
        "• Restore config from a previous backup\n"
        "• Migrate from another Railway container\n"
        "• Roll back after a bad config change"
    ), inline=False)
    return e, OwnerSyncView(gid)


async def _build_admin_set_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    g    = i.guild
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")
    au_raw = await get_config_or_none(gid, "system.admin_user_id")
    mu_raw = await get_config_or_none(gid, "system.mod_user_id")

    def _fmt_users(raw_val):
        if not raw_val or raw_val in ("null", "[]"):
            return "*(not set)*"
        try:
            ids = json.loads(raw_val)
            if not isinstance(ids, list):
                ids = [ids]
        except (json.JSONDecodeError, TypeError):
            ids = [raw_val]
        mentions = []
        for uid in ids:
            m = g.get_member(int(uid)) if str(uid).isdigit() else None
            mentions.append(m.mention if m else f"*(unknown user: {uid})*")
        return ", ".join(mentions) if mentions else "*(none)*"

    e = discord.Embed(title="🔑  ACCESS CONTROL", color=PRIMARY)
    e.add_field(name="🛡️ Admin Roles", value=_format_role_list(g, ar_raw), inline=False)
    e.add_field(name="👤 Admin Users", value=_fmt_users(au_raw),            inline=False)
    e.add_field(name="⚔️ Mod Roles",   value=_format_role_list(g, mr_raw), inline=False)
    e.add_field(name="👤 Mod Users",   value=_fmt_users(mu_raw),            inline=False)
    e.add_field(
        name="ℹ️  Permissions",
        value=(
             "**Admin** — full access to `/admin` panel.\n"
             "**Mod** — access to Force Actions only.\n"
             "Supports multiple roles & specific users.\n"
             "Use the dropdowns below — Discord's native pickers let you\n"
             "select by @role or @user mention directly."
        ),
        inline=False,
    )
    return e, OwnerAdminSetView(gid)


async def _build_migration_panel(i: discord.Interaction):
    # -----
    # CHANGE 1: Migration Panel Improvements
    # - Uses Discord native <t:TIMESTAMP:R> timestamps so Discord auto-renders
    #   them as "5 minutes ago" / "2 days ago" — no more raw second counts.
    # - Active threshold bumped to _INSTANCE_ACTIVE_THRESHOLD_S (90s) so a
    #   single missed 15s heartbeat doesn't falsely move an instance to History.
    # - Inactive instances shown in History with their last-seen timestamp.
    # -----
    gid  = str(i.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        instances = await conn.fetch(
            "SELECT instance_id, role, hostname, started_at, last_heartbeat, force_shutdown "
            "FROM bot_instances WHERE guild_id=$1 ORDER BY started_at",
            gid,
        )
    current_id = getattr(i.client, 'instance_id', '?')

    e = discord.Embed(title="🔀  DEPLOYMENT MIGRATION", color=PRIMARY)
    e.add_field(
        name="ℹ️  How it works",
        value=(
             "Start a new worker pointing to the **same database**.\n"
             "It will register as **STANDBY** automatically.\n"
             "Click **Transfer Leadership** to promote it and shut down the old one."
        ),
        inline=False,
    )

    if not instances:
        e.add_field(name="Active Instances", value="No instances registered.", inline=False)
    else:
        active_lines  = []
        history_lines = []
        now = datetime.now(timezone.utc)

        for inst in instances:
            iid  = inst["instance_id"]
            role = inst["role"]
            host = inst["hostname"] or "?"
            hb   = inst["last_heartbeat"]

            if hb and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)

            age_s = (now - hb).total_seconds() if hb else 999_999

            # CHANGE 1: Discord native relative timestamp — no manual conversion.
            # Format: <t:UNIX:R> → Discord renders as "2 minutes ago" live.
            if hb:
                ts_display = f"<t:{int(hb.timestamp())}:R>"
            else:
                ts_display = "Never"

            status_icon = "🟢" if age_s < _INSTANCE_ACTIVE_THRESHOLD_S else "⚫"
            role_icon   = "👑" if role == "LEADER" else "⏳"
            me_marker   = "  ← *this instance*" if iid == current_id else ""

            line = (
                f"{status_icon} {role_icon} `{iid}`\n"
                f"   Role: **{role}** │ Host: `{host}` │ Last Seen: {ts_display}{me_marker}"
            )

            if age_s < _INSTANCE_ACTIVE_THRESHOLD_S:
                active_lines.append(line)
            else:
                history_lines.append(line)

        if active_lines:
            e.add_field(name="🟢 Active Instances", value="\n\n".join(active_lines), inline=False)
        else:
            e.add_field(name="🟢 Active Instances", value="No instances currently online.", inline=False)

        if history_lines:
            e.add_field(name="⚫ History / Offline", value="\n\n".join(history_lines), inline=False)

    return e, OwnerMigrationView(gid, list(instances) if instances else [])


# -----
# INTERACTIVE VIEWS
# -----
class OwnerMainView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📊 Bot Stats",   style=discord.ButtonStyle.secondary, row=0)
    async def stats_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_stats_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="💾 Backup",      style=discord.ButtonStyle.secondary, row=0)
    async def backup_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_backup_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔄 Sync Backup", style=discord.ButtonStyle.secondary, row=0)
    async def sync_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_sync_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔑 Admin Set",   style=discord.ButtonStyle.primary,   row=1)
    async def admin_set_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_admin_set_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔀 Migration",   style=discord.ButtonStyle.primary,   row=1)
    async def migration_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_migration_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerStatsView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_stats_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerBackupView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📸 Create Backup Now", style=discord.ButtonStyle.success)
    async def create_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        gid = self.guild_id
        try:
            include_full = await get_config_or_none(gid, "owner.backup_full") == "true"
            backup_doc = await create_backup(
                gid,
                initiated_by=str(i.user.id),
                backup_type="manual",
                include_user_data=include_full,
            )
            ch_id = await get_config_or_none(gid, "owner.backup_channel_id")
            if ch_id:
                channel = i.guild.get_channel(int(ch_id))
                if channel:
                    msg = await send_backup_to_channel(channel, backup_doc)
                    if msg:
                        await set_backup_message_id(backup_doc["backup_id"], str(msg.id))
            e, v = await _build_backup_panel(i)
            e.add_field(
                name="✅ Backup Created",
                value=f"ID: `{backup_doc['backup_id']}`",
                inline=False,
            )
            await i.edit_original_response(embed=e, view=v)
        except Exception:
            logger.exception(f"[Owner] Manual backup failed for guild {self.guild_id}")
            await i.edit_original_response(
                embed=error_embed("Backup failed. Check logs for details."),
                view=OwnerBackupView(gid),
            )

    @discord.ui.button(label="⚙️ Configure Backup", style=discord.ButtonStyle.secondary)
    async def config_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_backup_config_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back",              style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


class _BackupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="✏️ Change backup channel...",
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        await set_config(self.guild_id, "owner.backup_channel_id", str(self.values[0].id), str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _BackupFreqSelect(discord.ui.Select):
    def __init__(self, guild_id: str, current: int):
        super().__init__(
            placeholder="✏️ Change backup frequency...",
            options=[
                discord.SelectOption(label="Every 12 hours", value="12",  default=(current == 12)),
                discord.SelectOption(label="Every 24 hours", value="24",  default=(current == 24)),
                discord.SelectOption(label="Every 48 hours", value="48",  default=(current == 48)),
                discord.SelectOption(label="Every 7 days",   value="168", default=(current == 168)),
            ],
            row=1,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        await set_config(self.guild_id, "owner.backup_interval_hours", self.values[0], str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ConfirmDisableBackupView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm Disable", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await set_config(self.guild_id, "owner.backup_auto_enabled", "false", str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ConfirmToggleFullView(discord.ui.View):
    def __init__(self, guild_id: str, new_val: str):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.new_val  = new_val

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await set_config(self.guild_id, "owner.backup_full", self.new_val, str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class OwnerBackupConfigView(discord.ui.View):
    def __init__(self, guild_id: str, current_hours: int, is_full: bool, enabled: bool):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.is_full  = is_full
        self.enabled  = enabled
        self.add_item(_BackupChannelSelect(guild_id))
        self.add_item(_BackupFreqSelect(guild_id, current_hours))

        toggle_lbl   = "🗜️ Config Only" if is_full else "📦 Full Backup"
        toggle_btn   = discord.ui.Button(label=toggle_lbl, style=discord.ButtonStyle.secondary, row=2)
        toggle_btn.callback = self._toggle_full
        self.add_item(toggle_btn)

        dis_lbl   = "⏸️ Disable Auto-Backup" if enabled else "▶️ Enable Auto-Backup"
        dis_style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
        dis_btn   = discord.ui.Button(label=dis_lbl, style=dis_style, row=2)
        dis_btn.callback = self._toggle_enabled
        self.add_item(dis_btn)

        back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    async def _toggle_full(self, i: discord.Interaction):
        if not self.is_full:
            e = confirm_embed("Enable Full Backup?", "Backups will include user points and inventory. Files will be larger.")
            await i.response.edit_message(embed=e, view=_ConfirmToggleFullView(self.guild_id, "true"))
        else:
            e = confirm_embed("Switch to Config-Only?", "User data will no longer be included in backups.")
            await i.response.edit_message(embed=e, view=_ConfirmToggleFullView(self.guild_id, "false"))

    async def _toggle_enabled(self, i: discord.Interaction):
        if self.enabled:
            e = confirm_embed("Disable Auto-Backup?", "Automatic backups will stop running. Existing backups are unaffected.")
            await i.response.edit_message(embed=e, view=_ConfirmDisableBackupView(self.guild_id))
        else:
            await set_config(self.guild_id, "owner.backup_auto_enabled", "true", str(i.user.id))
            e, v = await _build_backup_config_panel(i)
            await i.response.edit_message(embed=e, view=v)

    async def _back(self, i: discord.Interaction):
        await i.response.defer()
        e, v = await _build_backup_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# RESTORE/SYNC VIEWS
# -----
class BackupIDModal(discord.ui.Modal, title="Restore from Backup ID"):
    backup_id_input = discord.ui.TextInput(
        label="Backup ID",
        placeholder="e.g. 2M-20260325-A4F2",
        max_length=20,
    )

    def __init__(self, guild_id: str):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, i: discord.Interaction):
        bid = self.backup_id_input.value.strip().upper()
        logger.info(f"[Owner] BackupIDModal submitted: backup_id={bid}  |  user={i.user.id}  |  guild={self.guild_id}")
        row = await get_backup_by_id(self.guild_id, bid)
        if not row:
            return await i.response.send_message(
                embed=error_embed(f"Backup ID `{bid}` not found in this server's backup log."),
                ephemeral=True,
            )
        ch_id  = await get_config_or_none(self.guild_id, "owner.backup_channel_id")
        msg_id = row.get("channel_msg_id")
        if not ch_id or not msg_id:
            return await i.response.send_message(
                embed=error_embed(f"No linked message for `{bid}`. Use **📁 Upload File** instead."),
                ephemeral=True,
            )
        channel = i.guild.get_channel(int(ch_id))
        if not channel:
            return await i.response.send_message(
                embed=error_embed("Backup channel not found."), ephemeral=True
            )

        try:
            msg        = await channel.fetch_message(int(msg_id))
            attachment = next((a for a in msg.attachments if a.filename.endswith(".json")), None)
            if not attachment:
                raise ValueError("No JSON attachment on backup message.")
            data = json.loads(await attachment.read())
        except Exception:
            logger.exception(f"[Owner] Failed to fetch backup message for bid={bid}  |  channel={ch_id}  |  msg={msg_id}")
            return await i.response.send_message(
                embed=error_embed(f"Failed to fetch backup. Check logs for details."), ephemeral=True
            )

        valid, warnings = validate_backup_file(data, self.guild_id)
        if not valid:
            return await i.response.send_message(
                embed=error_embed(f"Validation failed: {warnings[0]}"), ephemeral=True
            )

        await i.response.send_message(
            embed=_sync_confirm_embed(data, warnings),
            view=SyncConfirmView(self.guild_id, data),
            ephemeral=True,
        )


class SyncConfirmView(discord.ui.View):
    def __init__(self, guild_id: str, backup_doc: dict):
        super().__init__(timeout=120)
        self.guild_id   = guild_id
        self.backup_doc = backup_doc

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm Restore", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        bid = self.backup_doc["backup_id"]
        logger.warning(
            f"[Owner] ⚠️ Restore CONFIRMED  |  backup_id={bid}  |  "
            f"user={i.user.id}  |  guild={self.guild_id}"
        )

        try:
            pre_id, pre_sync_doc = await restore_backup(self.guild_id, self.backup_doc, str(i.user.id))
        except Exception:
            logger.exception(f"[Owner] Restore FAILED for backup_id={bid}  |  guild={self.guild_id}")
            return await i.edit_original_response(
                embed=error_embed("Restore failed unexpectedly. A pre-sync backup may have been created. Check logs."),
                view=None,
            )

        ch_id = await get_config_or_none(self.guild_id, "owner.backup_channel_id")
        if ch_id:
            channel = i.guild.get_channel(int(ch_id))
            if channel:
                # CVE-2M-017: Send the pre-sync backup file to channel as a real safety net
                try:
                    msg = await send_backup_to_channel(channel, pre_sync_doc)
                    if msg:
                        await set_backup_message_id(pre_sync_doc["backup_id"], str(msg.id))
                except Exception:
                    logger.exception(f"[Owner] Failed to post pre-sync backup {pre_id} to channel {ch_id}.")

                n_keys = len(self.backup_doc["payload"].get("bot_config", []))
                try:
                    await channel.send(
                        f"🔄 SYNC RESTORE\n"
                        f"Backup ID     : `{bid}`\n"
                        f"Initiated by  : {i.user.mention}  │  <t:{int(i.created_at.timestamp())}:f>\n"
                        f"Keys restored : {n_keys}  │  Checksum: ✅\n"
                        f"Pre-sync save : `{pre_id}`"
                    )
                except discord.HTTPException:
                    logger.warning(f"[Owner] Could not send restore audit message to channel {ch_id}.")

        await i.edit_original_response(
            embed=success_embed(
                f"Restore complete!\n"
                f"Config replaced with `{bid}`.\n"
                f"Pre-sync backup saved as `{pre_id}`."
            ),
            view=None,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_sync_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerSyncView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📋 Enter Backup ID",    style=discord.ButtonStyle.secondary)
    async def enter_id_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(BackupIDModal(self.guild_id))

    @discord.ui.button(label="📁 Upload Backup File", style=discord.ButtonStyle.secondary)
    async def upload_btn(self, i: discord.Interaction, _: discord.ui.Button):
        gid = self.guild_id
        await i.response.edit_message(
            embed=discord.Embed(
                title="📁  Upload Backup File",
                description=(
                    "Send your backup `.json` file as a message **in this channel** "
                    "within **60 seconds**.\n\n"
                    "The message will be deleted automatically after processing."
                ),
                color=PRIMARY,
            ),
            view=None,
        )

        def _check(m: discord.Message) -> bool:
            return m.author.id == i.user.id and m.channel.id == i.channel_id and len(m.attachments) > 0

        try:
            msg = await i.client.wait_for("message", check=_check, timeout=60.0)
        except asyncio.TimeoutError:
            logger.debug(f"[Owner] File upload timed out for user {i.user.id} in guild {gid}.")
            e, v = await _build_sync_panel(i)
            return await i.edit_original_response(embed=e, view=v)

        try:
            # CVE-2M-022: Validate file extension and size before reading
            attachment = next(
                (a for a in msg.attachments if a.filename.endswith(".json") and a.size < 10_000_000),
                None
            )
            if not attachment:
                raise ValueError("No valid .json attachment under 10MB found.")
            data = json.loads(await attachment.read())
        except Exception:
            logger.exception(f"[Owner] Failed to parse uploaded backup from user {i.user.id} in guild {gid}.")
            try:
                await msg.delete()
            except Exception:
                pass
            return await i.edit_original_response(
                embed=error_embed("Could not read the file. Ensure it is a valid 2M backup JSON."),
                view=OwnerSyncView(gid),
            )

        try:
            await msg.delete()
        except discord.HTTPException:
            logger.warning(f"[Owner] Could not delete upload message {msg.id} from user {i.user.id}.")

        valid, warnings = validate_backup_file(data, str(i.guild_id))
        if not valid:
            return await i.edit_original_response(
                embed=error_embed(f"Backup invalid: {warnings[0]}"),
                view=OwnerSyncView(gid),
            )

        await i.edit_original_response(
            embed=_sync_confirm_embed(data, warnings),
            view=SyncConfirmView(gid, data),
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# ADMIN IDENTITY VIEWS
# -----
class _AdminRoleSelect(discord.ui.RoleSelect):
    # -----
    # CHANGE 3: Multi-role admin/mod configuration.
    # Uses Discord's native RoleSelect (searchable by @role mention) instead of
    # a dropdown of pre-listed IDs. Supports up to 25 roles simultaneously.
    # min_values=0 so the user can open and close the panel without being
    # forced to re-select. A new selection replaces the stored list.
    # -----
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="🛡️ Set Admin Roles… (select to replace list)",
            row=0,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(r.id) for r in self.values]
        await set_config(self.guild_id, "system.admin_role_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Admin roles set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _AdminUserSelect(discord.ui.UserSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="👤 Set Admin Users… (select to replace list)",
            row=1,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(u.id) for u in self.values]
        await set_config(self.guild_id, "system.admin_user_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Admin users set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ModRoleSelect(discord.ui.RoleSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="⚔️ Set Mod Roles… (select to replace list)",
            row=2,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(r.id) for r in self.values]
        await set_config(self.guild_id, "system.mod_role_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Mod roles set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ModUserSelect(discord.ui.UserSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="👤 Set Mod Users… (select to replace list)",
            row=3,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(u.id) for u in self.values]
        await set_config(self.guild_id, "system.mod_user_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Mod users set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class OwnerAdminSetView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(_AdminRoleSelect(guild_id))
        self.add_item(_AdminUserSelect(guild_id))
        self.add_item(_ModRoleSelect(guild_id))
        self.add_item(_ModUserSelect(guild_id))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# CLUSTER MIGRATION VIEWS
# -----
class MigrationTargetSelect(discord.ui.Select):
    def __init__(self, instances, action: str):
        self.action = action
        opts = []
        for inst in instances:
            iid  = inst["instance_id"]
            role = inst["role"]
            hb   = inst["last_heartbeat"]
            ts   = f"<t:{int(hb.timestamp())}:R>" if hb else "never"
            opts.append(discord.SelectOption(
                label=f"{iid} ({role})",
                description=f"Host: {inst['hostname'] or '?'} — last seen {ts[:50]}",
                value=iid,
            ))
        if not opts:
            opts = [discord.SelectOption(label="No instances available", value="none")]
        super().__init__(
            placeholder=f"Select instance to {action}...",
            options=opts,
        )

    async def callback(self, i: discord.Interaction):
        target = self.values[0]
        if target == "none":
            return await i.response.send_message("No instance selected.", ephemeral=True)

        pool = await get_pool()
        gid  = str(i.guild_id)

        if self.action == "transfer":
            async with pool.acquire() as conn:
                target_row = await conn.fetchrow("SELECT role FROM bot_instances WHERE instance_id=$1", target)
                if not target_row:
                    return await i.response.send_message(embed=error_embed("Instance not found."), ephemeral=True)
                if target_row["role"] == "LEADER":
                    return await i.response.send_message(embed=error_embed("That instance is already the LEADER."), ephemeral=True)

                await conn.execute("UPDATE bot_instances SET role='STANDBY' WHERE guild_id=$1 AND role='LEADER'", gid)
                await conn.execute("UPDATE bot_instances SET role='LEADER' WHERE instance_id=$1", target)

            logger.info(f"[Owner] Leadership transferred to instance {target} by user {i.user.id} in guild {gid}.")
            e, v = await _build_migration_panel(i)
            e.add_field(name="✅ Transfer Complete", value=f"Instance `{target}` is now LEADER.\nThe old leader will shut down within ~20 seconds.", inline=False)
            await i.response.edit_message(embed=e, view=v)

        elif self.action == "shutdown":
            async with pool.acquire() as conn:
                await conn.execute("UPDATE bot_instances SET force_shutdown=TRUE WHERE instance_id=$1", target)
            logger.info(f"[Owner] Force shutdown signal sent to instance {target} by user {i.user.id} in guild {gid}.")
            e, v = await _build_migration_panel(i)
            e.add_field(name="⚠️ Shutdown Signal Sent", value=f"Instance `{target}` will shut down within ~15 seconds.", inline=False)
            await i.response.edit_message(embed=e, view=v)


class MigrationActionView(discord.ui.View):
    def __init__(self, gid: str, instances: list, action: str):
        super().__init__(timeout=120)
        self.gid = gid
        self.add_item(MigrationTargetSelect(instances, action))

        btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
        async def go_back(inter):
            await inter.response.defer()
            e, v = await _build_migration_panel(inter)
            await inter.edit_original_response(embed=e, view=v)
        btn.callback = go_back
        self.add_item(btn)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class OwnerMigrationView(discord.ui.View):
    # -----
    # CHANGE 2: Full Migration Export (Cross-DB / Cross-Provider)
    # Added "🚀 Full Migration Export" button that creates a full backup
    # (config + all user data) and sends it as a downloadable file.
    # Flow: Worker A exports → upload JSON to Worker B → /owner > Sync Backup
    # This replaces the old static "how it works" text with a real action.
    # -----
    def __init__(self, guild_id: str, instances: list):
        super().__init__(timeout=300)
        self.guild_id  = guild_id
        self.instances = instances

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="👑 Transfer Leadership", style=discord.ButtonStyle.primary, row=0)
    async def transfer_btn(self, i: discord.Interaction, _: discord.ui.Button):
        now = datetime.now(timezone.utc)
        # Only show instances that are actually alive (active threshold)
        standby = [
            inst for inst in self.instances
            if inst["role"] == "STANDBY"
            and inst["last_heartbeat"]
            and (now - inst["last_heartbeat"].replace(tzinfo=timezone.utc)
                 if inst["last_heartbeat"].tzinfo is None
                 else now - inst["last_heartbeat"]).total_seconds() < _INSTANCE_ACTIVE_THRESHOLD_S
        ]
        if not standby:
            return await i.response.send_message(
                embed=error_embed("No active STANDBY instance found.\nStart a new worker pointing to the **same database**, then refresh."),
                ephemeral=True,
            )
        e = discord.Embed(title="👑  SELECT NEW LEADER", description="Choose the active STANDBY instance to promote.", color=0xFEE75C)
        await i.response.edit_message(embed=e, view=MigrationActionView(self.guild_id, standby, "transfer"))

    @discord.ui.button(label="🛑 Force Shutdown", style=discord.ButtonStyle.danger, row=0)
    async def shutdown_btn(self, i: discord.Interaction, _: discord.ui.Button):
        if not self.instances:
            return await i.response.send_message(embed=error_embed("No instances to shut down."), ephemeral=True)
        e = discord.Embed(title="🛑  FORCE SHUTDOWN", description="Choose the instance to force-shutdown.", color=0xED4245)
        await i.response.edit_message(embed=e, view=MigrationActionView(self.guild_id, self.instances, "shutdown"))

    @discord.ui.button(label="🚀 Full Migration Export", style=discord.ButtonStyle.success, row=1)
    async def full_export_btn(self, i: discord.Interaction, _: discord.ui.Button):
        # -----
        # CHANGE 2: Full migration export for cross-provider moves.
        # Creates a full backup (config + economy data), sends the .json file
        # directly as an ephemeral attachment so the owner can download it.
        # Instructions are embedded so the process is self-contained.
        # -----
        await i.response.defer(ephemeral=True)
        gid = self.guild_id
        logger.info(f"[Owner] Full migration export initiated by user {i.user.id} for guild {gid}.")
        try:
            backup_doc = await create_backup(
                gid,
                initiated_by=str(i.user.id),
                backup_type="migration_export",
                include_user_data=True,   # Full export: config + all economy data
            )
        except Exception:
            logger.exception(f"[Owner] Full migration export failed for guild {gid}.")
            return await i.followup.send(
                embed=error_embed("Export failed. Check logs for details."),
                ephemeral=True,
            )

        bid = backup_doc["backup_id"]
        n_cfg   = len(backup_doc["payload"].get("bot_config", []))
        n_users = len(backup_doc["payload"].get("users", []))
        n_inv   = len(backup_doc["payload"].get("user_inventory", []))

        file_bytes = json.dumps(backup_doc, indent=2, default=str).encode()
        file_obj   = discord.File(io.BytesIO(file_bytes), filename=f"{bid}.json")

        e = discord.Embed(title="🚀  FULL MIGRATION EXPORT", color=0x57F287)
        e.add_field(
            name="📦 What's included",
            value=(
                f"**Config keys:** {n_cfg}\n"
                f"**Users:** {n_users}\n"
                f"**Inventory records:** {n_inv}\n"
                f"**Backup ID:** `{bid}`"
            ),
            inline=False,
        )
        e.add_field(
            name="📋 Next steps — moving to a new provider",
            value=(
                "1. Download the `.json` file attached below.\n"
                "2. Deploy your new bot (Worker B) with the new `DATABASE_URL`.\n"
                "3. On Worker B: run `/owner` → **🔄 Sync Backup** → **📁 Upload Backup File**.\n"
                "4. Upload this file. Worker B will restore all config and economy data.\n"
                "5. Once verified, shut down Worker A via **🛑 Force Shutdown**.\n\n"
                "⚠️ This file contains all user point balances. Keep it secure."
            ),
            inline=False,
        )
        logger.info(f"[Owner] Full migration export {bid} sent to user {i.user.id}  |  cfg={n_cfg}  users={n_users}.")
        await i.followup.send(embed=e, file=file_obj, ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_migration_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# DISCORD COG MOUNTING
# -----
class OwnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="owner", description="Owner-only control panel.")
    async def owner_cmd(self, i: discord.Interaction):
        # BUG FIX: Defer BEFORE any DB queries to avoid 3-second interaction timeout.
        if not await is_server_owner(i):
            await i.response.send_message("This command is restricted to the server owner.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        e, v = await _build_main_panel(i)
        await i.followup.send(embed=e, view=v, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerCog(bot))

+++ cogs/owner.py (修改后)
# =====
# MODULE: cogs/owner.py
# =====
# Architecture Overview:
# Provides the Server Owner Control Panel, accessible only to the guild owner.
# Handles backups, statistics, deployment migration, and access control setup.
#
# BUG FIX (CVE-2M-020):
# /owner and all DB-heavy panel builders now call i.response.defer() before
# making async DB queries. Discord's 3-second response window is easily exceeded
# by cold DB pool connections; deferring extends the window to 15 minutes.
# =====
import asyncio
import io
import json
import logging
import re
import time
# BUG FIX: 'datetime' and 'timezone' were missing — caused NameError crash
# every time _build_migration_panel() was called to render the panel.
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from backup.manager import (
    create_backup,
    get_backup_by_id,
    get_backup_log,
    restore_backup,
    set_backup_message_id,
    validate_backup_file,
)
from backup.scheduler import send_backup_to_channel
from config.manager import get_config_or_none, set_config, invalidate_guild_cache
from db.pool import get_pool
from guards.checks import is_server_owner
from utils.embeds import PRIMARY, confirm_embed, error_embed, success_embed
from utils.time import format_relative, format_uptime

logger = logging.getLogger(__name__)

# Active threshold: heartbeat fires every 15s, so 90s = 6 missed beats before
# we consider an instance offline. More forgiving than raw LEADER_TIMEOUT (60s).
_INSTANCE_ACTIVE_THRESHOLD_S = 90


# -----------------------------------------------------------------------------
# EMBED CMS CONFIGURATION
# -----------------------------------------------------------------------------
# List of all editable admin embeds for the UI Editor dropdown
ADMIN_EMBED_KEYS = [
    ("embed.admin.main", "🏠 Main Menu"),
    ("embed.admin.main_guide", "📖 Main Guide"),
    ("embed.admin.economy", "⚙️ Economy Panel"),
    ("embed.admin.economy_guide", "⚙️ Economy Guide"),
    ("embed.admin.decay", "⏱️ Decay Panel"),
    ("embed.admin.decay_guide", "⏱️ Decay Guide"),
    ("embed.admin.host", "🎮 Host Panel"),
    ("embed.admin.host_guide", "🎮 Host Guide"),
    ("embed.admin.vote", "🗳️ Vote Panel"),
    ("embed.admin.vote_guide", "🗳️ Vote Guide"),
    ("embed.admin.channel", "📡 Channel Panel"),
    ("embed.admin.channel_guide", "📡 Channel Guide"),
    ("embed.admin.system", "🔧 System Panel"),
    ("embed.admin.system_guide", "🔧 System Guide"),
    ("embed.admin.force", "⚡ Force Panel"),
    ("embed.admin.force_guide", "⚡ Force Guide"),
    ("embed.admin.shop", "🛒 Shop Panel"),
    ("embed.admin.shop_guide", "🛒 Shop Guide"),
    ("embed.admin.item_control", "📦 Item Control"),
]


# -----
# HELPER FUNCTIONS
# -----
async def _db_latency_ms() -> int:
    pool = await get_pool()
    t0 = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return int((time.perf_counter() - t0) * 1000)


def _format_role_list(guild: discord.Guild, raw_val: str | None) -> str:
    # -----
    # BUG FIX: Replaces the old _role_display() which only handled a single
    # role ID string. Now correctly parses JSON arrays stored by the multi-role
    # selects, and gracefully falls back for legacy single-ID strings.
    # -----
    if not raw_val or raw_val in ("null", "[]"):
        return "*(not set)*"
    try:
        ids = json.loads(raw_val)
        if not isinstance(ids, list):
            ids = [ids]
    except (json.JSONDecodeError, TypeError):
        ids = [raw_val]

    mentions = []
    for rid in ids:
        role = guild.get_role(int(rid)) if str(rid).isdigit() else None
        mentions.append(role.mention if role else f"*(unknown: {rid})*")
    return ", ".join(mentions) if mentions else "*(not set)*"


def _sync_confirm_embed(backup_doc: dict, warnings: list[str]) -> discord.Embed:
    e = discord.Embed(title="🔄  CONFIRM SYNC RESTORE", color=0xFEE75C)
    e.add_field(name="Backup ID",   value=f"`{backup_doc['backup_id']}`",                         inline=True)
    e.add_field(name="Created",     value=str(backup_doc.get("created_at", "?"))[:19],             inline=True)
    e.add_field(name="Type",        value=backup_doc.get("backup_type", "?"),                      inline=True)
    e.add_field(name="Config Keys", value=str(len(backup_doc["payload"].get("bot_config", []))),   inline=True)
    e.add_field(name="Checksum",    value="✅ Valid",                                              inline=True)
    e.add_field(name="\u200b",      value="\u200b",                                                inline=True)
    e.add_field(
        name="⚠️  This will overwrite the active configuration.",
        value="User data (points, inventory) is **not** affected unless the backup includes it.",
        inline=False,
    )
    if warnings:
        e.add_field(name="Warnings", value="\n".join(f"• {w}" for w in warnings), inline=False)
    return e


# -----
# PANEL BUILDERS
# -----
async def _build_main_panel(i: discord.Interaction):
    g     = i.guild
    gid   = str(g.id)
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")
    icon  = {"ACTIVE": "🟢", "PAUSED": "🟡", "UNCONFIGURED": "🔴"}.get(state, "⚪")

    # BUG FIX: Use _format_role_list() instead of old _role_display() which
    # only accepted a single ID and broke silently with JSON array values.
    e = discord.Embed(title="👑  TWO MOON — OWNER PANEL", color=PRIMARY)
    e.add_field(name="Server",      value=g.name,                            inline=True)
    e.add_field(name="Uptime",      value=format_uptime(),                   inline=True)
    e.add_field(name="State",       value=f"{icon} {state}",                 inline=True)
    e.add_field(name="Admin Roles", value=_format_role_list(g, ar_raw),      inline=True)
    e.add_field(name="Mod Roles",   value=_format_role_list(g, mr_raw),      inline=True)
    e.add_field(name="\u200b",      value="\u200b",                          inline=True)
    return e, OwnerMainView(gid)


async def _build_stats_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    pool = await get_pool()
    t0   = time.perf_counter()
    async with pool.acquire() as conn:
        s = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*)                       FROM users          WHERE guild_id=$1)::int AS total_users,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1)::int AS total_events,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1 AND is_valid=1)::int AS valid_events,
                (SELECT COUNT(*)                       FROM user_inventory WHERE guild_id=$1
                    AND (expires_at IS NULL OR expires_at > NOW()))::int                          AS active_rentals,
                (SELECT COUNT(*)                       FROM bet_pools      WHERE guild_id=$1 AND status='open')::int AS open_bets,
                (SELECT COALESCE(SUM(raw_points),  0)  FROM users          WHERE guild_id=$1)     AS total_points,
                (SELECT COALESCE(SUM(tax_amount),  0)  FROM bet_pools      WHERE guild_id=$1)     AS burned_points,
                (SELECT COUNT(*)                       FROM events         WHERE guild_id=$1
                    AND started_at > NOW() - INTERVAL '7 days')::int                              AS events_7d
            """,
            gid,
        )
    db_ms = int((time.perf_counter() - t0) * 1000)

    try:
        pool_cur, pool_max = pool.get_size(), pool.get_max_size()
    except AttributeError:
        pool_cur, pool_max = "?", 10

    last_bk = (await get_backup_log(gid, limit=1) or [None])[0]

    e = discord.Embed(title="📊  BOT STATISTICS", color=PRIMARY)
    e.add_field(name="🖥️ System", value=(
        f"**Uptime:** {format_uptime()}\n"
        f"**Discord Latency:** {round(i.client.latency * 1000)}ms\n"
        f"**DB Latency:** {db_ms}ms\n"
        f"**DB Pool:** {pool_cur} / {pool_max} connections"
    ), inline=False)
    e.add_field(name="📦 Data", value=(
        f"**Users:** {s['total_users']:,}\n"
        f"**Events:** {s['total_events']:,}  │  Valid: {s['valid_events']:,}\n"
        f"**Active Rentals:** {s['active_rentals']}\n"
        f"**Open Bet Pools:** {s['open_bets']}"
    ), inline=False)
    e.add_field(name="💰 Economy", value=(
        f"**Points in Circulation:** {float(s['total_points']):,.0f} pts\n"
        f"**Points Burned (Bet Tax):** {float(s['burned_points']):,.0f} pts\n"
        f"**Events (last 7 days):** {s['events_7d']}"
    ), inline=False)

    if last_bk:
        e.add_field(name="💾 Last Backup", value=(
            f"**ID:** `{last_bk['backup_id']}`\n"
            f"**When:** {format_relative(last_bk['created_at'])}\n"
            f"**Type:** {last_bk['backup_type']}"
        ), inline=False)
    else:
        e.add_field(name="💾 Backup", value="No backups recorded yet.", inline=False)
    return e, OwnerStatsView(gid)


async def _build_backup_panel(i: discord.Interaction):
    gid    = str(i.guild_id)
    ch_id  = await get_config_or_none(gid, "owner.backup_channel_id")
    intv   = await get_config_or_none(gid, "owner.backup_interval_hours", int) or 24
    en_raw = await get_config_or_none(gid, "owner.backup_auto_enabled")
    en     = en_raw != "false" if en_raw is not None else True
    logs   = await get_backup_log(gid, limit=5)
    ch     = i.guild.get_channel(int(ch_id)) if ch_id else None

    e = discord.Embed(title="💾  BACKUP CONFIGURATION", color=PRIMARY)
    e.add_field(name="Auto-Backup", value="✅ Enabled"  if en  else "⏸️ Disabled",     inline=True)
    e.add_field(name="Frequency",   value=f"Every {intv}h",                             inline=True)
    e.add_field(name="Channel",     value=ch.mention if ch else "*(not set)*",          inline=True)
    if logs:
        lines = [
            f"`{r['backup_id']}`  │  {str(r['created_at'])[:16].replace('T',' ')}  │  {r['config_keys']} keys  │  {r['backup_type']}"
            for r in logs
        ]
        e.add_field(name="Recent Backups", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="Recent Backups", value="No backups yet.", inline=False)
    return e, OwnerBackupView(gid)


async def _build_backup_config_panel(i: discord.Interaction):
    gid    = str(i.guild_id)
    ch_id  = await get_config_or_none(gid, "owner.backup_channel_id")
    intv   = await get_config_or_none(gid, "owner.backup_interval_hours", int) or 24
    en_raw = await get_config_or_none(gid, "owner.backup_auto_enabled")
    en     = en_raw != "false" if en_raw is not None else True
    fl_raw = await get_config_or_none(gid, "owner.backup_full")
    full   = fl_raw == "true" if fl_raw else False
    ch     = i.guild.get_channel(int(ch_id)) if ch_id else None

    e = discord.Embed(title="⚙️  BACKUP SETTINGS", color=PRIMARY)
    e.add_field(name="Auto-Backup", value="✅ Enabled"  if en   else "⏸️ Disabled",            inline=True)
    e.add_field(name="Frequency",   value=f"Every {intv}h",                                     inline=True)
    e.add_field(name="Channel",     value=ch.mention if ch else "*(not set)*",                  inline=True)
    e.add_field(name="Coverage",    value="Full (config + user data)" if full else "Config only", inline=True)
    return e, OwnerBackupConfigView(gid, intv, full, en)


async def _build_sync_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        n_keys   = await conn.fetchval("SELECT COUNT(*) FROM bot_config WHERE guild_id=$1", gid)
        last_mod = await conn.fetchval(
            "SELECT changed_at FROM config_audit_log WHERE guild_id=$1 ORDER BY changed_at DESC LIMIT 1", gid
        )
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"

    e = discord.Embed(title="🔄  SYNC BACKUP — Restore Configuration", color=PRIMARY)
    e.add_field(name="Active Config", value=(
        f"**State:** {state}  │  **Keys:** {n_keys}\n"
        f"**Last Modified:** {format_relative(last_mod) if last_mod else 'never'}"
    ), inline=False)
    e.add_field(name="Use this panel to:", value=(
        "• Restore config from a previous backup\n"
        "• Migrate from another Railway container\n"
        "• Roll back after a bad config change"
    ), inline=False)
    return e, OwnerSyncView(gid)


async def _build_admin_set_panel(i: discord.Interaction):
    gid  = str(i.guild_id)
    g    = i.guild
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")
    au_raw = await get_config_or_none(gid, "system.admin_user_id")
    mu_raw = await get_config_or_none(gid, "system.mod_user_id")

    def _fmt_users(raw_val):
        if not raw_val or raw_val in ("null", "[]"):
            return "*(not set)*"
        try:
            ids = json.loads(raw_val)
            if not isinstance(ids, list):
                ids = [ids]
        except (json.JSONDecodeError, TypeError):
            ids = [raw_val]
        mentions = []
        for uid in ids:
            m = g.get_member(int(uid)) if str(uid).isdigit() else None
            mentions.append(m.mention if m else f"*(unknown user: {uid})*")
        return ", ".join(mentions) if mentions else "*(none)*"

    e = discord.Embed(title="🔑  ACCESS CONTROL", color=PRIMARY)
    e.add_field(name="🛡️ Admin Roles", value=_format_role_list(g, ar_raw), inline=False)
    e.add_field(name="👤 Admin Users", value=_fmt_users(au_raw),            inline=False)
    e.add_field(name="⚔️ Mod Roles",   value=_format_role_list(g, mr_raw), inline=False)
    e.add_field(name="👤 Mod Users",   value=_fmt_users(mu_raw),            inline=False)
    e.add_field(
        name="ℹ️  Permissions",
        value=(
             "**Admin** — full access to `/admin` panel.\n"
             "**Mod** — access to Force Actions only.\n"
             "Supports multiple roles & specific users.\n"
             "Use the dropdowns below — Discord's native pickers let you\n"
             "select by @role or @user mention directly."
        ),
        inline=False,
    )
    return e, OwnerAdminSetView(gid)


async def _build_migration_panel(i: discord.Interaction):
    # -----
    # CHANGE 1: Migration Panel Improvements
    # - Uses Discord native <t:TIMESTAMP:R> timestamps so Discord auto-renders
    #   them as "5 minutes ago" / "2 days ago" — no more raw second counts.
    # - Active threshold bumped to _INSTANCE_ACTIVE_THRESHOLD_S (90s) so a
    #   single missed 15s heartbeat doesn't falsely move an instance to History.
    # - Inactive instances shown in History with their last-seen timestamp.
    # -----
    gid  = str(i.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        instances = await conn.fetch(
            "SELECT instance_id, role, hostname, started_at, last_heartbeat, force_shutdown "
            "FROM bot_instances WHERE guild_id=$1 ORDER BY started_at",
            gid,
        )
    current_id = getattr(i.client, 'instance_id', '?')

    e = discord.Embed(title="🔀  DEPLOYMENT MIGRATION", color=PRIMARY)
    e.add_field(
        name="ℹ️  How it works",
        value=(
             "Start a new worker pointing to the **same database**.\n"
             "It will register as **STANDBY** automatically.\n"
             "Click **Transfer Leadership** to promote it and shut down the old one."
        ),
        inline=False,
    )

    if not instances:
        e.add_field(name="Active Instances", value="No instances registered.", inline=False)
    else:
        active_lines  = []
        history_lines = []
        now = datetime.now(timezone.utc)

        for inst in instances:
            iid  = inst["instance_id"]
            role = inst["role"]
            host = inst["hostname"] or "?"
            hb   = inst["last_heartbeat"]

            if hb and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)

            age_s = (now - hb).total_seconds() if hb else 999_999

            # CHANGE 1: Discord native relative timestamp — no manual conversion.
            # Format: <t:UNIX:R> → Discord renders as "2 minutes ago" live.
            if hb:
                ts_display = f"<t:{int(hb.timestamp())}:R>"
            else:
                ts_display = "Never"

            status_icon = "🟢" if age_s < _INSTANCE_ACTIVE_THRESHOLD_S else "⚫"
            role_icon   = "👑" if role == "LEADER" else "⏳"
            me_marker   = "  ← *this instance*" if iid == current_id else ""

            line = (
                f"{status_icon} {role_icon} `{iid}`\n"
                f"   Role: **{role}** │ Host: `{host}` │ Last Seen: {ts_display}{me_marker}"
            )

            if age_s < _INSTANCE_ACTIVE_THRESHOLD_S:
                active_lines.append(line)
            else:
                history_lines.append(line)

        if active_lines:
            e.add_field(name="🟢 Active Instances", value="\n\n".join(active_lines), inline=False)
        else:
            e.add_field(name="🟢 Active Instances", value="No instances currently online.", inline=False)

        if history_lines:
            e.add_field(name="⚫ History / Offline", value="\n\n".join(history_lines), inline=False)

    return e, OwnerMigrationView(gid, list(instances) if instances else [])


# -----------------------------------------------------------------------------
# UI/EMBED EDITOR PANEL
# -----------------------------------------------------------------------------

async def _build_ui_editor_panel(i: discord.Interaction):
    """Build the UI Editor panel showing all editable admin embeds."""
    gid = str(i.guild_id)

    # Count how many embeds have been customized (not using defaults)
    pool = await get_pool()
    async with pool.acquire() as conn:
        custom_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM bot_config
            WHERE guild_id=$1 AND config_key LIKE 'embed.admin.%'
            """,
            gid
        )

    e = discord.Embed(title="🎨  UI/EMBED EDITOR", color=PRIMARY)
    e.description = (
        "Edit any admin panel embed directly from Discord.\n"
        "Changes apply immediately and are cached per-guild.\n\n"
        f"**Customized Embeds:** `{custom_count or 0}` / {len(ADMIN_EMBED_KEYS)}\n\n"
        "Select an embed below to edit its title, description, color, and thumbnail."
    )

    return e, OwnerUIEditorView(gid)


class EmbedSelect(discord.ui.StringSelect):
    """Dropdown for selecting which embed to edit."""

    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=label, value=key, emoji=key.split(".")[-1].upper()[:1])
            for key, label in ADMIN_EMBED_KEYS
        ]
        super().__init__(
            placeholder="✏️ Select an embed to edit...",
            options=options,
            row=0,
        )

    async def callback(self, i: discord.Interaction):
        embed_key = self.values[0]
        await i.response.send_modal(EmbedEditorModal(self.guild_id, embed_key))


class EmbedEditorModal(discord.ui.Modal, title="Edit Embed"):
    """Modal for editing embed properties."""

    title_input = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="Embed title (supports small-caps Unicode)",
        max_length=256,
        required=True,
    )
    description_input = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.long,
        placeholder="Embed description (supports Markdown)",
        max_length=4096,
        required=True,
    )
    color_input = discord.ui.TextInput(
        label="Color (Hex)",
        style=discord.TextStyle.short,
        placeholder="e.g., #FF5733 or 16711680",
        max_length=50,
        required=True,
    )
    thumbnail_input = discord.ui.TextInput(
        label="Thumbnail URL",
        style=discord.TextStyle.short,
        placeholder="https://i.imgur.com/your-image.gif (optional)",
        max_length=512,
        required=False,
    )

    def __init__(self, guild_id: str, embed_key: str):
        super().__init__()
        self.guild_id = guild_id
        self.embed_key = embed_key
        self.embed_label = next((label for key, label in ADMIN_EMBED_KEYS if key == embed_key), embed_key)
        self.title = f"Edit: {self.embed_label}"

    async def on_submit(self, i: discord.Interaction):
        try:
            # Parse color
            color_str = self.color_input.value.strip()
            if color_str.startswith("#"):
                color = int(color_str[1:], 16)
            else:
                color = int(color_str)

            # Build embed config JSON
            embed_config = {
                "title": self.title_input.value.strip(),
                "description": self.description_input.value.strip(),
                "color": color,
                "thumbnail": self.thumbnail_input.value.strip() or None,
            }

            # Save to database
            await set_config(
                self.guild_id,
                self.embed_key,
                json.dumps(embed_config),
                str(i.user.id),
            )

            # Invalidate cache so changes take effect immediately
            invalidate_guild_cache(self.guild_id)

            await i.response.send_message(
                embed=success_embed(f"✅ **{self.embed_label}** updated successfully!"),
                ephemeral=True,
            )
        except ValueError as ve:
            await i.response.send_message(
                embed=error_embed(f"Invalid color format. Use hex (#FF5733) or integer (16711680).\n`{ve}`"),
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception(f"[Owner] Failed to update embed {self.embed_key}")
            await i.response.send_message(
                embed=error_embed(f"Failed to update embed:\n`{exc}`"),
                ephemeral=True,
            )


class OwnerUIEditorView(discord.ui.View):
    """View for the UI Editor panel."""

    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(EmbedSelect(guild_id))

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# INTERACTIVE VIEWS
# -----
class OwnerMainView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📊 Bot Stats",   style=discord.ButtonStyle.secondary, row=0)
    async def stats_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_stats_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="💾 Backup",      style=discord.ButtonStyle.secondary, row=0)
    async def backup_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_backup_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔄 Sync Backup", style=discord.ButtonStyle.secondary, row=0)
    async def sync_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_sync_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🎨 UI Editor",   style=discord.ButtonStyle.primary,   row=0)
    async def ui_editor_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_ui_editor_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔑 Admin Set",   style=discord.ButtonStyle.primary,   row=1)
    async def admin_set_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_admin_set_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="🔀 Migration",   style=discord.ButtonStyle.primary,   row=1)
    async def migration_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_migration_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerStatsView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_stats_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerBackupView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📸 Create Backup Now", style=discord.ButtonStyle.success)
    async def create_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        gid = self.guild_id
        try:
            include_full = await get_config_or_none(gid, "owner.backup_full") == "true"
            backup_doc = await create_backup(
                gid,
                initiated_by=str(i.user.id),
                backup_type="manual",
                include_user_data=include_full,
            )
            ch_id = await get_config_or_none(gid, "owner.backup_channel_id")
            if ch_id:
                channel = i.guild.get_channel(int(ch_id))
                if channel:
                    msg = await send_backup_to_channel(channel, backup_doc)
                    if msg:
                        await set_backup_message_id(backup_doc["backup_id"], str(msg.id))
            e, v = await _build_backup_panel(i)
            e.add_field(
                name="✅ Backup Created",
                value=f"ID: `{backup_doc['backup_id']}`",
                inline=False,
            )
            await i.edit_original_response(embed=e, view=v)
        except Exception:
            logger.exception(f"[Owner] Manual backup failed for guild {self.guild_id}")
            await i.edit_original_response(
                embed=error_embed("Backup failed. Check logs for details."),
                view=OwnerBackupView(gid),
            )

    @discord.ui.button(label="⚙️ Configure Backup", style=discord.ButtonStyle.secondary)
    async def config_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_backup_config_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back",              style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


class _BackupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="✏️ Change backup channel...",
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        await set_config(self.guild_id, "owner.backup_channel_id", str(self.values[0].id), str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _BackupFreqSelect(discord.ui.Select):
    def __init__(self, guild_id: str, current: int):
        super().__init__(
            placeholder="✏️ Change backup frequency...",
            options=[
                discord.SelectOption(label="Every 12 hours", value="12",  default=(current == 12)),
                discord.SelectOption(label="Every 24 hours", value="24",  default=(current == 24)),
                discord.SelectOption(label="Every 48 hours", value="48",  default=(current == 48)),
                discord.SelectOption(label="Every 7 days",   value="168", default=(current == 168)),
            ],
            row=1,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        await set_config(self.guild_id, "owner.backup_interval_hours", self.values[0], str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ConfirmDisableBackupView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm Disable", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await set_config(self.guild_id, "owner.backup_auto_enabled", "false", str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ConfirmToggleFullView(discord.ui.View):
    def __init__(self, guild_id: str, new_val: str):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.new_val  = new_val

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await set_config(self.guild_id, "owner.backup_full", self.new_val, str(i.user.id))
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        e, v = await _build_backup_config_panel(i)
        await i.response.edit_message(embed=e, view=v)


class OwnerBackupConfigView(discord.ui.View):
    def __init__(self, guild_id: str, current_hours: int, is_full: bool, enabled: bool):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.is_full  = is_full
        self.enabled  = enabled
        self.add_item(_BackupChannelSelect(guild_id))
        self.add_item(_BackupFreqSelect(guild_id, current_hours))

        toggle_lbl   = "🗜️ Config Only" if is_full else "📦 Full Backup"
        toggle_btn   = discord.ui.Button(label=toggle_lbl, style=discord.ButtonStyle.secondary, row=2)
        toggle_btn.callback = self._toggle_full
        self.add_item(toggle_btn)

        dis_lbl   = "⏸️ Disable Auto-Backup" if enabled else "▶️ Enable Auto-Backup"
        dis_style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
        dis_btn   = discord.ui.Button(label=dis_lbl, style=dis_style, row=2)
        dis_btn.callback = self._toggle_enabled
        self.add_item(dis_btn)

        back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    async def _toggle_full(self, i: discord.Interaction):
        if not self.is_full:
            e = confirm_embed("Enable Full Backup?", "Backups will include user points and inventory. Files will be larger.")
            await i.response.edit_message(embed=e, view=_ConfirmToggleFullView(self.guild_id, "true"))
        else:
            e = confirm_embed("Switch to Config-Only?", "User data will no longer be included in backups.")
            await i.response.edit_message(embed=e, view=_ConfirmToggleFullView(self.guild_id, "false"))

    async def _toggle_enabled(self, i: discord.Interaction):
        if self.enabled:
            e = confirm_embed("Disable Auto-Backup?", "Automatic backups will stop running. Existing backups are unaffected.")
            await i.response.edit_message(embed=e, view=_ConfirmDisableBackupView(self.guild_id))
        else:
            await set_config(self.guild_id, "owner.backup_auto_enabled", "true", str(i.user.id))
            e, v = await _build_backup_config_panel(i)
            await i.response.edit_message(embed=e, view=v)

    async def _back(self, i: discord.Interaction):
        await i.response.defer()
        e, v = await _build_backup_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# RESTORE/SYNC VIEWS
# -----
class BackupIDModal(discord.ui.Modal, title="Restore from Backup ID"):
    backup_id_input = discord.ui.TextInput(
        label="Backup ID",
        placeholder="e.g. 2M-20260325-A4F2",
        max_length=20,
    )

    def __init__(self, guild_id: str):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, i: discord.Interaction):
        bid = self.backup_id_input.value.strip().upper()
        logger.info(f"[Owner] BackupIDModal submitted: backup_id={bid}  |  user={i.user.id}  |  guild={self.guild_id}")
        row = await get_backup_by_id(self.guild_id, bid)
        if not row:
            return await i.response.send_message(
                embed=error_embed(f"Backup ID `{bid}` not found in this server's backup log."),
                ephemeral=True,
            )
        ch_id  = await get_config_or_none(self.guild_id, "owner.backup_channel_id")
        msg_id = row.get("channel_msg_id")
        if not ch_id or not msg_id:
            return await i.response.send_message(
                embed=error_embed(f"No linked message for `{bid}`. Use **📁 Upload File** instead."),
                ephemeral=True,
            )
        channel = i.guild.get_channel(int(ch_id))
        if not channel:
            return await i.response.send_message(
                embed=error_embed("Backup channel not found."), ephemeral=True
            )

        try:
            msg        = await channel.fetch_message(int(msg_id))
            attachment = next((a for a in msg.attachments if a.filename.endswith(".json")), None)
            if not attachment:
                raise ValueError("No JSON attachment on backup message.")
            data = json.loads(await attachment.read())
        except Exception:
            logger.exception(f"[Owner] Failed to fetch backup message for bid={bid}  |  channel={ch_id}  |  msg={msg_id}")
            return await i.response.send_message(
                embed=error_embed(f"Failed to fetch backup. Check logs for details."), ephemeral=True
            )

        valid, warnings = validate_backup_file(data, self.guild_id)
        if not valid:
            return await i.response.send_message(
                embed=error_embed(f"Validation failed: {warnings[0]}"), ephemeral=True
            )

        await i.response.send_message(
            embed=_sync_confirm_embed(data, warnings),
            view=SyncConfirmView(self.guild_id, data),
            ephemeral=True,
        )


class SyncConfirmView(discord.ui.View):
    def __init__(self, guild_id: str, backup_doc: dict):
        super().__init__(timeout=120)
        self.guild_id   = guild_id
        self.backup_doc = backup_doc

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Confirm Restore", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        bid = self.backup_doc["backup_id"]
        logger.warning(
            f"[Owner] ⚠️ Restore CONFIRMED  |  backup_id={bid}  |  "
            f"user={i.user.id}  |  guild={self.guild_id}"
        )

        try:
            pre_id, pre_sync_doc = await restore_backup(self.guild_id, self.backup_doc, str(i.user.id))
        except Exception:
            logger.exception(f"[Owner] Restore FAILED for backup_id={bid}  |  guild={self.guild_id}")
            return await i.edit_original_response(
                embed=error_embed("Restore failed unexpectedly. A pre-sync backup may have been created. Check logs."),
                view=None,
            )

        ch_id = await get_config_or_none(self.guild_id, "owner.backup_channel_id")
        if ch_id:
            channel = i.guild.get_channel(int(ch_id))
            if channel:
                # CVE-2M-017: Send the pre-sync backup file to channel as a real safety net
                try:
                    msg = await send_backup_to_channel(channel, pre_sync_doc)
                    if msg:
                        await set_backup_message_id(pre_sync_doc["backup_id"], str(msg.id))
                except Exception:
                    logger.exception(f"[Owner] Failed to post pre-sync backup {pre_id} to channel {ch_id}.")

                n_keys = len(self.backup_doc["payload"].get("bot_config", []))
                try:
                    await channel.send(
                        f"🔄 SYNC RESTORE\n"
                        f"Backup ID     : `{bid}`\n"
                        f"Initiated by  : {i.user.mention}  │  <t:{int(i.created_at.timestamp())}:f>\n"
                        f"Keys restored : {n_keys}  │  Checksum: ✅\n"
                        f"Pre-sync save : `{pre_id}`"
                    )
                except discord.HTTPException:
                    logger.warning(f"[Owner] Could not send restore audit message to channel {ch_id}.")

        await i.edit_original_response(
            embed=success_embed(
                f"Restore complete!\n"
                f"Config replaced with `{bid}`.\n"
                f"Pre-sync backup saved as `{pre_id}`."
            ),
            view=None,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_sync_panel(i)
        await i.edit_original_response(embed=e, view=v)


class OwnerSyncView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📋 Enter Backup ID",    style=discord.ButtonStyle.secondary)
    async def enter_id_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(BackupIDModal(self.guild_id))

    @discord.ui.button(label="📁 Upload Backup File", style=discord.ButtonStyle.secondary)
    async def upload_btn(self, i: discord.Interaction, _: discord.ui.Button):
        gid = self.guild_id
        await i.response.edit_message(
            embed=discord.Embed(
                title="📁  Upload Backup File",
                description=(
                    "Send your backup `.json` file as a message **in this channel** "
                    "within **60 seconds**.\n\n"
                    "The message will be deleted automatically after processing."
                ),
                color=PRIMARY,
            ),
            view=None,
        )

        def _check(m: discord.Message) -> bool:
            return m.author.id == i.user.id and m.channel.id == i.channel_id and len(m.attachments) > 0

        try:
            msg = await i.client.wait_for("message", check=_check, timeout=60.0)
        except asyncio.TimeoutError:
            logger.debug(f"[Owner] File upload timed out for user {i.user.id} in guild {gid}.")
            e, v = await _build_sync_panel(i)
            return await i.edit_original_response(embed=e, view=v)

        try:
            # CVE-2M-022: Validate file extension and size before reading
            attachment = next(
                (a for a in msg.attachments if a.filename.endswith(".json") and a.size < 10_000_000),
                None
            )
            if not attachment:
                raise ValueError("No valid .json attachment under 10MB found.")
            data = json.loads(await attachment.read())
        except Exception:
            logger.exception(f"[Owner] Failed to parse uploaded backup from user {i.user.id} in guild {gid}.")
            try:
                await msg.delete()
            except Exception:
                pass
            return await i.edit_original_response(
                embed=error_embed("Could not read the file. Ensure it is a valid 2M backup JSON."),
                view=OwnerSyncView(gid),
            )

        try:
            await msg.delete()
        except discord.HTTPException:
            logger.warning(f"[Owner] Could not delete upload message {msg.id} from user {i.user.id}.")

        valid, warnings = validate_backup_file(data, str(i.guild_id))
        if not valid:
            return await i.edit_original_response(
                embed=error_embed(f"Backup invalid: {warnings[0]}"),
                view=OwnerSyncView(gid),
            )

        await i.edit_original_response(
            embed=_sync_confirm_embed(data, warnings),
            view=SyncConfirmView(gid, data),
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# ADMIN IDENTITY VIEWS
# -----
class _AdminRoleSelect(discord.ui.RoleSelect):
    # -----
    # CHANGE 3: Multi-role admin/mod configuration.
    # Uses Discord's native RoleSelect (searchable by @role mention) instead of
    # a dropdown of pre-listed IDs. Supports up to 25 roles simultaneously.
    # min_values=0 so the user can open and close the panel without being
    # forced to re-select. A new selection replaces the stored list.
    # -----
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="🛡️ Set Admin Roles… (select to replace list)",
            row=0,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(r.id) for r in self.values]
        await set_config(self.guild_id, "system.admin_role_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Admin roles set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _AdminUserSelect(discord.ui.UserSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="👤 Set Admin Users… (select to replace list)",
            row=1,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(u.id) for u in self.values]
        await set_config(self.guild_id, "system.admin_user_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Admin users set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ModRoleSelect(discord.ui.RoleSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="⚔️ Set Mod Roles… (select to replace list)",
            row=2,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(r.id) for r in self.values]
        await set_config(self.guild_id, "system.mod_role_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Mod roles set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class _ModUserSelect(discord.ui.UserSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="👤 Set Mod Users… (select to replace list)",
            row=3,
            min_values=1,
            max_values=25,
        )
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        ids = [str(u.id) for u in self.values]
        await set_config(self.guild_id, "system.mod_user_id", json.dumps(ids), str(i.user.id))
        logger.info(f"[Owner] Mod users set to {ids} by user {i.user.id}.")
        e, v = await _build_admin_set_panel(i)
        await i.response.edit_message(embed=e, view=v)


class OwnerAdminSetView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(_AdminRoleSelect(guild_id))
        self.add_item(_AdminUserSelect(guild_id))
        self.add_item(_ModRoleSelect(guild_id))
        self.add_item(_ModUserSelect(guild_id))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# CLUSTER MIGRATION VIEWS
# -----
class MigrationTargetSelect(discord.ui.Select):
    def __init__(self, instances, action: str):
        self.action = action
        opts = []
        for inst in instances:
            iid  = inst["instance_id"]
            role = inst["role"]
            hb   = inst["last_heartbeat"]
            ts   = f"<t:{int(hb.timestamp())}:R>" if hb else "never"
            opts.append(discord.SelectOption(
                label=f"{iid} ({role})",
                description=f"Host: {inst['hostname'] or '?'} — last seen {ts[:50]}",
                value=iid,
            ))
        if not opts:
            opts = [discord.SelectOption(label="No instances available", value="none")]
        super().__init__(
            placeholder=f"Select instance to {action}...",
            options=opts,
        )

    async def callback(self, i: discord.Interaction):
        target = self.values[0]
        if target == "none":
            return await i.response.send_message("No instance selected.", ephemeral=True)

        pool = await get_pool()
        gid  = str(i.guild_id)

        if self.action == "transfer":
            async with pool.acquire() as conn:
                target_row = await conn.fetchrow("SELECT role FROM bot_instances WHERE instance_id=$1", target)
                if not target_row:
                    return await i.response.send_message(embed=error_embed("Instance not found."), ephemeral=True)
                if target_row["role"] == "LEADER":
                    return await i.response.send_message(embed=error_embed("That instance is already the LEADER."), ephemeral=True)

                await conn.execute("UPDATE bot_instances SET role='STANDBY' WHERE guild_id=$1 AND role='LEADER'", gid)
                await conn.execute("UPDATE bot_instances SET role='LEADER' WHERE instance_id=$1", target)

            logger.info(f"[Owner] Leadership transferred to instance {target} by user {i.user.id} in guild {gid}.")
            e, v = await _build_migration_panel(i)
            e.add_field(name="✅ Transfer Complete", value=f"Instance `{target}` is now LEADER.\nThe old leader will shut down within ~20 seconds.", inline=False)
            await i.response.edit_message(embed=e, view=v)

        elif self.action == "shutdown":
            async with pool.acquire() as conn:
                await conn.execute("UPDATE bot_instances SET force_shutdown=TRUE WHERE instance_id=$1", target)
            logger.info(f"[Owner] Force shutdown signal sent to instance {target} by user {i.user.id} in guild {gid}.")
            e, v = await _build_migration_panel(i)
            e.add_field(name="⚠️ Shutdown Signal Sent", value=f"Instance `{target}` will shut down within ~15 seconds.", inline=False)
            await i.response.edit_message(embed=e, view=v)


class MigrationActionView(discord.ui.View):
    def __init__(self, gid: str, instances: list, action: str):
        super().__init__(timeout=120)
        self.gid = gid
        self.add_item(MigrationTargetSelect(instances, action))

        btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
        async def go_back(inter):
            await inter.response.defer()
            e, v = await _build_migration_panel(inter)
            await inter.edit_original_response(embed=e, view=v)
        btn.callback = go_back
        self.add_item(btn)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class OwnerMigrationView(discord.ui.View):
    # -----
    # CHANGE 2: Full Migration Export (Cross-DB / Cross-Provider)
    # Added "🚀 Full Migration Export" button that creates a full backup
    # (config + all user data) and sends it as a downloadable file.
    # Flow: Worker A exports → upload JSON to Worker B → /owner > Sync Backup
    # This replaces the old static "how it works" text with a real action.
    # -----
    def __init__(self, guild_id: str, instances: list):
        super().__init__(timeout=300)
        self.guild_id  = guild_id
        self.instances = instances

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="👑 Transfer Leadership", style=discord.ButtonStyle.primary, row=0)
    async def transfer_btn(self, i: discord.Interaction, _: discord.ui.Button):
        now = datetime.now(timezone.utc)
        # Only show instances that are actually alive (active threshold)
        standby = [
            inst for inst in self.instances
            if inst["role"] == "STANDBY"
            and inst["last_heartbeat"]
            and (now - inst["last_heartbeat"].replace(tzinfo=timezone.utc)
                 if inst["last_heartbeat"].tzinfo is None
                 else now - inst["last_heartbeat"]).total_seconds() < _INSTANCE_ACTIVE_THRESHOLD_S
        ]
        if not standby:
            return await i.response.send_message(
                embed=error_embed("No active STANDBY instance found.\nStart a new worker pointing to the **same database**, then refresh."),
                ephemeral=True,
            )
        e = discord.Embed(title="👑  SELECT NEW LEADER", description="Choose the active STANDBY instance to promote.", color=0xFEE75C)
        await i.response.edit_message(embed=e, view=MigrationActionView(self.guild_id, standby, "transfer"))

    @discord.ui.button(label="🛑 Force Shutdown", style=discord.ButtonStyle.danger, row=0)
    async def shutdown_btn(self, i: discord.Interaction, _: discord.ui.Button):
        if not self.instances:
            return await i.response.send_message(embed=error_embed("No instances to shut down."), ephemeral=True)
        e = discord.Embed(title="🛑  FORCE SHUTDOWN", description="Choose the instance to force-shutdown.", color=0xED4245)
        await i.response.edit_message(embed=e, view=MigrationActionView(self.guild_id, self.instances, "shutdown"))

    @discord.ui.button(label="🚀 Full Migration Export", style=discord.ButtonStyle.success, row=1)
    async def full_export_btn(self, i: discord.Interaction, _: discord.ui.Button):
        # -----
        # CHANGE 2: Full migration export for cross-provider moves.
        # Creates a full backup (config + economy data), sends the .json file
        # directly as an ephemeral attachment so the owner can download it.
        # Instructions are embedded so the process is self-contained.
        # -----
        await i.response.defer(ephemeral=True)
        gid = self.guild_id
        logger.info(f"[Owner] Full migration export initiated by user {i.user.id} for guild {gid}.")
        try:
            backup_doc = await create_backup(
                gid,
                initiated_by=str(i.user.id),
                backup_type="migration_export",
                include_user_data=True,   # Full export: config + all economy data
            )
        except Exception:
            logger.exception(f"[Owner] Full migration export failed for guild {gid}.")
            return await i.followup.send(
                embed=error_embed("Export failed. Check logs for details."),
                ephemeral=True,
            )

        bid = backup_doc["backup_id"]
        n_cfg   = len(backup_doc["payload"].get("bot_config", []))
        n_users = len(backup_doc["payload"].get("users", []))
        n_inv   = len(backup_doc["payload"].get("user_inventory", []))

        file_bytes = json.dumps(backup_doc, indent=2, default=str).encode()
        file_obj   = discord.File(io.BytesIO(file_bytes), filename=f"{bid}.json")

        e = discord.Embed(title="🚀  FULL MIGRATION EXPORT", color=0x57F287)
        e.add_field(
            name="📦 What's included",
            value=(
                f"**Config keys:** {n_cfg}\n"
                f"**Users:** {n_users}\n"
                f"**Inventory records:** {n_inv}\n"
                f"**Backup ID:** `{bid}`"
            ),
            inline=False,
        )
        e.add_field(
            name="📋 Next steps — moving to a new provider",
            value=(
                "1. Download the `.json` file attached below.\n"
                "2. Deploy your new bot (Worker B) with the new `DATABASE_URL`.\n"
                "3. On Worker B: run `/owner` → **🔄 Sync Backup** → **📁 Upload Backup File**.\n"
                "4. Upload this file. Worker B will restore all config and economy data.\n"
                "5. Once verified, shut down Worker A via **🛑 Force Shutdown**.\n\n"
                "⚠️ This file contains all user point balances. Keep it secure."
            ),
            inline=False,
        )
        logger.info(f"[Owner] Full migration export {bid} sent to user {i.user.id}  |  cfg={n_cfg}  users={n_users}.")
        await i.followup.send(embed=e, file=file_obj, ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_migration_panel(i)
        await i.edit_original_response(embed=e, view=v)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer()
        e, v = await _build_main_panel(i)
        await i.edit_original_response(embed=e, view=v)


# -----
# DISCORD COG MOUNTING
# -----
class OwnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="owner", description="Owner-only control panel.")
    async def owner_cmd(self, i: discord.Interaction):
        # BUG FIX: Defer BEFORE any DB queries to avoid 3-second interaction timeout.
        if not await is_server_owner(i):
            await i.response.send_message("This command is restricted to the server owner.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        e, v = await _build_main_panel(i)
        await i.followup.send(embed=e, view=v, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerCog(bot))