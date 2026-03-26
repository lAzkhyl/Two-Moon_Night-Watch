--- cogs/admin.py (原始)
# =====
# MODULE: cogs/admin.py
# =====
# Architecture Overview:
# Administrator Control Panel. Handles day-to-day management: economy parameters,
# game channels, point adjustments, and system state toggling.
#
# Every config change is written to 'config_audit_log' for a full paper trail.
#
# UI ARCHITECTURE:
# Every panel (main + all sub-panels) renders as two stacked embeds per message:
#   Embed 1 — State card: current live values, roles, last audit entry.
#   Embed 2 — Guide card: concise description of each button/setting.
# Main panel carries Asset Server GIF thumbnails. Sub-panels omit thumbnails.
# Emoji constants and thumbnail URLs live in utils/emojis.py.
#
# CHANGELOG:
# FIX-ADM-001: Race condition in Setup Wizard — SELECT FOR UPDATE per guild.
# FIX-ADM-002: Draft delete not atomic — DELETE now in same tx as bulk_set_config.
# FIX-ADM-003: AdminNumberModal returned error without keeping panel alive — fixed.
# FIX-ADM-004: Set ACTIVE/PAUSED now requires ConfirmSystemStateView.
# FIX-ADM-005: AdminChannelSel.callback() defers before DB write.
# FIX-ADM-006: set_active/set_paused now defer before edit.
# FIX-ADM-007: All Views store message ref and call edit on timeout.
# FIX-ADM-008: ForcePointsModal ID parsing replaced lstrip with re.sub.
# FIX-ADM-009: Decay view label corrected from "Tiers" to "Decay".
# FIX-ADM-010: _format_role_ids deduplicated — single shared implementation.
# FIX-ADM-011: AdminDecayView, AdminHostView, AdminVoteView added.
# FIX-ADM-012: require_mod guard in b9 uses explicit defer.
# FIX-ADM-013: Setup Wizard preset descriptions added (wizard later removed).
# FIX-ADM-014: AdminNumberModal shows current value in placeholder.
# REFACTOR-ADM-015: Full UI overhaul — dual-embed layout, small-caps titles,
#                   per-section current-value display, consistent colour palette,
#                   last-audit-entry preview in main panel header.
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
from utils.paginator import Paginator, build_pages
from utils.time import format_relative

logger = logging.getLogger(__name__)


# =====
# PANEL CONSTANTS
# =====

# Small-cap Unicode section titles
_T_ADMIN   = "ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ"
_T_NAV     = "ɴᴀᴠɪɢᴀᴛɪᴏɴ ɢᴜɪᴅᴇ"
_T_ECONOMY = "⚙️ ᴇᴄᴏɴᴏᴍʏ"
_T_EC_G    = "⚙️ ᴇᴄᴏɴᴏᴍʏ ɢᴜɪᴅᴇ"
_T_DECAY   = "⏱️ ᴅᴇᴄᴀʏ"
_T_DC_G    = "⏱️ ᴅᴇᴄᴀʏ ɢᴜɪᴅᴇ"
_T_HOST    = "🎮 ʜᴏꜱᴛ"
_T_HO_G    = "🎮 ʜᴏꜱᴛ ɢᴜɪᴅᴇ"
_T_VOTE    = "🗳️ ᴠᴏᴛᴇ"
_T_VO_G    = "🗳️ ᴠᴏᴛᴇ ɢᴜɪᴅᴇ"
_T_CHANNEL = "📡 ᴄʜᴀɴɴᴇʟꜱ"
_T_CH_G    = "📡 ᴄʜᴀɴɴᴇʟ ɢᴜɪᴅᴇ"
_T_SYSTEM  = "🔧 ꜱʏꜱᴛᴇᴍ"
_T_SY_G    = "🔧 ꜱʏꜱᴛᴇᴍ ɢᴜɪᴅᴇ"
_T_FORCE   = "⚡ ꜰᴏʀᴄᴇ"
_T_FO_G    = "⚡ ꜰᴏʀᴄᴇ ɢᴜɪᴅᴇ"

# Accent colours (main panel only — sub-panels use PRIMARY / WHITE)
_C_RED   = 0xFF0000
_C_WHITE = 0xFFFFFF


# =====
# SHARED UTILITY FUNCTIONS
# =====

def _format_role_ids(guild: discord.Guild, raw_val: str | None) -> str:
    """
    Parses both legacy single-ID strings and modern JSON arrays into role mentions.
    FIX-ADM-010: Single shared implementation — owner.py should import this.
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
    """
    Formats a raw config value for display.
    Returns backtick-wrapped value+suffix, or *(not set)* when absent.
    """
    if not raw or raw in ("null", "[]", ""):
        return "*(not set)*"
    return f"`{raw}{suffix}`"


def _ch(raw: str | None) -> str:
    """Formats a channel/category ID as a Discord mention."""
    return f"<#{raw}>" if raw else "*(not set)*"


# =====
# PANEL EMBED BUILDERS
# Each builder is async and returns [embed_state, embed_guide].
# =====

async def _main_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Main admin panel — two embeds: status card + navigation guide."""
    gid    = str(i.guild_id)
    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")

    ar_display = _format_role_ids(i.guild, ar_raw)
    mr_display = _format_role_ids(i.guild, mr_raw)

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

    # Last-edit footer: shows who edited and exactly which key changed.
    audit_line = ""
    if last_audit:
        relative = format_relative(last_audit["changed_at"])
        key      = last_audit["config_key"]
        new_val  = str(last_audit["new_value"] or "—")
        if len(new_val) > 40:
            new_val = new_val[:37] + "..."
        audit_line = (
            f"\n-# Edited {relative} by <@{last_audit['changed_by']}>\n"
            f"> -# {key} → {new_val}"
        )

    desc_e1 = (
        f"**{TICK_ACTIVE} ACTIVE / {ALERT_PAUSED} PAUSED**\n"
        f"### Authorization:\n"
        f"**Admin:**\n> {ar_display}\n\n"
        f"**Mod:**\n> {mr_display}"
        f"{audit_line}"
    )
    e1 = discord.Embed(title=_T_ADMIN, description=desc_e1, color=_C_RED)
    e1.set_thumbnail(url=THUMBNAIL_ADMIN)

    desc_e2 = (
        "⚙️ Economy\n"
        "> Award rates for joining & completing events.\n"
        "> Tune join bonus, completion bonus, point cap,\n"
        "> and the time window counted toward scaling.\n\n"
        "⏱️ Decay\n"
        "> Controls how quickly idle users lose points.\n"
        "> Set grace period, three zone durations,\n"
        "> and the per-day loss rate for each zone.\n\n"
        "🎮 Host\n"
        "> Rules governing who can host and how often.\n"
        "> Cooldown, min duration, voter threshold,\n"
        "> income multiplier, and reputation window.\n\n"
        "🗳️ Vote\n"
        "> Post-event host reputation system.\n"
        "> Configure the voting window and the score\n"
        "> weight assigned to each vote type.\n\n"
        "📡 Channels\n"
        "> Bind bot features to specific channels.\n"
        "> Gamenight text, activity feed,\n"
        "> and the VC category for event rooms.\n\n"
        "👁️ View All\n"
        "> Export every config key to a .txt file.\n"
        "> Useful for auditing, backup review,\n"
        "> or diagnosing unexpected bot behaviour.\n\n"
        "🔧 System\n"
        "> Master on/off switch for the bot.\n"
        "> ACTIVE enables all public commands;\n"
        "> PAUSED blocks them while admin still works.\n\n"
        "⚡ Force\n"
        "> Direct point balance manipulation. Mod+ only.\n"
        "> Award or deduct any amount from any user\n"
        "> by entering their ID or @mention."
    )
    e2 = discord.Embed(title=_T_NAV, description=desc_e2, color=_C_WHITE)
    e2.set_thumbnail(url=THUMBNAIL_NAV)

    return [e1, e2]


