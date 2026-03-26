# =====
# MODULE: cogs/admin.py
# =====
# Architecture Overview:
# Administrator Control Panel — Entity-First Dropdown Navigation.
#
# UI ARCHITECTURE (v5):
#   /admin opens AdminRootView containing ONE discord.ui.Select (AdminNavSelect).
#   Selecting a category navigates to that panel view (no page reload, in-place edit).
#   Every panel renders two stacked embeds:
#     Embed 1 — State card:  current live config values for this category.
#     Embed 2 — Guide card:  concise description of each control in the panel.
#
# CMS INTEGRATION:
#   All embed titles, colors, and thumbnails are stored in bot_config under
#   keys like `embed.admin.<key>` (seeded by migration 006).
#   _build_cms_embed() is the single DRY helper that loads CMS data and
#   constructs the embed — no copy-pasted fallback blocks anywhere.
#
# CATEGORY → VIEW MAP:
#   ⚙️ System & Channels  → AdminSystemView
#   👥 Point               → AdminPointView   (Economy + Decay unified)
#   🎮 Host & Events       → AdminHostView
#   🗳️ Vote & Reputation   → AdminVoteView
#   🛒 Shop Management     → AdminShopView
#
# CHANGELOG:
#   FIX-ADM-001  Race condition in Setup Wizard — SELECT FOR UPDATE per guild.
#   FIX-ADM-002  Draft delete not atomic — wrapped in same tx as bulk_set_config.
#   FIX-ADM-003  AdminNumberModal error is ephemeral — panel stays alive.
#   FIX-ADM-004  State changes require ConfirmSystemStateView before execution.
#   FIX-ADM-005  AdminChannelSel.callback() defers before DB write.
#   FIX-ADM-006  set_active/set_paused defer before edit.
#   FIX-ADM-007  All Views store message ref and call edit on timeout.
#   FIX-ADM-008  ForcePointsModal ID parsing uses re.sub, not lstrip.
#   FIX-ADM-009  Decay label corrected.
#   FIX-ADM-010  _format_role_ids deduplicated — single shared implementation.
#   FIX-ADM-011  Host / Vote / Decay views added.
#   FIX-ADM-012  require_mod guard uses explicit defer.
#   FIX-ADM-013  Setup Wizard preset descriptions added.
#   FIX-ADM-014  AdminNumberModal shows current value in placeholder.
#   REFACTOR-ADM-015  Dual-embed layout, small-caps titles, colour palette.
#   REFACTOR-ADM-016  Full Entity-First Dropdown architecture.
#                     Economy + Decay consolidated into 👥 Point.
#                     Shop Management panel implemented.
#                     _build_cms_embed() DRY helper introduced.
#                     _TimeoutView base class eliminates on_timeout boilerplate.
# =====

import io
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from config.defaults import PRESETS
from config.manager import (
    bulk_set_config,
    get_all_config,
    get_config_or_none,
    set_config,
)
from db.pool import get_pool
from economy.points import award, deduct
from guards.checks import require_admin, require_mod
from utils.embeds import ERROR, PRIMARY, WARNING, confirm_embed, error_embed, success_embed
from utils.emojis import ALERT_PAUSED, TICK_ACTIVE, THUMBNAIL_ADMIN, THUMBNAIL_NAV
from utils.time import format_relative

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Small-cap Unicode titles — used as fallbacks when CMS key is missing.
_T_ROOT    = "ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ"
_T_NAV     = "ɴᴀᴠɪɢᴀᴛɪᴏɴ ɢᴜɪᴅᴇ"
_T_SYSTEM  = "⚙️ ꜱʏꜱᴛᴇᴍ & ᴄʜᴀɴɴᴇʟꜱ"
_T_SY_G    = "⚙️ ꜱʏꜱᴛᴇᴍ ɢᴜɪᴅᴇ"
_T_POINT   = "👥 ᴘᴏɪɴᴛ"
_T_PT_G    = "👥 ᴘᴏɪɴᴛ ɢᴜɪᴅᴇ"
_T_HOST    = "🎮 ʜᴏꜱᴛ & ᴇᴠᴇɴᴛꜱ"
_T_HO_G    = "🎮 ʜᴏꜱᴛ ɢᴜɪᴅᴇ"
_T_VOTE    = "🗳️ ᴠᴏᴛᴇ & ʀᴇᴘᴜᴛᴀᴛɪᴏɴ"
_T_VO_G    = "🗳️ ᴠᴏᴛᴇ ɢᴜɪᴅᴇ"
_T_SHOP    = "🛒 ꜱʜᴏᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ"
_T_SH_G    = "🛒 ꜱʜᴏᴘ ɢᴜɪᴅᴇ"
_T_ITEM    = "📦 ɪᴛᴇᴍ ᴄᴏɴᴛʀᴏʟ"

_C_RED   = 0xFF0000
_C_WHITE = 0xFFFFFF


# =============================================================================
# BASE VIEW — eliminates on_timeout boilerplate in every sub-view
# =============================================================================

