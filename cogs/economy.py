# =====
# MODULE: cogs/economy.py
# =====
# Architecture Overview:
# This file processes transactions where users spend their accumulated economy
# points. It interacts directly with the 'shop_items' and 'user_inventory'
# tables.
#
# Developer Note:
# Database mutations here are wrapped in careful point-balance verifications 
# to prevent users from buying items they cannot afford or causing negative
# balances. Be very cautious when modifying SQL updates here.
# =====
import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

from db.pool import get_pool
from economy.points import get_effective_points, deduct, award, calculate_decay
from config.manager import get_config_or_none, get_all_config
from utils.embeds import PRIMARY, error_embed, success_embed
from guards.checks import require_active

logger = logging.getLogger(__name__)


class ShopItemSelect(discord.ui.Select):
    # -----
    # The dropdown menu that handles actual purchasing logic for the shop.
    # It receives a pre-fetched list of active items from the database.
    # -----
    def __init__(self, items):
        opts = [discord.SelectOption(
            label=r['label'], 
            description=f"{r['cost']} pts - {str(r['description'])[:50]}", 
            value=r['item_id']
        ) for r in items[:25]]  # Discord dropdowns support a maximum of 25 options
        
        super().__init__(placeholder="Select an item to buy...", options=opts)
        self.items = items

    async def callback(self, i: discord.Interaction):
        item_id = self.values[0]
        item = next((x for x in self.items if x["item_id"] == item_id), None)
        if not item: 
            return await i.response.send_message(embed=error_embed("Item not found."), ephemeral=True)
        
        gid = str(i.guild_id)
        uid = str(i.user.id)
        
        # CVE-2M-005: Atomic purchase transaction with SELECT FOR UPDATE
        # CVE-2M-007: Stock management within the same transaction
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Lock the user row to prevent concurrent purchase race conditions
                user_row = await conn.fetchrow(
                    "SELECT raw_points, last_active_at FROM users "
                    "WHERE guild_id=$1 AND discord_id=$2 FOR UPDATE",
                    gid, uid
                )
                if not user_row:
                    return await i.response.send_message(
                        embed=error_embed("You have no points yet."), ephemeral=True
                    )

                # Calculate effective points with decay within the locked transaction
                config = await get_all_config(gid)
                effective = calculate_decay(
                    float(user_row["raw_points"]),
                    user_row["last_active_at"],
                    config
                )

                if effective < item["cost"]:
                    return await i.response.send_message(
                        embed=error_embed(f"Not enough points. You need {item['cost']} pts (You have {effective:.0f})."), 
                        ephemeral=True
                    )

                # CVE-2M-007: Lock and check stock before purchase
                item_row = await conn.fetchrow(
                    "SELECT stock FROM shop_items WHERE item_id=$1 AND guild_id=$2 FOR UPDATE",
                    item_id, gid
                )
                if item_row and item_row["stock"] is not None and item_row["stock"] <= 0:
                    return await i.response.send_message(
                        embed=error_embed("This item is out of stock."), ephemeral=True
                    )

                # Deduct points atomically
                await conn.execute(
                    "UPDATE users SET raw_points = GREATEST(0, raw_points - $3) "
                    "WHERE guild_id=$1 AND discord_id=$2",
                    gid, uid, float(item["cost"])
                )

                # CVE-2M-007: Reduce stock if applicable
                if item_row and item_row["stock"] is not None:
                    await conn.execute(
                        "UPDATE shop_items SET stock = stock - 1 WHERE item_id=$1 AND guild_id=$2",
                        item_id, gid
                    )

                # Insert into user inventory
                dur = timedelta(days=item["duration_days"]) if item["duration_days"] else None
                if dur:
                    await conn.execute(
                        """
                        INSERT INTO user_inventory (guild_id, user_id, item_id, expires_at)
                        VALUES ($1, $2, $3, NOW() + $4::interval)
                        ON CONFLICT (guild_id, user_id, item_id) 
                        DO UPDATE SET expires_at = user_inventory.expires_at + $4::interval
                        """,
                        gid, uid, item_id, dur
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO user_inventory (guild_id, user_id, item_id, expires_at)
                        VALUES ($1, $2, $3, NULL)
                        ON CONFLICT (guild_id, user_id, item_id) DO NOTHING
                        """,
                        gid, uid, item_id
                    )

        # Handle unique logic for roles
        # CVE-2M-001: Fixed column name discord_role_id → role_id
        if item["item_type"] == "role_rental" and item.get("role_id"):
            role = i.guild.get_role(int(item["role_id"]))
            if role:
                try:
                    await i.user.add_roles(role)
                except discord.HTTPException:
                    pass
                    
        await i.response.send_message(
            embed=success_embed(f"Successfully purchased **{item['label']}** for {item['cost']} pts!"), 
            ephemeral=True
        )


class ShopMainView(discord.ui.View):
    def __init__(self, items):
        super().__init__(timeout=120)
        self.add_item(ShopItemSelect(items))


class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="shop", description="Open the Point Shop")
    async def shop_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            items = await conn.fetch("SELECT * FROM shop_items WHERE guild_id=$1 AND is_active=TRUE", str(i.guild_id))
            
        if not items:
            return await i.response.send_message("🛍️ The shop is currently empty.", ephemeral=True)
            
        lines = [f"**{r['label']}** — {r['cost']} pts\n_{r['description'] or 'No desc'}_" for r in items]
        e = discord.Embed(title="🛒 Two Moon Shop", description="\n\n".join(lines), color=PRIMARY)
        await i.response.send_message(embed=e, view=ShopMainView(items), ephemeral=True)

    @app_commands.command(name="bet", description="Place a bet on an active event.")
    async def bet_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        # Betting system has not been fully implemented yet.
        await i.response.send_message("Betting pools are currently closed.", ephemeral=True)

    @app_commands.command(name="bounty", description="Manage bounties.")
    async def bounty_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        # Bounty board system has not been fully implemented yet.
        await i.response.send_message("Bounty board is under construction.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))