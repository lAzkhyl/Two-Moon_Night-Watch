# =====
# MODULE: guards/checks.py
# =====
# Architecture Overview:
# This file centralizes permission checks across the discord.py application.
# It prevents unauthorized users from executing sensitive administrative or
# moderation slash commands.
#
# Developer Note:
# By keeping authorization logic isolated here, we maintain clean code in our
# cogs. When a user fails a check, these functions automatically respond with
# an ephemeral error message, completely stopping the command execution.
#
# CVE-2M-021: All queries now use the cached get_config_or_none() instead of
# raw DB queries, reducing connection pool pressure.
# =====
import discord
import json
from config.manager import get_config_or_none

async def is_server_owner(interaction: discord.Interaction) -> bool:
    # -----
    # Verifies if the user executing the command is the absolute creator and
    # owner of the Discord Server (Guild).
    #
    # Note: Ownership is determined by Discord's API directly. This provides
    # a hardcoded fallback that overrides local database permissions.
    # -----
    if not interaction.guild:
        return False
    return interaction.user.id == interaction.guild.owner_id


async def require_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a Server.", ephemeral=True)
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
        
    gid = str(interaction.guild_id)
    uid = str(interaction.user.id)
    roles = [str(r.id) for r in getattr(interaction.user, "roles", [])]

    # Check Roles (JSON List)
    admin_role_ids_raw = await get_config_or_none(gid, "system.admin_role_id")
    if admin_role_ids_raw:
        try:
            admin_role_ids = json.loads(admin_role_ids_raw)
            if isinstance(admin_role_ids, list) and any(r in roles for r in admin_role_ids):
                return True
        except: pass

    # Check Users (JSON List)
    admin_user_ids_raw = await get_config_or_none(gid, "system.admin_user_id")
    if admin_user_ids_raw:
        try:
            admin_user_ids = json.loads(admin_user_ids_raw)
            if isinstance(admin_user_ids, list) and uid in admin_user_ids:
                return True
        except: pass
        
    await interaction.response.send_message(
        "Access denied. You need Administrator permissions.",
        ephemeral=True,
    )
    return False


async def require_mod(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a Server.", ephemeral=True)
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
        
    gid = str(interaction.guild_id)
    uid = str(interaction.user.id)
    roles = [str(r.id) for r in getattr(interaction.user, "roles", [])]

    # Check Admin First (Hierarchy)
    admin_role_ids_raw = await get_config_or_none(gid, "system.admin_role_id")
    if admin_role_ids_raw:
        try:
            admin_role_ids = json.loads(admin_role_ids_raw)
            if isinstance(admin_role_ids, list) and any(r in roles for r in admin_role_ids):
                return True
        except: pass

    admin_user_ids_raw = await get_config_or_none(gid, "system.admin_user_id")
    if admin_user_ids_raw:
        try:
            admin_user_ids = json.loads(admin_user_ids_raw)
            if isinstance(admin_user_ids, list) and uid in admin_user_ids:
                return True
        except: pass

    # Check Mod Roles (JSON List)
    mod_role_ids_raw = await get_config_or_none(gid, "system.mod_role_id")
    if mod_role_ids_raw:
        try:
            mod_role_ids = json.loads(mod_role_ids_raw)
            if isinstance(mod_role_ids, list) and any(r in roles for r in mod_role_ids):
                return True
        except: pass

    # Check Mod Users (JSON List)
    mod_user_ids_raw = await get_config_or_none(gid, "system.mod_user_id")
    if mod_user_ids_raw:
        try:
            mod_user_ids = json.loads(mod_user_ids_raw)
            if isinstance(mod_user_ids, list) and uid in mod_user_ids:
                return True
        except: pass
        
    await interaction.response.send_message(
        "Access denied. You need Moderator permissions (or higher).",
        ephemeral=True,
    )
    return False


async def require_active(interaction: discord.Interaction) -> bool:
    # -----
    # Acts as a master service switch. It intercepts normal public commands 
    # (/stats, /shop, /gamenight) if the system is halted or incomplete.
    #
    # States:
    # - ACTIVE: Commands process normally.
    # - PAUSED: Commands are temporarily blocked (typically for maintenance).
    # - UNCONFIGURED: Commands are blocked until the setup wizard completes.
    # -----
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a Server.", ephemeral=True)
        return False
        
    # CVE-2M-021: Use cached config instead of raw DB query
    state = await get_config_or_none(str(interaction.guild_id), "system.state")
        
    messages = {
        "UNCONFIGURED": "System is unconfigured. An administrator must run `/admin` to setup the bot.",
        "PAUSED":       "System is temporarily paused by an administrator. Please wait.",
    }
    
    if state != "ACTIVE":
        await interaction.response.send_message(
            messages.get(state, "System services are currently unavailable."),
            ephemeral=True,
        )
        return False
        
    return True