class _TimeoutView(discord.ui.View):
    """Disables all children and updates the message when the view times out."""

    def __init__(self, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# =============================================================================
# SHARED UTILITY FUNCTIONS
# =============================================================================

def _format_role_ids(guild: discord.Guild, raw_val: str | None) -> str:
    """
    Parses both legacy single-ID strings and modern JSON arrays into role mentions.
    FIX-ADM-010: Single shared implementation.
    """
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


def _val(raw: str | None, suffix: str = "") -> str:
    """Formats a raw config value for inline display; *(not set)* when absent."""
    if not raw or raw in ("null", "[]", ""):
        return "*(not set)*"
    return f"`{raw}{suffix}`"


def _ch(raw: str | None) -> str:
    """Formats a channel/category ID as a Discord mention."""
    return f"<#{raw}>" if raw else "*(not set)*"


async def _build_cms_embed(
    guild_id: str,
    cms_key: str,
    fallback_title: str,
    fallback_color: int,
    description: str | None = None,
    fallback_desc: str = "",
) -> discord.Embed:
    """
    Single DRY helper for constructing every admin panel embed.

    Loads title, color, and thumbnail from the CMS database record at
    `embed.admin.<cms_key>`. The description argument controls two modes:

      description=<str>  → State embed: uses caller-provided live content.
                           DB description field is ignored (it's a template).
      description=None   → Guide embed: uses DB description or fallback_desc.

    This function is the ONLY place embed fallback logic exists in this module.
    """
    title  = fallback_title
    color  = fallback_color
    thumb  = None
    db_desc = fallback_desc

    raw = await get_config_or_none(guild_id, f"embed.admin.{cms_key}")
    if raw:
        try:
            cfg    = json.loads(raw)
            title  = cfg.get("title",     fallback_title)
            color  = int(cfg.get("color", fallback_color))
            thumb  = cfg.get("thumbnail") or None
            db_desc = cfg.get("description", fallback_desc)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    final_desc = description if description is not None else db_desc
    e = discord.Embed(title=title, description=final_desc, color=color)
    if thumb:
        e.set_thumbnail(url=thumb)
    return e


async def _open_number_modal(
    i: discord.Interaction,
    key: str,
    title: str,
    placeholder: str,
) -> None:
    """Fetches current value then opens the modal with an informative placeholder."""
    current = await get_config_or_none(str(i.guild_id), key)
    await i.response.send_modal(AdminNumberModal(key, title, placeholder, current_value=current))


# =============================================================================
# PANEL EMBED BUILDERS
# Each async function returns [state_embed, guide_embed].
# =============================================================================

async def _root_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Main panel — system status card + navigation guide."""
    gid    = str(i.guild_id)
    state  = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")

    pool = await get_pool()
    async with pool.acquire() as conn:
        last_audit = await conn.fetchrow(
            """
            SELECT changed_by, config_key, new_value, changed_at
            FROM config_audit_log WHERE guild_id=$1
            ORDER BY changed_at DESC LIMIT 1
            """,
            gid,
        )

    audit_line = ""
    if last_audit:
        relative = format_relative(last_audit["changed_at"])
        new_val  = str(last_audit["new_value"] or "—")[:37]
        audit_line = (
            f"\n-# Edited {relative} by <@{last_audit['changed_by']}>\n"
            f"> -# {last_audit['config_key']} → {new_val}"
        )

    if state == "ACTIVE":
        state_str = f"{TICK_ACTIVE} **ACTIVE**"
    elif state == "PAUSED":
        state_str = f"{ALERT_PAUSED} **PAUSED**"
    else:
        state_str = "🔴 **UNCONFIGURED**"

    desc_state = (
        f"**{state_str}**\n"
        f"### Authorization:\n"
        f"**Admin:**\n> {_format_role_ids(i.guild, ar_raw)}\n\n"
        f"**Mod:**\n> {_format_role_ids(i.guild, mr_raw)}"
        f"{audit_line}"
    )
    desc_guide = (
        "⚙️ **System & Channels** — Bot state, point name, channel bindings, force actions.\n\n"
        "👥 **Point** — Event economy (join/end bonus, event math) and decay zones.\n\n"
        "🎮 **Host & Events** — Hosting rules: cooldown, duration, income multiplier.\n\n"
        "🗳️ **Vote & Reputation** — Post-event voting window and score weights.\n\n"
        "🛒 **Shop Management** — Add and manage purchasable shop items."
    )

    e1 = await _build_cms_embed(gid, "main",       _T_ROOT, _C_RED,   description=desc_state)
    e2 = await _build_cms_embed(gid, "main_guide",  _T_NAV,  _C_WHITE, fallback_desc=desc_guide)
    e1.set_thumbnail(url=THUMBNAIL_ADMIN)
    e2.set_thumbnail(url=THUMBNAIL_NAV)
    return [e1, e2]


async def _system_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    gid      = str(i.guild_id)
    state    = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"
    pt_name  = await get_config_or_none(gid, "system.point_name") or "points"
    ar_raw   = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw   = await get_config_or_none(gid, "system.mod_role_id")
    gn_raw   = await get_config_or_none(gid, "channel.gamenight_id")
    act_raw  = await get_config_or_none(gid, "channel.activity_id")
    vc_raw   = await get_config_or_none(gid, "channel.vc_category_id")

    state_str = (
        f"{TICK_ACTIVE} **ACTIVE**"  if state == "ACTIVE"  else
        f"{ALERT_PAUSED} **PAUSED**" if state == "PAUSED"  else
        "🔴 **UNCONFIGURED**"
    )
    desc = (
        f"**State:** {state_str}\n"
        f"**Point Name:** `{pt_name}`\n\n"
        f"**Admin Role:** {_format_role_ids(i.guild, ar_raw)}\n"
        f"**Mod Role:** {_format_role_ids(i.guild, mr_raw)}\n\n"
        f"**Gamenight:** {_ch(gn_raw)}\n"
        f"**Activity:**  {_ch(act_raw)}\n"
        f"**VC Category:** {_ch(vc_raw)}"
    )
    guide = (
        "**▶️ / ⏸️ Bot State** — Toggle ACTIVE / PAUSED with a confirmation gate.\n"
        "**🏷️ Point Name** — Rename the in-server currency (e.g. coins, gems).\n"
        "**📡 Channels** — Bind gamenight text channel, activity feed, and VC category.\n"
        "**⚡ Force Action** — Direct balance manipulation. Mod+ only. Fully audited.\n"
        "**👁️ View All** — Export full bot_config as a downloadable .txt file."
    )

    e1 = await _build_cms_embed(gid, "system",       _T_SYSTEM, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "system_guide",  _T_SY_G,  _C_WHITE, fallback_desc=guide)
    return [e1, e2]


async def _point_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Unified Point panel — Economy settings + Decay zones in one view."""
    gid        = str(i.guild_id)
    join_b     = await get_config_or_none(gid, "ec.join_bonus")
    comp_b     = await get_config_or_none(gid, "ec.completion_bonus")
    max_b      = await get_config_or_none(gid, "ec.base_max_bonus")
    mins_tick  = await get_config_or_none(gid, "ec.mins_per_tick")
    pts_tick   = await get_config_or_none(gid, "ec.pts_per_tick")
    grace      = await get_config_or_none(gid, "decay.grace_days")
    zones_raw  = await get_config_or_none(gid, "decay.zones_config")

    # Format zones for display
    zones_display = "*(not configured)*"
    if zones_raw:
        try:
            zones = json.loads(zones_raw)
            if isinstance(zones, list) and zones:
                lines = []
                for z in sorted(zones, key=lambda x: x.get("zone_id", 0)):
                    dur   = int(z.get("duration_days", -1))
                    rate  = float(z.get("rate_per_day", 0))
                    label = z.get("label", f"Zone {z.get('zone_id','?')}")
                    dur_s = f"{dur}d" if dur != -1 else "∞"
                    lines.append(f"> `{label}` — {dur_s} · {rate} pts/day")
                zones_display = "\n".join(lines)
        except (json.JSONDecodeError, TypeError):
            pass

    desc = (
        "**── Event Economy ──**\n"
        f"**Join Bonus**       — {_val(join_b,    ' pts')}\n"
        f"**Event End Bonus**  — {_val(comp_b,    ' pts')}\n"
        f"**Max Bonus Cap**    — {_val(max_b,     ' pts')}\n"
        f"**Mins / Tick**      — {_val(mins_tick, ' min')}\n"
        f"**Pts / Tick**       — {_val(pts_tick,  ' pts')}\n\n"
        "**── Decay ──**\n"
        f"**Grace Period** — {_val(grace, ' days')}\n"
        f"**Zones:**\n{zones_display}"
    )
    guide = (
        "**Join Bonus** — Points awarded the instant a user joins an event.\n"
        "**Event End Bonus** — Bonus distributed when the host ends the event normally.\n"
        "**Max Bonus Cap** — Hard ceiling on total per-event earnings.\n"
        "**Event Math** — Set Mins/Tick, Pts/Tick, and Max Cap together in one modal.\n\n"
        "**Grace Period** — Days of inactivity before any decay starts.\n"
        "**Add Zone** — Append a new decay tier (label, duration, rate/day).\n"
        "**Manage Zones** — Edit or delete existing decay zones via dropdown.\n\n"
        "> Zone with `duration_days = -1` is terminal — it runs indefinitely.\n"
        "> Any user activity resets the decay clock back to the grace period."
    )

    e1 = await _build_cms_embed(gid, "point",       _T_POINT, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "point_guide",  _T_PT_G,  _C_WHITE, fallback_desc=guide)
    return [e1, e2]


async def _host_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    gid         = str(i.guild_id)
    cooldown    = await get_config_or_none(gid, "host.cooldown_hours")
    min_dur     = await get_config_or_none(gid, "host.min_duration_minutes")
    auto_end    = await get_config_or_none(gid, "host.auto_end_tolerance_minutes")
    income_mult = await get_config_or_none(gid, "host.income_multiplier")
    rolling     = await get_config_or_none(gid, "host.rolling_window")

    desc = (
        f"**Cooldown**          — {_val(cooldown,    ' hours')}\n"
        f"**Min Duration**      — {_val(min_dur,     ' min')}\n"
        f"**Auto-End Tolerance** — {_val(auto_end,   ' min')}\n"
        f"**Income Multiplier** — {_val(income_mult, '×')}\n"
        f"**Rolling Window**    — {_val(rolling,     ' events')}"
    )
    guide = (
        "**Cooldown** — Hours a host must wait before they can open another event.\n"
        "**Min Duration** — Events shorter than this threshold earn no host rewards.\n"
        "**Auto-End VC Tolerance** — Minutes of empty VC before the event auto-closes.\n"
        "**Income Multiplier** — Host earns this multiple of the standard participant reward.\n"
        "**Rolling Window** — Number of recent events averaged to compute reputation score."
    )

    e1 = await _build_cms_embed(gid, "host",       _T_HOST, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "host_guide",  _T_HO_G, _C_WHITE, fallback_desc=guide)
    return [e1, e2]


async def _vote_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    gid        = str(i.guild_id)
    min_voters = await get_config_or_none(gid, "host.min_voters")
    outlier    = await get_config_or_none(gid, "host.outlier_trim_threshold")
    window     = await get_config_or_none(gid, "vote.window_minutes")
    pos        = await get_config_or_none(gid, "vote.score_positive")
    neu        = await get_config_or_none(gid, "vote.score_neutral")
    neg        = await get_config_or_none(gid, "vote.score_negative")

    desc = (
        f"**Min Voters**     — {_val(min_voters)}\n"
        f"**Outlier Trim**   — {_val(outlier)}\n"
        f"**Vote Window**    — {_val(window,  ' min')}\n\n"
        f"**Score Positive** — {_val(pos, ' pts')}\n"
        f"**Score Neutral**  — {_val(neu, ' pts')}\n"
        f"**Score Negative** — {_val(neg, ' pts')}"
    )
    guide = (
        "**Min Voters** — Minimum vote submissions required to update host reputation.\n"
        "**Outlier Trim** — Votes beyond this count per event are discarded (anti-brigade).\n"
        "**Vote Window** — Minutes the poll stays open after an event closes.\n"
        "**Edit Scores** — Set all three vote weights (positive/neutral/negative) at once.\n\n"
        "> Scores feed into the host's rolling reputation average.\n"
        "> Lower scores reduce priority and income multiplier — they don't lock a host out."
    )

    e1 = await _build_cms_embed(gid, "vote",       _T_VOTE, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "vote_guide",  _T_VO_G, _C_WHITE, fallback_desc=guide)
    return [e1, e2]


async def _shop_panel_embeds(i: discord.Interaction, item_count: int) -> list[discord.Embed]:
    gid = str(i.guild_id)
    desc = (
        f"**Active Items:** `{item_count}`\n\n"
        "Use **➕ Add New Item** to create a shop listing.\n"
        "Use **📦 Manage Item** to edit or remove an existing item."
    )
    guide = (
        "**➕ Add New Item** — Opens a modal to create a new item (name, description, cost, type).\n"
        "**📦 Manage Item** — Dropdown of existing items; select one to open its control panel.\n"
        "**🌌 Black Market** — Coming soon. Will configure BM rotation and slot count.\n\n"
        "> Item types: `consumable`, `role`, `rental`, `permanent`\n"
        "> `role` items automatically assign a Discord role on purchase.\n"
        "> `rental` items expire after a configurable number of days."
    )

    e1 = await _build_cms_embed(gid, "shop",       _T_SHOP, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "shop_guide",  _T_SH_G, _C_WHITE, fallback_desc=guide)
    return [e1, e2]


async def _item_control_embeds(i: discord.Interaction, item: dict) -> list[discord.Embed]:
    gid = str(i.guild_id)
    bm_str  = "✅ Yes" if item.get("is_blackmarket") else "❌ No"
    act_str = "✅ Active" if item.get("is_active", True) else "🔴 Inactive"
    dur_str = f"`{item['duration_days']}d`" if item.get("duration_days") else "*(N/A)*"

    desc = (
        f"**Item:** `{item['label']}`\n"
        f"**Price:** `{item['cost']} pts`\n"
        f"**Type:** `{item['item_type']}`\n"
        f"**Duration:** {dur_str}\n"
        f"**Black Market:** {bm_str}\n"
        f"**Status:** {act_str}\n\n"
        f"**Description:**\n{item.get('description') or '*(none)*'}"
    )
    guide = (
        "**✏️ Edit Details** — Update name, description, price, and type.\n"
        "**🎚️ Set Rarity** — Assign a rarity tier label to this item.\n"
        "**🛒 Toggle Black Market** — Mark/unmark this item for BM rotation.\n"
        "**🗑️ Delete Item** — Permanently remove this item from the shop.\n\n"
        "> Deletion is irreversible — existing inventory entries are not affected."
    )

    e1 = await _build_cms_embed(gid, "item_control", _T_ITEM, PRIMARY, description=desc)
    e2 = await _build_cms_embed(gid, "shop_guide",    _T_SH_G, _C_WHITE, fallback_desc=guide)
    return [e1, e2]


# =============================================================================
# MODALS
# =============================================================================

class AdminNumberModal(discord.ui.Modal):
    """Generic single-value numeric/text config modal. FIX-ADM-003 / FIX-ADM-014."""
    value_input = discord.ui.TextInput(label="New Value", placeholder="Enter a value...")

    def __init__(self, key: str, title: str, placeholder: str, current_value: str | None = None):
        super().__init__(title=title[:45])
        self.cfg_key = key
        self.value_input.placeholder = (
            f"Current: {current_value}  ·  {placeholder}" if current_value is not None else placeholder
        )

    async def on_submit(self, i: discord.Interaction) -> None:
        new_val = self.value_input.value.strip()
        try:
            await set_config(str(i.guild_id), self.cfg_key, new_val, str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"`{self.cfg_key}` updated to `{new_val}`."), ephemeral=True
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"), ephemeral=True
            )