async def _economy_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Economy sub-panel — current values + field descriptions."""
    gid = str(i.guild_id)
    join_b  = await get_config_or_none(gid, "ec.join_bonus")
    comp_b  = await get_config_or_none(gid, "ec.completion_bonus")
    max_b   = await get_config_or_none(gid, "ec.base_max_bonus")
    t_cap   = await get_config_or_none(gid, "ec.t_cap")

    desc_e1 = (
        f"**Join Bonus** — {_val(join_b, ' pts')}\n"
        f"**Completion Bonus** — {_val(comp_b, ' pts')}\n"
        f"**Max Bonus** — {_val(max_b, ' pts')}\n"
        f"**Time Cap** — {_val(t_cap, ' min')}"
    )
    e1 = discord.Embed(title=_T_ECONOMY, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Join Bonus** — Points awarded to each participant at the moment they join.\n"
        "**Completion Bonus** — Extra points distributed when the host ends the event normally.\n"
        "**Max Bonus** — Hard ceiling on total points a single user can earn per event.\n"
        "**Time Cap** — Maximum event minutes counted toward time-based bonus scaling.\n\n"
        "> Adjust **Max Bonus** first if the economy feels inflationary.\n"
        "> Lower **Time Cap** to reduce the edge gained by running very long events."
    )
    e2 = discord.Embed(title=_T_EC_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _decay_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Decay sub-panel — current zone config + decay mechanics guide."""
    gid = str(i.guild_id)
    grace = await get_config_or_none(gid, "decay.grace_days")
    z1_d  = await get_config_or_none(gid, "decay.zone1_days")
    z2_d  = await get_config_or_none(gid, "decay.zone2_days")
    r1    = await get_config_or_none(gid, "decay.rate_zone1")
    r2    = await get_config_or_none(gid, "decay.rate_zone2")
    r3    = await get_config_or_none(gid, "decay.rate_zone3")

    desc_e1 = (
        f"**Grace Period** — {_val(grace, ' days')}\n"
        f"**Zone 1** — {_val(z1_d, ' days')}  ·  {_val(r1, ' pts/day')}\n"
        f"**Zone 2** — {_val(z2_d, ' days')}  ·  {_val(r2, ' pts/day')}\n"
        f"**Zone 3** — indefinite  ·  {_val(r3, ' pts/day')}"
    )
    e1 = discord.Embed(title=_T_DECAY, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "Inactivity decay runs daily. A user's points erode once the grace period expires.\n\n"
        "**Grace Period** — Days of inactivity before any decay begins.\n"
        "**Zone 1 / Zone 2 Duration** — How many days each phase lasts before advancing.\n"
        "**Zone 3** begins after Zone 1 + Zone 2 has elapsed and runs indefinitely.\n\n"
        "**Rate Zone 1–3** — Points lost per day in each zone. Zone 3 is the steepest.\n\n"
        "> Any server activity from the user resets the clock back to the grace period."
    )
    e2 = discord.Embed(title=_T_DC_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _host_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Host sub-panel — current host config + field guide."""
    gid = str(i.guild_id)
    cooldown    = await get_config_or_none(gid, "host.cooldown_hours")
    min_dur     = await get_config_or_none(gid, "host.min_duration_minutes")
    min_voters  = await get_config_or_none(gid, "host.min_voters")
    income_mult = await get_config_or_none(gid, "host.income_multiplier")
    rolling     = await get_config_or_none(gid, "host.rolling_window")
    outlier     = await get_config_or_none(gid, "host.outlier_trim_threshold")

    desc_e1 = (
        f"**Cooldown** — {_val(cooldown, ' hours')}\n"
        f"**Min Duration** — {_val(min_dur, ' min')}\n"
        f"**Min Voters** — {_val(min_voters)}\n"
        f"**Income Multiplier** — {_val(income_mult, '×')}\n"
        f"**Rolling Window** — {_val(rolling, ' events')}\n"
        f"**Outlier Trim** — {_val(outlier, ' votes')}"
    )
    e1 = discord.Embed(title=_T_HOST, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Cooldown** — Hours a host must wait before they can open another event.\n"
        "**Min Duration** — Events shorter than this threshold don't count toward host rewards.\n"
        "**Min Voters** — Minimum vote submissions required to update host reputation.\n"
        "**Income Multiplier** — Host earns this multiple of the standard participant reward.\n"
        "**Rolling Window** — Number of recent events averaged to compute reputation score.\n"
        "**Outlier Trim** — Votes above this per-event count are discarded to prevent brigading."
    )
    e2 = discord.Embed(title=_T_HO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _vote_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Vote sub-panel — current scoring config + voting system guide."""
    gid = str(i.guild_id)
    window = await get_config_or_none(gid, "vote.window_minutes")
    pos    = await get_config_or_none(gid, "vote.score_positive")
    neu    = await get_config_or_none(gid, "vote.score_neutral")
    neg    = await get_config_or_none(gid, "vote.score_negative")

    desc_e1 = (
        f"**Vote Window** — {_val(window, ' min')}\n"
        f"**Score Positive** — {_val(pos, ' pts')}\n"
        f"**Score Neutral** — {_val(neu, ' pts')}\n"
        f"**Score Negative** — {_val(neg, ' pts')}"
    )
    e1 = discord.Embed(title=_T_VOTE, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "Voting opens immediately after an event ends and closes once the window expires.\n\n"
        "**Vote Window** — Minutes the vote poll stays open after event close.\n"
        "**Score Positive** — Reputation points added per 👍 vote.\n"
        "**Score Neutral** — Reputation points added per 😐 vote.\n"
        "**Score Negative** — Reputation points added per 👎 vote.\n\n"
        "> Scores feed into the host's rolling reputation average.\n"
        "> Lower scores don't lock a host out — they reduce priority and income multiplier."
    )
    e2 = discord.Embed(title=_T_VO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _channel_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Channels sub-panel — current assignments + channel purpose guide."""
    gid = str(i.guild_id)
    gn_raw  = await get_config_or_none(gid, "channel.gamenight_id")
    act_raw = await get_config_or_none(gid, "channel.activity_id")
    vc_raw  = await get_config_or_none(gid, "channel.vc_category_id")

    desc_e1 = (
        f"**Gamenight** — {_ch(gn_raw)}\n"
        f"**Activity** — {_ch(act_raw)}\n"
        f"**VC Category** — {_ch(vc_raw)}"
    )
    e1 = discord.Embed(title=_T_CHANNEL, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Gamenight Channel** — Text channel for event announcements, join calls, and results.\n"
        "**Activity Channel** — General notification feed: milestones, leaderboards, bot events.\n"
        "**VC Category** — Voice category where temporary event rooms are created and destroyed.\n\n"
        "> All three must be assigned for events to function correctly.\n"
        "> Changes apply immediately — no restart required.\n"
        "> Use the dropdowns below to select channels."
    )
    e2 = discord.Embed(title=_T_CH_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _system_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """System sub-panel — current bot state + state-change guide."""
    gid   = str(i.guild_id)
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"

    if state == "ACTIVE":
        indicator = f"{TICK_ACTIVE} **ACTIVE**"
    elif state == "PAUSED":
        indicator = f"{ALERT_PAUSED} **PAUSED**"
    else:
        indicator = "🔴 **UNCONFIGURED**"

    desc_e1 = (
        f"Current state: {indicator}\n\n"
        f"**▶️ ACTIVE** — All public-facing commands are enabled.\n"
        f"**⏸️ PAUSED** — Public commands are blocked server-wide."
    )
    e1 = discord.Embed(title=_T_SYSTEM, description=desc_e1, color=WARNING)

    desc_e2 = (
        "**▶️ ACTIVE** — Users can join events, check balances, use the shop, and vote. Full functionality.\n"
        "**⏸️ PAUSED** — `/shop`, `/gamenight`, and all user-facing commands are disabled globally. "
        "Admin and owner commands remain fully operational.\n\n"
        "> Use **PAUSED** during maintenance, data migrations, or economy resets\n"
        "> to prevent race conditions with active users.\n"
        "> A confirmation prompt is shown before any state change executes."
    )
    e2 = discord.Embed(title=_T_SY_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _force_panel_embeds(_: discord.Interaction) -> list[discord.Embed]:
    """Force sub-panel — action summary + usage guide."""
    desc_e1 = (
        "⚠️ Direct balance manipulation — bypasses all economy logic.\n"
        "All force actions are permanent and recorded in the audit log.\n\n"
        "**➕ Award** — Adds points directly to the target's raw balance.\n"
        "**➖ Deduct** — Removes points. Fails gracefully if the user has no balance."
    )
    e1 = discord.Embed(title=_T_FORCE, description=desc_e1, color=WARNING)

    desc_e2 = (
        "**Target** — Enter a numeric User ID or paste a @mention into the modal.\n"
        "**Amount** — Must be a positive number. Decimals are supported (e.g. `12.5`).\n\n"
        "> To get a User ID: right-click or long-press the user → **Copy User ID**.\n"
        "> Deduct will not push a balance below zero — it fails with an error instead.\n"
        "> This section is restricted to **Moderators** and above."
    )
    e2 = discord.Embed(title=_T_FO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


# =====
# SHARED MODAL: AdminNumberModal
# FIX-ADM-014: Accepts optional current_value — displayed in modal placeholder.
# FIX-ADM-003: Error is ephemeral — panel remains usable after a failed submit.
# =====

class AdminNumberModal(discord.ui.Modal):
    value_input = discord.ui.TextInput(label="New Value", placeholder="Enter a number...")

    def __init__(self, key: str, title: str, placeholder: str, current_value: str | None = None):
        super().__init__(title=title[:45])
        self.cfg_key = key
        if current_value is not None:
            self.value_input.placeholder = f"Current: {current_value}  ·  {placeholder}"
        else:
            self.value_input.placeholder = placeholder

    async def on_submit(self, i: discord.Interaction):
        new_val = self.value_input.value.strip()
        try:
            await set_config(str(i.guild_id), self.cfg_key, new_val, str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"`{self.cfg_key}` updated to `{new_val}`."),
                ephemeral=True,
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"),
                ephemeral=True,
            )


async def _open_number_modal(
    i: discord.Interaction,
    key: str,
    title: str,
    placeholder: str,
) -> None:
    """Fetches current value then opens the modal with an informative placeholder."""
    current = await get_config_or_none(str(i.guild_id), key)
    await i.response.send_modal(
        AdminNumberModal(key, title, placeholder, current_value=current)
    )


# =====
# ECONOMY SETTINGS VIEW
# =====

class AdminEconomyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Join Bonus", style=discord.ButtonStyle.secondary)
    async def join_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.join_bonus", "Join Bonus (pts)", "e.g. 15")

    @discord.ui.button(label="Completion Bonus", style=discord.ButtonStyle.secondary)
    async def comp_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.completion_bonus", "Completion Bonus (pts)", "e.g. 10")

    @discord.ui.button(label="Max Bonus", style=discord.ButtonStyle.secondary)
    async def max_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.base_max_bonus", "Max Bonus (pts)", "e.g. 50")

    @discord.ui.button(label="Time Cap (min)", style=discord.ButtonStyle.secondary)
    async def t_cap(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.t_cap", "Time Cap (minutes)", "e.g. 120")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# DECAY SETTINGS VIEW
# FIX-ADM-009: Correctly labelled — was previously mislabelled "Tiers".
# =====

class AdminDecayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Grace Days", style=discord.ButtonStyle.secondary)
    async def grace(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.grace_days", "Grace Days", "Days before decay starts (e.g. 7)")

    @discord.ui.button(label="Zone 1 Days", style=discord.ButtonStyle.secondary)
    async def z1(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.zone1_days", "Zone 1 Days", "Slow decay zone duration (e.g. 7)")

    @discord.ui.button(label="Zone 2 Days", style=discord.ButtonStyle.secondary)
    async def z2(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.zone2_days", "Zone 2 Days", "Medium decay zone duration (e.g. 7)")

    @discord.ui.button(label="Rate Zone 1", style=discord.ButtonStyle.secondary, row=1)
    async def r1(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone1", "Rate Zone 1 (pts/day)", "e.g. 5.0")

    @discord.ui.button(label="Rate Zone 2", style=discord.ButtonStyle.secondary, row=1)
    async def r2(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone2", "Rate Zone 2 (pts/day)", "e.g. 15.0")

    @discord.ui.button(label="Rate Zone 3", style=discord.ButtonStyle.secondary, row=1)
    async def r3(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone3", "Rate Zone 3 (pts/day)", "e.g. 30.0")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# HOST SETTINGS VIEW
# FIX-ADM-011: Full UI coverage for host.* config keys.
# =====

class AdminHostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cooldown (hours)", style=discord.ButtonStyle.secondary)
    async def cooldown(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.cooldown_hours", "Host Cooldown (hours)", "e.g. 12")

    @discord.ui.button(label="Min Duration (min)", style=discord.ButtonStyle.secondary)
    async def min_dur(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.min_duration_minutes", "Min Duration (minutes)", "e.g. 45")

    @discord.ui.button(label="Min Voters", style=discord.ButtonStyle.secondary)
    async def min_voters(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.min_voters", "Minimum Voters", "e.g. 5")

    @discord.ui.button(label="Income Multiplier", style=discord.ButtonStyle.secondary)
    async def income_mult(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.income_multiplier", "Host Income Multiplier", "e.g. 2.0")

    @discord.ui.button(label="Rolling Window", style=discord.ButtonStyle.secondary, row=1)
    async def rolling(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.rolling_window", "Reputation Rolling Window (events)", "e.g. 10")

    @discord.ui.button(label="Outlier Trim", style=discord.ButtonStyle.secondary, row=1)
    async def outlier(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.outlier_trim_threshold", "Outlier Trim Threshold", "e.g. 8")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# VOTE SETTINGS VIEW
# FIX-ADM-011: Full UI coverage for vote.* config keys.
# =====

class AdminVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Vote Window (min)", style=discord.ButtonStyle.secondary)
    async def window(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.window_minutes", "Vote Window (minutes)", "e.g. 10")

    @discord.ui.button(label="Score Positive", style=discord.ButtonStyle.secondary)
    async def pos(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_positive", "Positive Vote Score", "e.g. 5")

    @discord.ui.button(label="Score Neutral", style=discord.ButtonStyle.secondary)
    async def neu(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_neutral", "Neutral Vote Score", "e.g. 3")

    @discord.ui.button(label="Score Negative", style=discord.ButtonStyle.secondary)
    async def neg(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_negative", "Negative Vote Score", "e.g. 1")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# CHANNEL SETTINGS VIEW
# FIX-ADM-005: callback() defers before DB write to avoid timeouts.
# =====

class AdminChannelSel(discord.ui.ChannelSelect):
    def __init__(self, key: str, placeholder: str, ctype: list):
        self.cfg_key = key
        super().__init__(placeholder=placeholder, channel_types=ctype, min_values=1, max_values=1)

    async def callback(self, i: discord.Interaction):
        # FIX-ADM-005: Defer first before DB write.
        await i.response.defer()
        try:
            await set_config(str(i.guild_id), self.cfg_key, str(self.values[0].id), str(i.user.id))
            await i.edit_original_response(
                embed=success_embed(f"`{self.cfg_key}` updated to <#{self.values[0].id}>!"),
                view=self.view,
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"),
                view=self.view,
            )


class AdminChannelGroupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None
        self.add_item(AdminChannelSel("channel.gamenight_id", "🎮 Gamenight Channel...", [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.activity_id",  "📢 Activity Channel...",  [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.vc_category_id", "🔊 VC Category...",     [discord.ChannelType.category]))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# SYSTEM SETTINGS VIEW
# FIX-ADM-004: State changes require ConfirmSystemStateView before execution.
# FIX-ADM-006: set_active/set_paused defer before DB write.
# =====

class ConfirmSystemStateView(discord.ui.View):
    """Confirmation gate before committing a system state change."""

    def __init__(self, target_state: str):
        super().__init__(timeout=60)
        self.target_state = target_state
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="✅ Yes, Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
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
                embed=error_embed(f"Failed to change system state:\n`{exc}`"),
                view=None,
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.edit_message(
            embed=discord.Embed(description="State change cancelled.", color=PRIMARY),
            view=None,
        )


class AdminSystemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="▶️ Set ACTIVE", style=discord.ButtonStyle.success)
    async def set_active(self, i: discord.Interaction, _: discord.ui.Button):
        e = confirm_embed(
            "Confirmation: Set ACTIVE",
            "The bot will start accepting public commands again.\n"
            "Ensure all configurations are correct before activating.",
        )
        view = ConfirmSystemStateView("ACTIVE")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="⏸️ Set PAUSED", style=discord.ButtonStyle.secondary)
    async def set_paused(self, i: discord.Interaction, _: discord.ui.Button):
        e = confirm_embed(
            "Confirmation: Set PAUSED",
            "The bot will stop serving public commands (`/shop`, `/gamenight`, etc).\n"
            "Admin and owner commands will still work.",
            warning="Currently active users will not be able to start new events.",
        )
        view = ConfirmSystemStateView("PAUSED")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# FORCE POINTS VIEW
# FIX-ADM-008: ID parsing fixed from lstrip (char-set) to re.sub (pattern).
# =====

class ForcePointsModal(discord.ui.Modal):
    user_input   = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789012345678 or @User")
    amount_input = discord.ui.TextInput(label="Point Amount",        placeholder="e.g. 100")

    def __init__(self, action: str):
        super().__init__(title=f"Force {'Award' if action == 'award' else 'Deduct'} Points")
        self.action = action

    async def on_submit(self, i: discord.Interaction):
        # FIX-ADM-008: re.sub strips mention syntax robustly.
        uid_raw = re.sub(r"[<@!>]", "", self.user_input.value.strip())
        try:
            amount = float(self.amount_input.value.strip())
            uid    = str(int(uid_raw))
        except ValueError:
            return await i.response.send_message(
                embed=error_embed(
                    "Invalid User ID or amount.\n"
                    "Enter a numeric User ID or a mention like `@Username`."
                ),
                ephemeral=True,
            )

        if amount <= 0:
            return await i.response.send_message(
                embed=error_embed("Amount must be greater than 0."),
                ephemeral=True,
            )

        gid = str(i.guild_id)
        if self.action == "award":
            await award(gid, uid, amount)
            await i.response.send_message(
                embed=success_embed(f"Awarded **{amount:.0f} pts** to <@{uid}>."),
                ephemeral=True,
            )
        else:
            ok = await deduct(gid, uid, amount)
            if ok:
                await i.response.send_message(
                    embed=success_embed(f"Deducted **{amount:.0f} pts** from <@{uid}>."),
                    ephemeral=True,
                )
            else:
                await i.response.send_message(
                    embed=error_embed(f"User <@{uid}> not found or has no points."),
                    ephemeral=True,
                )


class AdminForceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="➕ Award Points",  style=discord.ButtonStyle.success)
    async def award_pts(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(ForcePointsModal("award"))

    @discord.ui.button(label="➖ Deduct Points", style=discord.ButtonStyle.danger)
    async def deduct_pts(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(ForcePointsModal("deduct"))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# MAIN PANEL VIEW
# =====

class AdminMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # FIX-ADM-007: Edit Discord message on timeout so buttons don't ghost.
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="⚙️ Economy", style=discord.ButtonStyle.secondary, row=0)
    async def b1(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _economy_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminEconomyView())

    @discord.ui.button(label="⏱️ Decay", style=discord.ButtonStyle.secondary, row=0)
    async def b2(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _decay_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminDecayView())

    @discord.ui.button(label="🎮 Host", style=discord.ButtonStyle.secondary, row=0)
    async def b3(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _host_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminHostView())

    @discord.ui.button(label="🗳️ Vote", style=discord.ButtonStyle.secondary, row=0)
    async def b4(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _vote_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminVoteView())

    @discord.ui.button(label="📡 Channels", style=discord.ButtonStyle.secondary, row=1)
    async def b5(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _channel_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminChannelGroupView())

    @discord.ui.button(label="👁️ View All", style=discord.ButtonStyle.primary, row=1)
    async def b6(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        all_cfg = await get_all_config(str(i.guild_id))
        lines   = [f"{k}: {v}" for k, v in sorted(all_cfg.items())]
        txt     = "\n".join(lines)
        file    = discord.File(io.BytesIO(txt.encode()), filename="config_dump.txt")
        await i.followup.send(
            content=f"📄 Configuration dump — {len(lines)} keys:",
            file=file,
            ephemeral=True,
        )

    @discord.ui.button(label="🔧 System", style=discord.ButtonStyle.danger, row=1)
    async def b8(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _system_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminSystemView())

    @discord.ui.button(label="⚡ Force", style=discord.ButtonStyle.danger, row=1)
    async def b9(self, i: discord.Interaction, _: discord.ui.Button):
        # FIX-ADM-012: Explicit guard check — does not rely on require_mod side-effect.
        if not await require_mod(i):
            return
        embeds = await _force_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminForceView())


# =====
# DISCORD COG MOUNTING
# =====

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="admin", description="Administrator control panel.")
    async def admin_cmd(self, i: discord.Interaction):
        if not await require_admin(i):
            return

        gid   = str(i.guild_id)
        state = await get_config_or_none(gid, "system.state")

        if not state or state == "UNCONFIGURED":
            # Auto-initialize with Balanced preset. Channels are configured
            # post-init via the 📡 Channels button.
            await i.response.defer(ephemeral=True)
            try:
                preset_data = dict(PRESETS["balanced"])
                preset_data["system.state"]      = "ACTIVE"
                preset_data["system.guild_name"] = i.guild.name
                await bulk_set_config(gid, preset_data, str(i.user.id))
                logger.info(f"[Admin] Guild {gid} auto-initialized with Balanced preset by {i.user.id}.")
            except Exception:
                logger.exception(f"[Admin] Auto-init failed for guild {gid}.")
                await i.followup.send(
                    embed=error_embed("Failed to initialize bot configuration. Check logs."),
                    ephemeral=True,
                )
                return

            welcome = discord.Embed(title="🌕  Welcome to Two Moon!", color=0x57F287)
            welcome.description = (
                "The bot has been initialized with the **Balanced** preset and is now **ACTIVE**.\n\n"
                "**Next steps:**\n"
                "• Set your channels via **📡 Channels** — required for events to function.\n"
                "• Configure access roles via `/owner` → **🔑 Admin Set**.\n"
                "• Fine-tune economy, decay, and host settings using the buttons below."
            )
            welcome.set_footer(text="All values can be changed anytime from this panel.")
            main_view = AdminMainView()
            await i.followup.send(embed=welcome, view=main_view, ephemeral=True)
            main_view.message = await i.original_response()
        else:
            embeds    = await _main_panel_embeds(i)
            main_view = AdminMainView()
            await i.response.send_message(embeds=embeds, view=main_view, ephemeral=True)
            # FIX-ADM-007: Store message reference for on_timeout.
            main_view.message = await i.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))

+++ cogs/admin.py (修改后)
# =====
# MODULE: cogs/admin.py
# =====
# Architecture Overview:
# Administrator Control Panel. Handles day-to-day management: economy parameters,
# game channels, point adjustments, and system state toggling.
#
# Every config change is written to 'config_audit_log' for a full paper trail.
#
# UI ARCHITECTURE:
# Every panel (main + all sub-panels) renders as two stacked embeds per message:
#   Embed 1 — State card: current live values, roles, last audit entry.
#   Embed 2 — Guide card: concise description of each button/setting.
# Main panel carries Asset Server GIF thumbnails. Sub-panels omit thumbnails.
# Emoji constants and thumbnail URLs live in utils/emojis.py.
#
# CHANGELOG:
# FIX-ADM-001: Race condition in Setup Wizard — SELECT FOR UPDATE per guild.
# FIX-ADM-002: Draft delete not atomic — DELETE now in same tx as bulk_set_config.
# FIX-ADM-003: AdminNumberModal returned error without keeping panel alive — fixed.
# FIX-ADM-004: Set ACTIVE/PAUSED now requires ConfirmSystemStateView.
# FIX-ADM-005: AdminChannelSel.callback() defers before DB write.
# FIX-ADM-006: set_active/set_paused now defer before edit.
# FIX-ADM-007: All Views store message ref and call edit on timeout.
# FIX-ADM-008: ForcePointsModal ID parsing replaced lstrip with re.sub.
# FIX-ADM-009: Decay view label corrected from "Tiers" to "Decay".
# FIX-ADM-010: _format_role_ids deduplicated — single shared implementation.
# FIX-ADM-011: AdminDecayView, AdminHostView, AdminVoteView added.
# FIX-ADM-012: require_mod guard in b9 uses explicit defer.
# FIX-ADM-013: Setup Wizard preset descriptions added (wizard later removed).
# FIX-ADM-014: AdminNumberModal shows current value in placeholder.
# REFACTOR-ADM-015: Full UI overhaul — dual-embed layout, small-caps titles,
#                   per-section current-value display, consistent colour palette,
#                   last-audit-entry preview in main panel header.
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
from utils.paginator import Paginator, build_pages
from utils.time import format_relative

logger = logging.getLogger(__name__)


# =====
# PANEL CONSTANTS
# =====

# Small-cap Unicode section titles
_T_ADMIN   = "ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ"
_T_NAV     = "ɴᴀᴠɪɢᴀᴛɪᴏɴ ɢᴜɪᴅᴇ"
_T_ECONOMY = "⚙️ ᴇᴄᴏɴᴏᴍʏ"
_T_EC_G    = "⚙️ ᴇᴄᴏɴᴏᴍʏ ɢᴜɪᴅᴇ"
_T_DECAY   = "⏱️ ᴅᴇᴄᴀʏ"
_T_DC_G    = "⏱️ ᴅᴇᴄᴀʏ ɢᴜɪᴅᴇ"
_T_HOST    = "🎮 ʜᴏꜱᴛ"
_T_HO_G    = "🎮 ʜᴏꜱᴛ ɢᴜɪᴅᴇ"
_T_VOTE    = "🗳️ ᴠᴏᴛᴇ"
_T_VO_G    = "🗳️ ᴠᴏᴛᴇ ɢᴜɪᴅᴇ"
_T_CHANNEL = "📡 ᴄʜᴀɴɴᴇʟꜱ"
_T_CH_G    = "📡 ᴄʜᴀɴɴᴇʟ ɢᴜɪᴅᴇ"
_T_SYSTEM  = "🔧 ꜱʏꜱᴛᴇᴍ"
_T_SY_G    = "🔧 ꜱʏꜱᴛᴇᴍ ɢᴜɪᴅᴇ"
_T_FORCE   = "⚡ ꜰᴏʀᴄᴇ"
_T_FO_G    = "⚡ ꜰᴏʀᴄᴇ ɢᴜɪᴅᴇ"

# Accent colours (main panel only — sub-panels use PRIMARY / WHITE)
_C_RED   = 0xFF0000
_C_WHITE = 0xFFFFFF


# =====
# SHARED UTILITY FUNCTIONS
# =====

def _format_role_ids(guild: discord.Guild, raw_val: str | None) -> str:
    """
    Parses both legacy single-ID strings and modern JSON arrays into role mentions.
    FIX-ADM-010: Single shared implementation — owner.py should import this.
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
    """
    Formats a raw config value for display.
    Returns backtick-wrapped value+suffix, or *(not set)* when absent.
    """
    if not raw or raw in ("null", "[]", ""):
        return "*(not set)*"
    return f"`{raw}{suffix}`"


def _ch(raw: str | None) -> str:
    """Formats a channel/category ID as a Discord mention."""
    return f"<#{raw}>" if raw else "*(not set)*"


# =====
# PANEL EMBED BUILDERS
# Each builder is async and returns [embed_state, embed_guide].
# CMS INTEGRATION: Fetches embed templates from database with fallback to hardcoded defaults.
# =====

async def _get_embed_config(guild_id: str, key: str) -> dict | None:
    """Fetch an embed configuration from the database."""
    try:
        raw = await get_config_or_none(guild_id, key)
        if raw:
            return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


async def _main_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Main admin panel — two embeds: status card + navigation guide."""
    gid    = str(i.guild_id)

    # Try to fetch CMS embed configs first
    main_cfg = await _get_embed_config(gid, "embed.admin.main")
    guide_cfg = await _get_embed_config(gid, "embed.admin.main_guide")

    ar_raw = await get_config_or_none(gid, "system.admin_role_id")
    mr_raw = await get_config_or_none(gid, "system.mod_role_id")

    ar_display = _format_role_ids(i.guild, ar_raw)
    mr_display = _format_role_ids(i.guild, mr_raw)

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

    # Last-edit footer: shows who edited and exactly which key changed.
    audit_line = ""
    if last_audit:
        relative = format_relative(last_audit["changed_at"])
        key      = last_audit["config_key"]
        new_val  = str(last_audit["new_value"] or "—")
        if len(new_val) > 40:
            new_val = new_val[:37] + "..."
        audit_line = (
            f"\n-# Edited {relative} by <@{last_audit['changed_by']}>\n"
            f"> -# {key} → {new_val}"
        )

    # Determine state indicator
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"
    state_indicator = TICK_ACTIVE if state == "ACTIVE" else ALERT_PAUSED if state == "PAUSED" else "🔴"

    # Build description with dynamic values
    desc_e1 = (
        f"**{state_indicator} {state}**\n"
        f"### Authorization:\n"
        f"**Admin:**\n> {ar_display}\n\n"
        f"**Mod:**\n> {mr_display}"
        f"{audit_line}"
    )

    # Use CMS config or fallback to defaults
    if main_cfg:
        e1_desc = main_cfg.get("description", "").format(
            state_indicator=state_indicator,
            state=state,
            admin_roles=ar_display,
            mod_roles=mr_display,
            audit_line=audit_line
        )
        e1 = discord.Embed(
            title=main_cfg.get("title", _T_ADMIN),
            description=e1_desc,
            color=main_cfg.get("color", _C_RED)
        )
        if main_cfg.get("thumbnail"):
            e1.set_thumbnail(url=main_cfg["thumbnail"])
    else:
        e1 = discord.Embed(title=_T_ADMIN, description=desc_e1, color=_C_RED)
        e1.set_thumbnail(url=THUMBNAIL_ADMIN)

    desc_e2 = (
        "⚙️ Economy\n"
        "> Award rates for joining & completing events.\n"
        "> Tune join bonus, completion bonus, point cap,\n"
        "> and the time window counted toward scaling.\n\n"
        "⏱️ Decay\n"
        "> Controls how quickly idle users lose points.\n"
        "> Set grace period, three zone durations,\n"
        "> and the per-day loss rate for each zone.\n\n"
        "🎮 Host\n"
        "> Rules governing who can host and how often.\n"
        "> Cooldown, min duration, voter threshold,\n"
        "> income multiplier, and reputation window.\n\n"
        "🗳️ Vote\n"
        "> Post-event host reputation system.\n"
        "> Configure the voting window and the score\n"
        "> weight assigned to each vote type.\n\n"
        "📡 Channels\n"
        "> Bind bot features to specific channels.\n"
        "> Gamenight text, activity feed,\n"
        "> and the VC category for event rooms.\n\n"
        "👁️ View All\n"
        "> Export every config key to a .txt file.\n"
        "> Useful for auditing, backup review,\n"
        "> or diagnosing unexpected bot behaviour.\n\n"
        "🔧 System\n"
        "> Master on/off switch for the bot.\n"
        "> ACTIVE enables all public commands;\n"
        "> PAUSED blocks them while admin still works.\n\n"
        "⚡ Force\n"
        "> Direct point balance manipulation. Mod+ only.\n"
        "> Award or deduct any amount from any user\n"
        "> by entering their ID or @mention."
    )

    if guide_cfg:
        e2 = discord.Embed(
            title=guide_cfg.get("title", _T_NAV),
            description=guide_cfg.get("description", desc_e2),
            color=guide_cfg.get("color", _C_WHITE)
        )
        if guide_cfg.get("thumbnail"):
            e2.set_thumbnail(url=guide_cfg["thumbnail"])
    else:
        e2 = discord.Embed(title=_T_NAV, description=desc_e2, color=_C_WHITE)
        e2.set_thumbnail(url=THUMBNAIL_NAV)

    return [e1, e2]


async def _economy_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Economy sub-panel — current values + field descriptions."""
    gid = str(i.guild_id)
    join_b  = await get_config_or_none(gid, "ec.join_bonus")
    comp_b  = await get_config_or_none(gid, "ec.completion_bonus")
    max_b   = await get_config_or_none(gid, "ec.base_max_bonus")
    t_cap   = await get_config_or_none(gid, "ec.t_cap")

    desc_e1 = (
        f"**Join Bonus** — {_val(join_b, ' pts')}\n"
        f"**Completion Bonus** — {_val(comp_b, ' pts')}\n"
        f"**Max Bonus** — {_val(max_b, ' pts')}\n"
        f"**Time Cap** — {_val(t_cap, ' min')}"
    )
    e1 = discord.Embed(title=_T_ECONOMY, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Join Bonus** — Points awarded to each participant at the moment they join.\n"
        "**Completion Bonus** — Extra points distributed when the host ends the event normally.\n"
        "**Max Bonus** — Hard ceiling on total points a single user can earn per event.\n"
        "**Time Cap** — Maximum event minutes counted toward time-based bonus scaling.\n\n"
        "> Adjust **Max Bonus** first if the economy feels inflationary.\n"
        "> Lower **Time Cap** to reduce the edge gained by running very long events."
    )
    e2 = discord.Embed(title=_T_EC_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _decay_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Decay sub-panel — current zone config + decay mechanics guide."""
    gid = str(i.guild_id)
    grace = await get_config_or_none(gid, "decay.grace_days")
    z1_d  = await get_config_or_none(gid, "decay.zone1_days")
    z2_d  = await get_config_or_none(gid, "decay.zone2_days")
    r1    = await get_config_or_none(gid, "decay.rate_zone1")
    r2    = await get_config_or_none(gid, "decay.rate_zone2")
    r3    = await get_config_or_none(gid, "decay.rate_zone3")

    desc_e1 = (
        f"**Grace Period** — {_val(grace, ' days')}\n"
        f"**Zone 1** — {_val(z1_d, ' days')}  ·  {_val(r1, ' pts/day')}\n"
        f"**Zone 2** — {_val(z2_d, ' days')}  ·  {_val(r2, ' pts/day')}\n"
        f"**Zone 3** — indefinite  ·  {_val(r3, ' pts/day')}"
    )
    e1 = discord.Embed(title=_T_DECAY, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "Inactivity decay runs daily. A user's points erode once the grace period expires.\n\n"
        "**Grace Period** — Days of inactivity before any decay begins.\n"
        "**Zone 1 / Zone 2 Duration** — How many days each phase lasts before advancing.\n"
        "**Zone 3** begins after Zone 1 + Zone 2 has elapsed and runs indefinitely.\n\n"
        "**Rate Zone 1–3** — Points lost per day in each zone. Zone 3 is the steepest.\n\n"
        "> Any server activity from the user resets the clock back to the grace period."
    )
    e2 = discord.Embed(title=_T_DC_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _host_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Host sub-panel — current host config + field guide."""
    gid = str(i.guild_id)
    cooldown    = await get_config_or_none(gid, "host.cooldown_hours")
    min_dur     = await get_config_or_none(gid, "host.min_duration_minutes")
    min_voters  = await get_config_or_none(gid, "host.min_voters")
    income_mult = await get_config_or_none(gid, "host.income_multiplier")
    rolling     = await get_config_or_none(gid, "host.rolling_window")
    outlier     = await get_config_or_none(gid, "host.outlier_trim_threshold")

    desc_e1 = (
        f"**Cooldown** — {_val(cooldown, ' hours')}\n"
        f"**Min Duration** — {_val(min_dur, ' min')}\n"
        f"**Min Voters** — {_val(min_voters)}\n"
        f"**Income Multiplier** — {_val(income_mult, '×')}\n"
        f"**Rolling Window** — {_val(rolling, ' events')}\n"
        f"**Outlier Trim** — {_val(outlier, ' votes')}"
    )
    e1 = discord.Embed(title=_T_HOST, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Cooldown** — Hours a host must wait before they can open another event.\n"
        "**Min Duration** — Events shorter than this threshold don't count toward host rewards.\n"
        "**Min Voters** — Minimum vote submissions required to update host reputation.\n"
        "**Income Multiplier** — Host earns this multiple of the standard participant reward.\n"
        "**Rolling Window** — Number of recent events averaged to compute reputation score.\n"
        "**Outlier Trim** — Votes above this per-event count are discarded to prevent brigading."
    )
    e2 = discord.Embed(title=_T_HO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _vote_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Vote sub-panel — current scoring config + voting system guide."""
    gid = str(i.guild_id)
    window = await get_config_or_none(gid, "vote.window_minutes")
    pos    = await get_config_or_none(gid, "vote.score_positive")
    neu    = await get_config_or_none(gid, "vote.score_neutral")
    neg    = await get_config_or_none(gid, "vote.score_negative")

    desc_e1 = (
        f"**Vote Window** — {_val(window, ' min')}\n"
        f"**Score Positive** — {_val(pos, ' pts')}\n"
        f"**Score Neutral** — {_val(neu, ' pts')}\n"
        f"**Score Negative** — {_val(neg, ' pts')}"
    )
    e1 = discord.Embed(title=_T_VOTE, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "Voting opens immediately after an event ends and closes once the window expires.\n\n"
        "**Vote Window** — Minutes the vote poll stays open after event close.\n"
        "**Score Positive** — Reputation points added per 👍 vote.\n"
        "**Score Neutral** — Reputation points added per 😐 vote.\n"
        "**Score Negative** — Reputation points added per 👎 vote.\n\n"
        "> Scores feed into the host's rolling reputation average.\n"
        "> Lower scores don't lock a host out — they reduce priority and income multiplier."
    )
    e2 = discord.Embed(title=_T_VO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _channel_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """Channels sub-panel — current assignments + channel purpose guide."""
    gid = str(i.guild_id)
    gn_raw  = await get_config_or_none(gid, "channel.gamenight_id")
    act_raw = await get_config_or_none(gid, "channel.activity_id")
    vc_raw  = await get_config_or_none(gid, "channel.vc_category_id")

    desc_e1 = (
        f"**Gamenight** — {_ch(gn_raw)}\n"
        f"**Activity** — {_ch(act_raw)}\n"
        f"**VC Category** — {_ch(vc_raw)}"
    )
    e1 = discord.Embed(title=_T_CHANNEL, description=desc_e1, color=PRIMARY)

    desc_e2 = (
        "**Gamenight Channel** — Text channel for event announcements, join calls, and results.\n"
        "**Activity Channel** — General notification feed: milestones, leaderboards, bot events.\n"
        "**VC Category** — Voice category where temporary event rooms are created and destroyed.\n\n"
        "> All three must be assigned for events to function correctly.\n"
        "> Changes apply immediately — no restart required.\n"
        "> Use the dropdowns below to select channels."
    )
    e2 = discord.Embed(title=_T_CH_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _system_panel_embeds(i: discord.Interaction) -> list[discord.Embed]:
    """System sub-panel — current bot state + state-change guide."""
    gid   = str(i.guild_id)
    state = await get_config_or_none(gid, "system.state") or "UNCONFIGURED"

    if state == "ACTIVE":
        indicator = f"{TICK_ACTIVE} **ACTIVE**"
    elif state == "PAUSED":
        indicator = f"{ALERT_PAUSED} **PAUSED**"
    else:
        indicator = "🔴 **UNCONFIGURED**"

    desc_e1 = (
        f"Current state: {indicator}\n\n"
        f"**▶️ ACTIVE** — All public-facing commands are enabled.\n"
        f"**⏸️ PAUSED** — Public commands are blocked server-wide."
    )
    e1 = discord.Embed(title=_T_SYSTEM, description=desc_e1, color=WARNING)

    desc_e2 = (
        "**▶️ ACTIVE** — Users can join events, check balances, use the shop, and vote. Full functionality.\n"
        "**⏸️ PAUSED** — `/shop`, `/gamenight`, and all user-facing commands are disabled globally. "
        "Admin and owner commands remain fully operational.\n\n"
        "> Use **PAUSED** during maintenance, data migrations, or economy resets\n"
        "> to prevent race conditions with active users.\n"
        "> A confirmation prompt is shown before any state change executes."
    )
    e2 = discord.Embed(title=_T_SY_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


async def _force_panel_embeds(_: discord.Interaction) -> list[discord.Embed]:
    """Force sub-panel — action summary + usage guide."""
    desc_e1 = (
        "⚠️ Direct balance manipulation — bypasses all economy logic.\n"
        "All force actions are permanent and recorded in the audit log.\n\n"
        "**➕ Award** — Adds points directly to the target's raw balance.\n"
        "**➖ Deduct** — Removes points. Fails gracefully if the user has no balance."
    )
    e1 = discord.Embed(title=_T_FORCE, description=desc_e1, color=WARNING)

    desc_e2 = (
        "**Target** — Enter a numeric User ID or paste a @mention into the modal.\n"
        "**Amount** — Must be a positive number. Decimals are supported (e.g. `12.5`).\n\n"
        "> To get a User ID: right-click or long-press the user → **Copy User ID**.\n"
        "> Deduct will not push a balance below zero — it fails with an error instead.\n"
        "> This section is restricted to **Moderators** and above."
    )
    e2 = discord.Embed(title=_T_FO_G, description=desc_e2, color=_C_WHITE)

    return [e1, e2]


# =====
# SHARED MODAL: AdminNumberModal
# FIX-ADM-014: Accepts optional current_value — displayed in modal placeholder.
# FIX-ADM-003: Error is ephemeral — panel remains usable after a failed submit.
# =====

class AdminNumberModal(discord.ui.Modal):
    value_input = discord.ui.TextInput(label="New Value", placeholder="Enter a number...")

    def __init__(self, key: str, title: str, placeholder: str, current_value: str | None = None):
        super().__init__(title=title[:45])
        self.cfg_key = key
        if current_value is not None:
            self.value_input.placeholder = f"Current: {current_value}  ·  {placeholder}"
        else:
            self.value_input.placeholder = placeholder

    async def on_submit(self, i: discord.Interaction):
        new_val = self.value_input.value.strip()
        try:
            await set_config(str(i.guild_id), self.cfg_key, new_val, str(i.user.id))
            await i.response.send_message(
                embed=success_embed(f"`{self.cfg_key}` updated to `{new_val}`."),
                ephemeral=True,
            )
        except Exception as exc:
            await i.response.send_message(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"),
                ephemeral=True,
            )


async def _open_number_modal(
    i: discord.Interaction,
    key: str,
    title: str,
    placeholder: str,
) -> None:
    """Fetches current value then opens the modal with an informative placeholder."""
    current = await get_config_or_none(str(i.guild_id), key)
    await i.response.send_modal(
        AdminNumberModal(key, title, placeholder, current_value=current)
    )


# =====
# ECONOMY SETTINGS VIEW
# =====

class AdminEconomyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Join Bonus", style=discord.ButtonStyle.secondary)
    async def join_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.join_bonus", "Join Bonus (pts)", "e.g. 15")

    @discord.ui.button(label="Completion Bonus", style=discord.ButtonStyle.secondary)
    async def comp_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.completion_bonus", "Completion Bonus (pts)", "e.g. 10")

    @discord.ui.button(label="Max Bonus", style=discord.ButtonStyle.secondary)
    async def max_bonus(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.base_max_bonus", "Max Bonus (pts)", "e.g. 50")

    @discord.ui.button(label="Time Cap (min)", style=discord.ButtonStyle.secondary)
    async def t_cap(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "ec.t_cap", "Time Cap (minutes)", "e.g. 120")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# DECAY SETTINGS VIEW
# FIX-ADM-009: Correctly labelled — was previously mislabelled "Tiers".
# =====

class AdminDecayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Grace Days", style=discord.ButtonStyle.secondary)
    async def grace(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.grace_days", "Grace Days", "Days before decay starts (e.g. 7)")

    @discord.ui.button(label="Zone 1 Days", style=discord.ButtonStyle.secondary)
    async def z1(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.zone1_days", "Zone 1 Days", "Slow decay zone duration (e.g. 7)")

    @discord.ui.button(label="Zone 2 Days", style=discord.ButtonStyle.secondary)
    async def z2(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.zone2_days", "Zone 2 Days", "Medium decay zone duration (e.g. 7)")

    @discord.ui.button(label="Rate Zone 1", style=discord.ButtonStyle.secondary, row=1)
    async def r1(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone1", "Rate Zone 1 (pts/day)", "e.g. 5.0")

    @discord.ui.button(label="Rate Zone 2", style=discord.ButtonStyle.secondary, row=1)
    async def r2(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone2", "Rate Zone 2 (pts/day)", "e.g. 15.0")

    @discord.ui.button(label="Rate Zone 3", style=discord.ButtonStyle.secondary, row=1)
    async def r3(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "decay.rate_zone3", "Rate Zone 3 (pts/day)", "e.g. 30.0")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# HOST SETTINGS VIEW
# FIX-ADM-011: Full UI coverage for host.* config keys.
# =====

class AdminHostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cooldown (hours)", style=discord.ButtonStyle.secondary)
    async def cooldown(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.cooldown_hours", "Host Cooldown (hours)", "e.g. 12")

    @discord.ui.button(label="Min Duration (min)", style=discord.ButtonStyle.secondary)
    async def min_dur(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.min_duration_minutes", "Min Duration (minutes)", "e.g. 45")

    @discord.ui.button(label="Min Voters", style=discord.ButtonStyle.secondary)
    async def min_voters(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.min_voters", "Minimum Voters", "e.g. 5")

    @discord.ui.button(label="Income Multiplier", style=discord.ButtonStyle.secondary)
    async def income_mult(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.income_multiplier", "Host Income Multiplier", "e.g. 2.0")

    @discord.ui.button(label="Rolling Window", style=discord.ButtonStyle.secondary, row=1)
    async def rolling(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.rolling_window", "Reputation Rolling Window (events)", "e.g. 10")

    @discord.ui.button(label="Outlier Trim", style=discord.ButtonStyle.secondary, row=1)
    async def outlier(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "host.outlier_trim_threshold", "Outlier Trim Threshold", "e.g. 8")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# VOTE SETTINGS VIEW
# FIX-ADM-011: Full UI coverage for vote.* config keys.
# =====

class AdminVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Vote Window (min)", style=discord.ButtonStyle.secondary)
    async def window(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.window_minutes", "Vote Window (minutes)", "e.g. 10")

    @discord.ui.button(label="Score Positive", style=discord.ButtonStyle.secondary)
    async def pos(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_positive", "Positive Vote Score", "e.g. 5")

    @discord.ui.button(label="Score Neutral", style=discord.ButtonStyle.secondary)
    async def neu(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_neutral", "Neutral Vote Score", "e.g. 3")

    @discord.ui.button(label="Score Negative", style=discord.ButtonStyle.secondary)
    async def neg(self, i: discord.Interaction, _: discord.ui.Button):
        await _open_number_modal(i, "vote.score_negative", "Negative Vote Score", "e.g. 1")

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# CHANNEL SETTINGS VIEW
# FIX-ADM-005: callback() defers before DB write to avoid timeouts.
# =====

class AdminChannelSel(discord.ui.ChannelSelect):
    def __init__(self, key: str, placeholder: str, ctype: list):
        self.cfg_key = key
        super().__init__(placeholder=placeholder, channel_types=ctype, min_values=1, max_values=1)

    async def callback(self, i: discord.Interaction):
        # FIX-ADM-005: Defer first before DB write.
        await i.response.defer()
        try:
            await set_config(str(i.guild_id), self.cfg_key, str(self.values[0].id), str(i.user.id))
            await i.edit_original_response(
                embed=success_embed(f"`{self.cfg_key}` updated to <#{self.values[0].id}>!"),
                view=self.view,
            )
        except Exception as exc:
            await i.edit_original_response(
                embed=error_embed(f"Failed to update `{self.cfg_key}`:\n`{exc}`"),
                view=self.view,
            )


class AdminChannelGroupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None
        self.add_item(AdminChannelSel("channel.gamenight_id", "🎮 Gamenight Channel...", [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.activity_id",  "📢 Activity Channel...",  [discord.ChannelType.text]))
        self.add_item(AdminChannelSel("channel.vc_category_id", "🔊 VC Category...",     [discord.ChannelType.category]))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# SYSTEM SETTINGS VIEW
# FIX-ADM-004: State changes require ConfirmSystemStateView before execution.
# FIX-ADM-006: set_active/set_paused defer before DB write.
# =====

class ConfirmSystemStateView(discord.ui.View):
    """Confirmation gate before committing a system state change."""

    def __init__(self, target_state: str):
        super().__init__(timeout=60)
        self.target_state = target_state
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="✅ Yes, Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, _: discord.ui.Button):
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
                embed=error_embed(f"Failed to change system state:\n`{exc}`"),
                view=None,
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.edit_message(
            embed=discord.Embed(description="State change cancelled.", color=PRIMARY),
            view=None,
        )


class AdminSystemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="▶️ Set ACTIVE", style=discord.ButtonStyle.success)
    async def set_active(self, i: discord.Interaction, _: discord.ui.Button):
        e = confirm_embed(
            "Confirmation: Set ACTIVE",
            "The bot will start accepting public commands again.\n"
            "Ensure all configurations are correct before activating.",
        )
        view = ConfirmSystemStateView("ACTIVE")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="⏸️ Set PAUSED", style=discord.ButtonStyle.secondary)
    async def set_paused(self, i: discord.Interaction, _: discord.ui.Button):
        e = confirm_embed(
            "Confirmation: Set PAUSED",
            "The bot will stop serving public commands (`/shop`, `/gamenight`, etc).\n"
            "Admin and owner commands will still work.",
            warning="Currently active users will not be able to start new events.",
        )
        view = ConfirmSystemStateView("PAUSED")
        await i.response.edit_message(embed=e, view=view)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# FORCE POINTS VIEW
# FIX-ADM-008: ID parsing fixed from lstrip (char-set) to re.sub (pattern).
# =====

class ForcePointsModal(discord.ui.Modal):
    user_input   = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789012345678 or @User")
    amount_input = discord.ui.TextInput(label="Point Amount",        placeholder="e.g. 100")

    def __init__(self, action: str):
        super().__init__(title=f"Force {'Award' if action == 'award' else 'Deduct'} Points")
        self.action = action

    async def on_submit(self, i: discord.Interaction):
        # FIX-ADM-008: re.sub strips mention syntax robustly.
        uid_raw = re.sub(r"[<@!>]", "", self.user_input.value.strip())
        try:
            amount = float(self.amount_input.value.strip())
            uid    = str(int(uid_raw))
        except ValueError:
            return await i.response.send_message(
                embed=error_embed(
                    "Invalid User ID or amount.\n"
                    "Enter a numeric User ID or a mention like `@Username`."
                ),
                ephemeral=True,
            )

        if amount <= 0:
            return await i.response.send_message(
                embed=error_embed("Amount must be greater than 0."),
                ephemeral=True,
            )

        gid = str(i.guild_id)
        if self.action == "award":
            await award(gid, uid, amount)
            await i.response.send_message(
                embed=success_embed(f"Awarded **{amount:.0f} pts** to <@{uid}>."),
                ephemeral=True,
            )
        else:
            ok = await deduct(gid, uid, amount)
            if ok:
                await i.response.send_message(
                    embed=success_embed(f"Deducted **{amount:.0f} pts** from <@{uid}>."),
                    ephemeral=True,
                )
            else:
                await i.response.send_message(
                    embed=error_embed(f"User <@{uid}> not found or has no points."),
                    ephemeral=True,
                )


class AdminForceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="➕ Award Points",  style=discord.ButtonStyle.success)
    async def award_pts(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(ForcePointsModal("award"))

    @discord.ui.button(label="➖ Deduct Points", style=discord.ButtonStyle.danger)
    async def deduct_pts(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(ForcePointsModal("deduct"))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _main_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminMainView())


# =====
# MAIN PANEL VIEW
# =====

class AdminMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # FIX-ADM-007: Edit Discord message on timeout so buttons don't ghost.
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="⚙️ Economy", style=discord.ButtonStyle.secondary, row=0)
    async def b1(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _economy_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminEconomyView())

    @discord.ui.button(label="⏱️ Decay", style=discord.ButtonStyle.secondary, row=0)
    async def b2(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _decay_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminDecayView())

    @discord.ui.button(label="🎮 Host", style=discord.ButtonStyle.secondary, row=0)
    async def b3(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _host_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminHostView())

    @discord.ui.button(label="🗳️ Vote", style=discord.ButtonStyle.secondary, row=0)
    async def b4(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _vote_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminVoteView())

    @discord.ui.button(label="📡 Channels", style=discord.ButtonStyle.secondary, row=1)
    async def b5(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _channel_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminChannelGroupView())

    @discord.ui.button(label="👁️ View All", style=discord.ButtonStyle.primary, row=1)
    async def b6(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        all_cfg = await get_all_config(str(i.guild_id))
        lines   = [f"{k}: {v}" for k, v in sorted(all_cfg.items())]
        txt     = "\n".join(lines)
        file    = discord.File(io.BytesIO(txt.encode()), filename="config_dump.txt")
        await i.followup.send(
            content=f"📄 Configuration dump — {len(lines)} keys:",
            file=file,
            ephemeral=True,
        )

    @discord.ui.button(label="🔧 System", style=discord.ButtonStyle.danger, row=1)
    async def b8(self, i: discord.Interaction, _: discord.ui.Button):
        embeds = await _system_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminSystemView())

    @discord.ui.button(label="⚡ Force", style=discord.ButtonStyle.danger, row=1)
    async def b9(self, i: discord.Interaction, _: discord.ui.Button):
        # FIX-ADM-012: Explicit guard check — does not rely on require_mod side-effect.
        if not await require_mod(i):
            return
        embeds = await _force_panel_embeds(i)
        await i.response.edit_message(embeds=embeds, view=AdminForceView())


# =====
# DISCORD COG MOUNTING
# =====

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="admin", description="Administrator control panel.")
    async def admin_cmd(self, i: discord.Interaction):
        if not await require_admin(i):
            return

        gid   = str(i.guild_id)
        state = await get_config_or_none(gid, "system.state")

        if not state or state == "UNCONFIGURED":
            # Auto-initialize with Balanced preset. Channels are configured
            # post-init via the 📡 Channels button.
            await i.response.defer(ephemeral=True)
            try:
                preset_data = dict(PRESETS["balanced"])
                preset_data["system.state"]      = "ACTIVE"
                preset_data["system.guild_name"] = i.guild.name
                await bulk_set_config(gid, preset_data, str(i.user.id))
                logger.info(f"[Admin] Guild {gid} auto-initialized with Balanced preset by {i.user.id}.")
            except Exception:
                logger.exception(f"[Admin] Auto-init failed for guild {gid}.")
                await i.followup.send(
                    embed=error_embed("Failed to initialize bot configuration. Check logs."),
                    ephemeral=True,
                )
                return

            welcome = discord.Embed(title="🌕  Welcome to Two Moon!", color=0x57F287)
            welcome.description = (
                "The bot has been initialized with the **Balanced** preset and is now **ACTIVE**.\n\n"
                "**Next steps:**\n"
                "• Set your channels via **📡 Channels** — required for events to function.\n"
                "• Configure access roles via `/owner` → **🔑 Admin Set**.\n"
                "• Fine-tune economy, decay, and host settings using the buttons below."
            )
            welcome.set_footer(text="All values can be changed anytime from this panel.")
            main_view = AdminMainView()
            await i.followup.send(embed=welcome, view=main_view, ephemeral=True)
            main_view.message = await i.original_response()
        else:
            embeds    = await _main_panel_embeds(i)
            main_view = AdminMainView()
            await i.response.send_message(embeds=embeds, view=main_view, ephemeral=True)
            # FIX-ADM-007: Store message reference for on_timeout.
            main_view.message = await i.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))