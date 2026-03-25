# =====
# MODULE: utils/embeds.py
# =====
# Architecture Overview:
# A factory module for standardizing Discord embeds. This ensures consistent 
# branding, hex colors, and identical success/error UX patterns across all commands.
# =====

import discord

PRIMARY = 0xF0C040
SUCCESS = 0x57F287
ERROR   = 0xED4245
WARNING = 0xFEE75C
INFO    = 0x5865F2


def panel_embed(title: str, color: int = PRIMARY) -> discord.Embed:
    # -----
    # Generates a standard blank panel embed.
    # -----
    return discord.Embed(title=title, color=color)


def error_embed(msg: str) -> discord.Embed:
    # -----
    # Generates a standard error card with a red hex code and an X icon.
    # -----
    return discord.Embed(description=f"❌  {msg}", color=ERROR)


def success_embed(msg: str) -> discord.Embed:
    # -----
    # Generates a standard success card with a green hex code and a check icon.
    # -----
    return discord.Embed(description=f"✅  {msg}", color=SUCCESS)


def confirm_embed(title: str, body: str, warning: str | None = None) -> discord.Embed:
    # -----
    # Generates a warning card with an optional severe note field.
    # Typically used in destructive Admin/Owner workflows.
    # -----
    e = discord.Embed(title=f"⚠️  {title}", description=body, color=WARNING)
    if warning:
        e.add_field(name="Note", value=warning, inline=False)
    return e