class EventMathModal(discord.ui.Modal, title="Event Math Settings"):
    """Captures Mins/Tick, Pts/Tick, and Max Cap in a single modal submit."""
    mins_tick = discord.ui.TextInput(label="Mins / Tick",  placeholder="Minutes per earning tick, e.g. 10")
    pts_tick  = discord.ui.TextInput(label="Pts / Tick",   placeholder="Points awarded per tick, e.g. 5")
    max_cap   = discord.ui.TextInput(label="Max Cap",      placeholder="Max earnable pts per event, e.g. 50")

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        uid = str(i.user.id)
        errors: list[str] = []

        async def _save(key: str, raw: str) -> None:
            val = raw.strip()
            if not val:
                return
            try:
                float(val)  # Validate numeric
                await set_config(gid, key, val, uid)
            except ValueError:
                errors.append(f"`{key}` — `{val}` is not a valid number.")
            except Exception as exc:
                errors.append(f"`{key}` — {exc}")

        await _save("ec.mins_per_tick",   self.mins_tick.value)
        await _save("ec.pts_per_tick",    self.pts_tick.value)
        await _save("ec.base_max_bonus",  self.max_cap.value)

        if errors:
            await i.response.send_message(
                embed=error_embed("Some values failed to save:\n" + "\n".join(errors)), ephemeral=True
            )
        else:
            await i.response.send_message(
                embed=success_embed("Event math settings updated successfully."), ephemeral=True
            )


class AddZoneModal(discord.ui.Modal, title="Add Decay Zone"):
    """Creates a new entry in the decay.zones_config JSON array."""
    label_in = discord.ui.TextInput(label="Zone Label",     placeholder="e.g. Zone 4")
    dur_in   = discord.ui.TextInput(label="Duration (days, -1 = infinite)", placeholder="e.g. 14  or  -1")
    rate_in  = discord.ui.TextInput(label="Rate (pts/day)", placeholder="e.g. 25.0")

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        try:
            dur  = int(self.dur_in.value.strip())
            rate = float(self.rate_in.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=error_embed("Duration must be an integer; Rate must be a number."), ephemeral=True
            )

        raw = await get_config_or_none(gid, "decay.zones_config")
        zones: list[dict] = []
        if raw:
            try:
                zones = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                zones = []

        next_id = max((z.get("zone_id", 0) for z in zones), default=0) + 1
        zones.append({
            "zone_id":      next_id,
            "label":        self.label_in.value.strip() or f"Zone {next_id}",
            "duration_days": dur,
            "rate_per_day": rate,
        })

        try:
            await set_config(gid, "decay.zones_config", json.dumps(zones), str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"Zone `{zones[-1]['label']}` added successfully."), ephemeral=True
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to save zone:\n`{exc}`"), ephemeral=True
            )


class EditZoneModal(discord.ui.Modal):
    """Edits duration_days and rate_per_day of an existing zone in-place."""
    dur_in  = discord.ui.TextInput(label="New Duration (days, -1 = infinite)", placeholder="e.g. 14")
    rate_in = discord.ui.TextInput(label="New Rate (pts/day)", placeholder="e.g. 20.0")

    def __init__(self, zone_id: int):
        super().__init__(title=f"Edit Zone {zone_id}")
        self.zone_id = zone_id

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        try:
            new_dur  = int(self.dur_in.value.strip())
            new_rate = float(self.rate_in.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=error_embed("Duration must be an integer; Rate must be a number."), ephemeral=True
            )

        raw = await get_config_or_none(gid, "decay.zones_config")
        try:
            zones: list[dict] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            zones = []

        updated = False
        for z in zones:
            if int(z.get("zone_id", -99)) == self.zone_id:
                z["duration_days"] = new_dur
                z["rate_per_day"]  = new_rate
                updated = True
                break

        if not updated:
            return await i.response.send_message(
                embed=error_embed(f"Zone ID `{self.zone_id}` not found."), ephemeral=True
            )

        try:
            await set_config(gid, "decay.zones_config", json.dumps(zones), str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"Zone `{self.zone_id}` updated."), ephemeral=True
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to save:\n`{exc}`"), ephemeral=True
            )


class VoteScoresModal(discord.ui.Modal, title="Edit Vote Scores"):
    """Sets all three vote score weights in one submit."""
    pos_in = discord.ui.TextInput(label="Score Positive (👍)", placeholder="e.g. 5")
    neu_in = discord.ui.TextInput(label="Score Neutral  (😐)", placeholder="e.g. 3")
    neg_in = discord.ui.TextInput(label="Score Negative (👎)", placeholder="e.g. 1")

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        uid = str(i.user.id)
        errors: list[str] = []

        async def _save(key: str, raw: str) -> None:
            val = raw.strip()
            if not val:
                return
            try:
                float(val)
                await set_config(gid, key, val, uid)
            except ValueError:
                errors.append(f"`{key}` — not a valid number.")
            except Exception as exc:
                errors.append(f"`{key}` — {exc}")

        await _save("vote.score_positive", self.pos_in.value)
        await _save("vote.score_neutral",  self.neu_in.value)
        await _save("vote.score_negative", self.neg_in.value)

        if errors:
            await i.response.send_message(
                embed=error_embed("Some scores failed:\n" + "\n".join(errors)), ephemeral=True
            )
        else:
            await i.response.send_message(
                embed=success_embed("Vote scores updated."), ephemeral=True
            )


class ForcePointsModal(discord.ui.Modal):
    """Direct balance manipulation modal. FIX-ADM-008: uses re.sub for ID parsing."""
    user_input   = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789012345678")
    amount_input = discord.ui.TextInput(label="Point Amount",        placeholder="e.g. 100")

    def __init__(self, action: str):
        super().__init__(title=f"Force {'Award' if action == 'award' else 'Deduct'} Points")
        self.action = action

    async def on_submit(self, i: discord.Interaction) -> None:
        uid_raw = re.sub(r"[<@!>]", "", self.user_input.value.strip())
        try:
            amount = float(self.amount_input.value.strip())
            uid    = str(int(uid_raw))
        except ValueError:
            return await i.response.send_message(
                embed=error_embed("Invalid User ID or amount. Enter a numeric ID or @mention."),
                ephemeral=True,
            )

        if amount <= 0:
            return await i.response.send_message(
                embed=error_embed("Amount must be greater than 0."), ephemeral=True
            )

        gid = str(i.guild_id)
        if self.action == "award":
            await award(gid, uid, amount)
            await i.response.send_message(
                embed=success_embed(f"Awarded **{amount:.0f} pts** to <@{uid}>."), ephemeral=True
            )
        else:
            ok = await deduct(gid, uid, amount)
            if ok:
                await i.response.send_message(
                    embed=success_embed(f"Deducted **{amount:.0f} pts** from <@{uid}>."), ephemeral=True
                )
            else:
                await i.response.send_message(
                    embed=error_embed(f"User <@{uid}> not found or has no balance."), ephemeral=True
                )


class AddShopItemModal(discord.ui.Modal, title="Add New Shop Item"):
    """Creates a new row in shop_items."""
    label_in = discord.ui.TextInput(label="Item Name",    placeholder="e.g. VIP Badge",     max_length=80)
    desc_in  = discord.ui.TextInput(label="Description",  placeholder="What does this do?",  style=discord.TextStyle.paragraph, required=False)
    cost_in  = discord.ui.TextInput(label="Cost (pts)",   placeholder="e.g. 500")
    type_in  = discord.ui.TextInput(label="Type",         placeholder="consumable / role / rental / permanent")

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        try:
            cost = int(self.cost_in.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=error_embed("Cost must be a whole number."), ephemeral=True
            )

        item_type = self.type_in.value.strip().lower()
        if item_type not in ("consumable", "role", "rental", "permanent"):
            return await i.response.send_message(
                embed=error_embed("Type must be one of: `consumable`, `role`, `rental`, `permanent`."),
                ephemeral=True,
            )

        import uuid
        item_id = str(uuid.uuid4())[:8]

        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO shop_items (item_id, guild_id, label, description, cost, item_type, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                    """,
                    item_id, gid,
                    self.label_in.value.strip(),
                    self.desc_in.value.strip() or None,
                    cost,
                    item_type,
                )
            await i.response.send_message(
                embed=success_embed(f"Item **{self.label_in.value.strip()}** created (ID: `{item_id}`)."),
                ephemeral=True,
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to create item:\n`{exc}`"), ephemeral=True
            )


class EditShopItemModal(discord.ui.Modal):
    """Edits an existing shop item's label, description, cost, and type."""
    label_in = discord.ui.TextInput(label="Item Name",  max_length=80)
    desc_in  = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False)
    cost_in  = discord.ui.TextInput(label="Cost (pts)")
    type_in  = discord.ui.TextInput(label="Type (consumable/role/rental/permanent)")

    def __init__(self, item: dict):
        super().__init__(title=f"Edit: {item['label'][:30]}")
        self.item_id         = item["item_id"]
        self.label_in.default = item["label"]
        self.desc_in.default  = item.get("description") or ""
        self.cost_in.default  = str(item["cost"])
        self.type_in.default  = item["item_type"]

    async def on_submit(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        try:
            cost = int(self.cost_in.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=error_embed("Cost must be a whole number."), ephemeral=True
            )

        item_type = self.type_in.value.strip().lower()
        if item_type not in ("consumable", "role", "rental", "permanent"):
            return await i.response.send_message(
                embed=error_embed("Type must be: `consumable`, `role`, `rental`, or `permanent`."),
                ephemeral=True,
            )

        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE shop_items
                    SET label=$3, description=$4, cost=$5, item_type=$6
                    WHERE item_id=$1 AND guild_id=$2
                    """,
                    self.item_id, gid,
                    self.label_in.value.strip(),
                    self.desc_in.value.strip() or None,
                    cost, item_type,
                )
            await i.response.send_message(
                embed=success_embed("Item updated successfully."), ephemeral=True
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to update item:\n`{exc}`"), ephemeral=True
            )


# =============================================================================
# CONFIRMATION VIEWS
# =============================================================================

class ConfirmSystemStateView(_TimeoutView):
    """Confirmation gate before committing a system state change. FIX-ADM-004."""

    def __init__(self, target_state: str):
        super().__init__(timeout=60)
        self.target_state = target_state

    @discord.ui.button(label="✅ Yes, Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.defer()
        try:
            await set_config(str(i.guild_id), "system.state", self.target_state, str(i.user.id))
            icon = "🟢" if self.target_state == "ACTIVE" else "🟡"
            await i.edit_original_response(
                embed=success_embed(f"System state changed to {icon} **{self.target_state}**."),
                view=None,
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to change system state:\n`{exc}`"), view=None
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.edit_message(
            embed=discord.Embed(description="State change cancelled.", color=PRIMARY), view=None
        )


class ConfirmDeleteItemView(_TimeoutView):
    """Confirmation gate before permanently deleting a shop item."""

    def __init__(self, item_id: str, item_label: str):
        super().__init__(timeout=60)
        self.item_id    = item_id
        self.item_label = item_label

    @discord.ui.button(label="🗑️ Yes, Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.defer()
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM shop_items WHERE item_id=$1 AND guild_id=$2",
                    self.item_id, str(i.guild_id),
                )
            await i.edit_original_response(
                embed=success_embed(f"Item **{self.item_label}** deleted."), view=None
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to delete item:\n`{exc}`"), view=None
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.edit_message(
            embed=discord.Embed(description="Deletion cancelled.", color=PRIMARY), view=None
        )


# =============================================================================
# CHANNEL SELECT COMPONENT
# =============================================================================

class AdminChannelSel(discord.ui.ChannelSelect):
    """Inline channel picker that writes directly to bot_config. FIX-ADM-005."""

    def __init__(self, key: str, placeholder: str, ctype: list):
        self.cfg_key = key
        super().__init__(placeholder=placeholder, channel_types=ctype, min_values=1, max_values=1)

    async def callback(self, i: discord.Interaction) -> None:
        await i.response.defer()
        try:
            await set_config(str(i.guild_id), self.cfg_key, str(self.values[0].id), str(i.user.id))
            await i.edit_original_response(
                embed=success_embed(f"`{self.cfg_key}` set to <#{self.values[0].id}>."),
                view=self.view,
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"),
                view=self.view,
            )


# =============================================================================
# MANAGE ZONE SELECT
# =============================================================================

class ManageZoneSelect(discord.ui.Select):
    """Dropdown of current decay zones. Selecting one opens an edit/delete view."""

    def __init__(self, zones: list[dict]):
        options = []
        for z in sorted(zones, key=lambda x: x.get("zone_id", 0)):
            dur   = int(z.get("duration_days", -1))
            rate  = float(z.get("rate_per_day", 0))
            label = z.get("label", f"Zone {z.get('zone_id','?')}")
            dur_s = f"{dur}d" if dur != -1 else "∞"
            options.append(discord.SelectOption(
                label=label[:25],
                value=str(z.get("zone_id")),
                description=f"{dur_s} · {rate} pts/day",
            ))
        super().__init__(placeholder="Select a zone to manage…", options=options or [
            discord.SelectOption(label="No zones configured", value="_none", description="Add zones first")
        ])
        self._zones = {str(z.get("zone_id")): z for z in zones}

    async def callback(self, i: discord.Interaction) -> None:
        if self.values[0] == "_none":
            return await i.response.defer()
        zone_id = int(self.values[0])
        zone    = self._zones.get(self.values[0], {})
        view    = AdminZoneControlView(zone_id, zone, parent_view=self.view)
        e = discord.Embed(
            title=f"📐 {zone.get('label', f'Zone {zone_id}')}",
            description=(
                f"**Duration:** `{zone.get('duration_days', -1)}d` (-1 = infinite)\n"
                f"**Rate:** `{zone.get('rate_per_day', 0)} pts/day`\n\n"
                "Choose an action below."
            ),
            color=PRIMARY,
        )
        await i.response.edit_message(embeds=[e], view=view)


class AdminZoneControlView(_TimeoutView):
    """Inline control panel for a single decay zone."""

    def __init__(self, zone_id: int, zone: dict, parent_view: discord.ui.View):
        super().__init__(timeout=300)
        self.zone_id     = zone_id
        self.zone        = zone
        self.parent_view = parent_view

    @discord.ui.button(label="✏️ Edit Zone", style=discord.ButtonStyle.primary)
    async def edit_zone(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(EditZoneModal(self.zone_id))

    @discord.ui.button(label="🗑️ Delete Zone", style=discord.ButtonStyle.danger)
    async def delete_zone(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        gid = str(i.guild_id)
        raw = await get_config_or_none(gid, "decay.zones_config")
        try:
            zones: list[dict] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            zones = []

        zones = [z for z in zones if int(z.get("zone_id", -99)) != self.zone_id]
        try:
            await set_config(gid, "decay.zones_config", json.dumps(zones), str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"Zone `{self.zone_id}` deleted."), ephemeral=True
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to delete zone:\n`{exc}`"), ephemeral=True
            )

    @discord.ui.button(label="← Back to Zones", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        embeds = await _point_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=self.parent_view)


# =============================================================================
# MANAGE ITEM SELECT
# =============================================================================

class ManageItemSelect(discord.ui.Select):
    """Dropdown of current shop items. Selecting one opens AdminItemControlView."""

    def __init__(self, items: list):
        options = [
            discord.SelectOption(
                label=row["label"][:25],
                value=row["item_id"],
                description=f"{row['cost']} pts · {row['item_type']}",
            )
            for row in items
        ] or [discord.SelectOption(label="No items in shop", value="_none", description="Add items first")]

        super().__init__(placeholder="Select an item to manage…", options=options)
        self._items = {row["item_id"]: dict(row) for row in items}

    async def callback(self, i: discord.Interaction) -> None:
        if self.values[0] == "_none":
            return await i.response.defer()

        item = self._items.get(self.values[0])
        if not item:
            return await i.response.send_message(
                embed=error_embed("Item not found. Try reopening the shop panel."), ephemeral=True
            )

        embeds = await _item_control_embeds(i, item)
        view   = AdminItemControlView(item, parent_view=self.view)
        await i.response.edit_message(embeds=embeds, view=view)


class AdminItemControlView(_TimeoutView):
    """Control panel for a single shop item."""

    def __init__(self, item: dict, parent_view: discord.ui.View):
        super().__init__(timeout=300)
        self.item        = item
        self.parent_view = parent_view

    @discord.ui.button(label="✏️ Edit Details & Price", style=discord.ButtonStyle.primary)
    async def edit_item(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(EditShopItemModal(self.item))

    @discord.ui.button(label="🎚️ Set Rarity", style=discord.ButtonStyle.secondary)
    async def set_rarity(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        # Rarity is stored as a bot_config key per item to avoid a schema migration.
        key = f"shop.item.{self.item['item_id']}.rarity"
        await _open_number_modal(i, key, "Set Item Rarity", "e.g. common / uncommon / rare / legendary")

    @discord.ui.button(label="🛒 Toggle Black Market", style=discord.ButtonStyle.secondary)
    async def toggle_bm(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.defer()
        new_bm = not bool(self.item.get("is_blackmarket", False))
        pool   = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE shop_items SET is_blackmarket=$3 WHERE item_id=$1 AND guild_id=$2",
                    self.item["item_id"], str(i.guild_id), new_bm,
                )
            self.item["is_blackmarket"] = new_bm
            status = "enabled" if new_bm else "disabled"
            await i.edit_original_response(
                embed=success_embed(f"Black market {status} for **{self.item['label']}**."),
                view=self,
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to toggle black market:\n`{exc}`"), view=self
            )

    @discord.ui.button(label="🗑️ Delete Item", style=discord.ButtonStyle.danger)
    async def delete_item(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        view = ConfirmDeleteItemView(self.item["item_id"], self.item["label"])
        e    = confirm_embed(
            "Confirm Deletion",
            f"Delete **{self.item['label']}** permanently?\nThis cannot be undone.",
        )
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="← Back to Shop", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.edit_message(
            embeds=await _shop_panel_embeds(i, _count_items_from_view(self.parent_view)),
            view=self.parent_view,
        )


def _count_items_from_view(view: discord.ui.View) -> int:
    """Extracts the item count from a ManageItemSelect's options for embed display."""
    for child in view.children:
        if isinstance(child, ManageItemSelect):
            return len(child._items)
    return 0


# =============================================================================
# CATEGORY PANEL VIEWS
# =============================================================================

class AdminSystemView(_TimeoutView):
    """⚙️ System & Channels panel."""

    def __init__(self):
        super().__init__()
        # Channel selects — each occupies its own row (rows 1-3).
        self.add_item(AdminChannelSel("channel.gamenight_id",  "🎮 Gamenight Channel…",  [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.activity_id",   "📢 Activity Channel…",   [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.vc_category_id","🔊 VC Category…",        [discord.ChannelType.category]))

    # Row 0 — state control
    @discord.ui.button(label="▶️ Set ACTIVE",  style=discord.ButtonStyle.success,   row=0)
    async def set_active(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        e = confirm_embed(
            "Confirmation: Set ACTIVE",
            "The bot will start accepting public commands.\nEnsure all channels are configured.",
        )
        view = ConfirmSystemStateView("ACTIVE")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="⏸️ Set PAUSED", style=discord.ButtonStyle.secondary,  row=0)
    async def set_paused(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        e = confirm_embed(
            "Confirmation: Set PAUSED",
            "The bot will stop serving public commands.\nAdmin commands remain active.",
            warning="Active events will not be interrupted but no new ones can start.",
        )
        view = ConfirmSystemStateView("PAUSED")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="🏷️ Point Name",  style=discord.ButtonStyle.secondary,  row=0)
    async def point_name(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "system.point_name", "Rename Currency", "e.g. coins, gems, stars")

    @discord.ui.button(label="⚡ Force Action", style=discord.ButtonStyle.danger,   row=0)
    async def force_action(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        if not await require_mod(i):
            return
        view = _ForceSubView(parent_view=self)
        e    = discord.Embed(
            title="⚡ ꜰᴏʀᴄᴇ ᴀᴄᴛɪᴏɴ",
            description=(
                "⚠️ Direct balance manipulation — bypasses all economy logic.\n"
                "All actions are permanent and recorded in the audit log.\n\n"
                "**➕ Award** — Adds points to the target's raw balance.\n"
                "**➖ Deduct** — Removes points; floors at 0."
            ),
            color=WARNING,
        )
        await i.response.edit_message(embed=e, view=view)

    # Row 4 — utility & back
    @discord.ui.button(label="👁️ View All Config", style=discord.ButtonStyle.primary, row=4)
    async def view_all(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.defer(ephemeral=True)
        all_cfg = await get_all_config(str(i.guild_id))
        lines   = [f"{k}: {v}" for k, v in sorted(all_cfg.items())]
        txt     = "\n".join(lines)
        file    = discord.File(io.BytesIO(txt.encode()), filename="config_dump.txt")
        await i.followup.send(
            content=f"📄 Config dump — {len(lines)} keys:", file=file, ephemeral=True
        )

    @discord.ui.button(label="← Main Menu", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _navigate_to_root(i, self)


class _ForceSubView(_TimeoutView):
    """Inline award/deduct button pair — shown in place of the system panel."""

    def __init__(self, parent_view: discord.ui.View):
        super().__init__()
        self.parent_view = parent_view

    @discord.ui.button(label="➕ Award Points",  style=discord.ButtonStyle.success)
    async def award_pts(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(ForcePointsModal("award"))

    @discord.ui.button(label="➖ Deduct Points", style=discord.ButtonStyle.danger)
    async def deduct_pts(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(ForcePointsModal("deduct"))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        embeds = await _system_panel_embeds(i)
        self.parent_view.message = self.message
        await i.response.edit_message(embeds=embeds, view=self.parent_view)


class AdminPointView(_TimeoutView):
    """👥 Point panel — Economy + Decay unified."""

    # Row 0 — event economy
    @discord.ui.button(label="📥 Join Bonus",        style=discord.ButtonStyle.secondary, row=0)
    async def join_bonus(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "ec.join_bonus", "Join Bonus (pts)", "e.g. 15")

    @discord.ui.button(label="🏁 Event End Bonus",   style=discord.ButtonStyle.secondary, row=0)
    async def comp_bonus(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "ec.completion_bonus", "Event End Bonus (pts)", "e.g. 10")

    @discord.ui.button(label="⚙️ Event Math",         style=discord.ButtonStyle.primary,   row=0)
    async def event_math(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(EventMathModal())

    # Row 1 — decay
    @discord.ui.button(label="🛡️ Grace Period",       style=discord.ButtonStyle.secondary, row=1)
    async def grace(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "decay.grace_days", "Grace Period (days)", "e.g. 7")

    @discord.ui.button(label="➕ Add Zone",            style=discord.ButtonStyle.secondary, row=1)
    async def add_zone(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(AddZoneModal())

    @discord.ui.button(label="✏️ Manage Zones",        style=discord.ButtonStyle.primary,   row=1)
    async def manage_zones(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        gid = str(i.guild_id)
        raw = await get_config_or_none(gid, "decay.zones_config")
        try:
            zones: list[dict] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            zones = []

        if not zones:
            return await i.response.send_message(
                embed=error_embed("No decay zones configured. Use **➕ Add Zone** first."),
                ephemeral=True,
            )

        view = _ZoneSelectView(zones, parent_point_view=self)
        e    = discord.Embed(
            title="✏️ ᴍᴀɴᴀɢᴇ ᴢᴏɴᴇꜱ",
            description="Select a zone from the dropdown to edit or delete it.",
            color=PRIMARY,
        )
        await i.response.edit_message(embed=e, view=view)

    # Row 2 — back
    @discord.ui.button(label="← Main Menu", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _navigate_to_root(i, self)


class _ZoneSelectView(_TimeoutView):
    """Wraps ManageZoneSelect in a navigable view."""

    def __init__(self, zones: list[dict], parent_point_view: discord.ui.View):
        super().__init__()
        self.parent_point_view = parent_point_view
        self.add_item(ManageZoneSelect(zones))

    @discord.ui.button(label="← Back to Point", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        embeds = await _point_panel_embeds(i)
        self.parent_point_view.message = self.message
        await i.response.edit_message(embeds=embeds, view=self.parent_point_view)


class AdminHostView(_TimeoutView):
    """🎮 Host & Events panel."""

    @discord.ui.button(label="⏱️ Host Cooldown",         style=discord.ButtonStyle.secondary, row=0)
    async def cooldown(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.cooldown_hours", "Host Cooldown (hours)", "e.g. 12")

    @discord.ui.button(label="📏 Min Duration",           style=discord.ButtonStyle.secondary, row=0)
    async def min_dur(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.min_duration_minutes", "Min Duration (minutes)", "e.g. 45")

    @discord.ui.button(label="👻 Auto-End VC Tolerance",  style=discord.ButtonStyle.secondary, row=0)
    async def auto_end(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.auto_end_tolerance_minutes", "Auto-End Tolerance (minutes)", "e.g. 5")

    @discord.ui.button(label="💰 Income Multiplier",      style=discord.ButtonStyle.secondary, row=1)
    async def income_mult(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.income_multiplier", "Host Income Multiplier", "e.g. 2.0")

    @discord.ui.button(label="🔄 Rolling Window",         style=discord.ButtonStyle.secondary, row=1)
    async def rolling(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.rolling_window", "Reputation Rolling Window (events)", "e.g. 10")

    @discord.ui.button(label="← Main Menu", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _navigate_to_root(i, self)


class AdminVoteView(_TimeoutView):
    """🗳️ Vote & Reputation panel."""

    @discord.ui.button(label="👥 Min Voters",    style=discord.ButtonStyle.secondary, row=0)
    async def min_voters(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.min_voters", "Minimum Voters", "e.g. 5")

    @discord.ui.button(label="✂️ Outlier Trim",  style=discord.ButtonStyle.secondary, row=0)
    async def outlier(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "host.outlier_trim_threshold", "Outlier Trim Threshold (votes)", "e.g. 8")

    @discord.ui.button(label="⏲️ Vote Window",   style=discord.ButtonStyle.secondary, row=0)
    async def window(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _open_number_modal(i, "vote.window_minutes", "Vote Window (minutes)", "e.g. 10")

    @discord.ui.button(label="⭐ Edit Scores",   style=discord.ButtonStyle.primary,   row=0)
    async def edit_scores(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(VoteScoresModal())

    @discord.ui.button(label="← Main Menu", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _navigate_to_root(i, self)


class AdminShopView(_TimeoutView):
    """🛒 Shop Management panel. Requires async construction via AdminShopView.create()."""

    def __init__(self, items: list):
        super().__init__()
        self._item_count = len(items)
        if items:
            self.add_item(ManageItemSelect(items))

    @classmethod
    async def create(cls, i: discord.Interaction) -> "AdminShopView":
        """Async factory: fetches shop items and builds the view."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            items = await conn.fetch(
                "SELECT item_id, label, description, cost, item_type, is_active, is_blackmarket, duration_days "
                "FROM shop_items WHERE guild_id=$1 AND is_active=TRUE ORDER BY label",
                str(i.guild_id),
            )
        return cls(list(items))

    # Row 0 — note: ManageItemSelect occupies row 0; buttons are on row 1
    @discord.ui.button(label="➕ Add New Item", style=discord.ButtonStyle.primary, row=1)
    async def add_item(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await i.response.send_modal(AddShopItemModal())

    @discord.ui.button(label="🌌 Black Market",  style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def black_market(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        pass  # 🔒 Coming Soon

    @discord.ui.button(label="← Main Menu", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button) -> None:
        await _navigate_to_root(i, self)


# =============================================================================
# ROOT NAV SELECT + ROOT VIEW
# =============================================================================

class AdminNavSelect(discord.ui.Select):
    """
    THE single dropdown that IS the /admin main menu.
    Exactly 5 categories as specified. No buttons on the root view.
    """

    def __init__(self):
        options = [
            discord.SelectOption(
                label="⚙️ System & Channels",  value="system",
                description="Bot state, point name, channels, force actions",
            ),
            discord.SelectOption(
                label="👥 Point",               value="point",
                description="Join/end bonus, event math, grace & decay zones",
            ),
            discord.SelectOption(
                label="🎮 Host & Events",        value="host",
                description="Cooldown, duration, auto-end, income multiplier",
            ),
            discord.SelectOption(
                label="🗳️ Vote & Reputation",   value="vote",
                description="Min voters, outlier trim, window, score weights",
            ),
            discord.SelectOption(
                label="🛒 Shop Management",      value="shop",
                description="Add items, manage listings, black market",
            ),
        ]
        super().__init__(
            placeholder="Select a category…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, i: discord.Interaction) -> None:
        val = self.values[0]

        if val == "system":
            embeds = await _system_panel_embeds(i)
            view   = AdminSystemView()

        elif val == "point":
            embeds = await _point_panel_embeds(i)
            view   = AdminPointView()

        elif val == "host":
            embeds = await _host_panel_embeds(i)
            view   = AdminHostView()

        elif val == "vote":
            embeds = await _vote_panel_embeds(i)
            view   = AdminVoteView()

        elif val == "shop":
            view   = await AdminShopView.create(i)
            embeds = await _shop_panel_embeds(i, view._item_count)

        else:
            return await i.response.defer()

        view.message = self.view.message
        await i.response.edit_message(embeds=embeds, view=view)


class AdminRootView(_TimeoutView):
    """Main /admin view — contains ONLY the AdminNavSelect dropdown."""

    def __init__(self):
        super().__init__()
        self.add_item(AdminNavSelect())


# =============================================================================
# BACK-NAVIGATION HELPER
# =============================================================================

async def _navigate_to_root(i: discord.Interaction, current_view: _TimeoutView) -> None:
    """
    Shared back-navigation: re-renders the root panel and swaps to AdminRootView.
    Pass the current sub-view so the message reference can be forwarded.
    """
    embeds    = await _root_panel_embeds(i)
    root_view = AdminRootView()
    root_view.message = current_view.message
    await i.response.edit_message(embeds=embeds, view=root_view)


# =============================================================================
# COG
# =============================================================================

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="admin", description="Administrator control panel.")
    async def admin_cmd(self, i: discord.Interaction) -> None:
        if not await require_admin(i):
            return

        gid   = str(i.guild_id)
        state = await get_config_or_none(gid, "system.state")

        if not state or state == "UNCONFIGURED":
            # First-run: auto-initialise with the Balanced preset.
            await i.response.defer(ephemeral=True)
            try:
                preset_data = dict(PRESETS["balanced"])
                preset_data["system.state"]      = "ACTIVE"
                preset_data["system.guild_name"] = i.guild.name
                await bulk_set_config(gid, preset_data, str(i.user.id))
                logger.info(f"[Admin] Guild {gid} auto-initialised with Balanced preset by {i.user.id}.")
            except Exception:
                logger.exception(f"[Admin] Auto-init failed for guild {gid}.")
                await i.followup.send(
                    embed=error_embed("Failed to initialise bot configuration. Check logs."),
                    ephemeral=True,
                )
                return

            welcome = discord.Embed(title="🌕  Welcome to Two Moon!", color=0x57F287)
            welcome.description = (
                "The bot has been initialised with the **Balanced** preset and is now **ACTIVE**.\n\n"
                "**Next steps:**\n"
                "• Set channels via **⚙️ System & Channels** → channel dropdowns.\n"
                "• Configure roles via `/owner` → **🔑 Admin Set**.\n"
                "• Fine-tune economy and decay via **👥 Point**.\n\n"
                "Use the dropdown below to navigate."
            )
            welcome.set_footer(text="All settings can be changed anytime from this panel.")

            root_view = AdminRootView()
            await i.followup.send(embed=welcome, view=root_view, ephemeral=True)
            root_view.message = await i.original_response()

        else:
            embeds    = await _root_panel_embeds(i)
            root_view = AdminRootView()
            await i.response.send_message(embeds=embeds, view=root_view, ephemeral=True)
            # FIX-ADM-007: Store message reference for on_timeout.
            root_view.message = await i.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))