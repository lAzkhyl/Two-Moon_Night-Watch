# =====
# MODULE: cogs/public.py
# =====
# Architecture Overview:
# This file houses all the user-facing slash commands that regular 
# non-administrative members use to interact with the bot.
#
# Features include checking personal stats (both as an event Participant 
# and as a Host) and rendering the global Leaderboards.
#
# Performance Note:
# The leaderboard command uses "LIMIT 1000" in its PostgreSQL queries. 
# This is a critical memory optimization to prevent the bot from crashing 
# if a server scales up to tens of thousands of users.
# =====
import discord
from discord import app_commands
from discord.ext import commands

from db.pool import get_pool
from economy.points import get_effective_points
from economy.host import compute_host_reputation, get_host_tier
from config.manager import get_all_config, get_config_or_none
from utils.paginator import Paginator, build_pages
from utils.embeds import PRIMARY
from guards.checks import require_active


class LeaderboardSelect(discord.ui.Select):
    # -----
    # A dropdown menu allowing users to toggle between viewing the top
    # Participants (by raw points) and the top Hosts (by reputation stars).
    # -----
    def __init__(self, bot):
        opts = [
            discord.SelectOption(label="Participants", description="Ranked by points", value="part"),
            discord.SelectOption(label="Hosts", description="Ranked by reputation", value="host"),
        ]
        super().__init__(placeholder="Select leaderboard...", options=opts)
        self.bot = bot

    async def callback(self, i: discord.Interaction):
        gid = str(i.guild_id)
        pool = await get_pool()
        
        if self.values[0] == "part":
            # -----
            # Participant Leaderboard
            # We fetch up to 1000 users. Before displaying, we apply the local
            # server's 'decay' formula to show their real, effective points 
            # rather than their raw historical points.
            # -----
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT discord_id, raw_points, last_active_at FROM users WHERE guild_id=$1 ORDER BY raw_points DESC LIMIT 1000", gid)
            cfg = await get_all_config(gid)
            
            # Local import to prevent circular dependencies at boot
            from economy.points import calculate_decay
            
            lst = []
            for r in rows:
                ep = calculate_decay(r["raw_points"], r["last_active_at"], cfg)
                lst.append((r["discord_id"], ep))
                
            # Re-sort natively after decay is applied
            lst.sort(key=lambda x: x[1], reverse=True)
            lines = [f"**{idx+1}.** <@{u}> — {pts:.0f} pts" for idx, (u, pts) in enumerate(lst[:100])]
            
            if not lines: 
                lines = ["No participants yet."]
                
            pages = build_pages("🏆 Participant Leaderboard", lines, 10, PRIMARY)
            
        else:
            # -----
            # Host Leaderboard
            # Fetches the recent event ratings for hosts based on the 
            # sliding window configuration (e.g., last 10 games).
            # -----
            config = await get_all_config(gid)
            win = int(config.get("host.rolling_window", 10))
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT host_id, AVG(rating_score) as avg_rep
                    FROM (
                        SELECT e.host_id, r.rating_score,
                               ROW_NUMBER() OVER(PARTITION BY e.host_id ORDER BY e.ended_at DESC) as rn
                        FROM events e JOIN event_ratings r ON e.event_id=r.event_id
                        WHERE e.guild_id=$1 AND e.is_valid=1
                    ) sub
                    WHERE rn <= $2
                    GROUP BY host_id
                    ORDER BY avg_rep DESC
                    """,
                    gid, win
                )
            lines = [f"**{idx+1}.** <@{r['host_id']}> — ⭐ {float(r['avg_rep']):.2f}" for idx, r in enumerate(rows[:100])]
            
            if not lines: 
                lines = ["No hosts yet."]
                
            pages = build_pages("🎖️ Host Leaderboard", lines, 10, PRIMARY)

        await i.response.edit_message(embed=pages[0], view=Paginator(pages, owner_id=i.user.id))


class LeaderboardView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.add_item(LeaderboardSelect(bot))


class PublicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="stats", description="View your profile statistics or someone else's.")
    async def stats_cmd(self, i: discord.Interaction, user: discord.Member = None):
        if not await require_active(i): return
        
        target = user or i.user
        gid = str(i.guild_id)
        uid = str(target.id)
        
        # Gathering metrics concurrently
        ep = await get_effective_points(gid, uid)
        pool = await get_pool()
        rep = await compute_host_reputation(pool, gid, uid)
        
        t_defs = await get_config_or_none(gid, "host.tier_definitions")
        htier = await get_host_tier(rep, t_defs or "[]")
        
        async with pool.acquire() as conn:
            inv = await conn.fetch("SELECT item_id, expires_at FROM user_inventory WHERE guild_id=$1 AND user_id=$2", gid, uid)
            
        inv_lines = [f"• {r['item_id']} (expires: {str(r['expires_at'])[:10] if r['expires_at'] else 'Never'})" for r in inv]
        if not inv_lines: 
            inv_lines = ["No items owned."]
        
        e = discord.Embed(title=f"📊 Stats — {target.display_name}", color=PRIMARY)
        e.add_field(name="Participant", value=f"Points: **{ep:.0f} pts**", inline=True)
        e.add_field(name="Host", value=f"Reputation: **⭐ {rep:.2f}**\nTier: **{htier['label'] if htier else 'Unrated'}**", inline=True)
        e.add_field(name="Inventory", value="\n".join(inv_lines), inline=False)
        e.set_thumbnail(url=target.display_avatar.url)
        
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="mypoints", description="Live view of your current points.")
    async def mypoints_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        await i.response.send_message("Join an active event to see a live preview. (Currently under construction)", ephemeral=True)

    @app_commands.command(name="leaderboard", description="View the server leaderboards.")
    async def leaderboard_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        e = discord.Embed(title="🏆 Select Leaderboard", description="Choose a category from the dropdown below.", color=PRIMARY)
        await i.response.send_message(embed=e, view=LeaderboardView(self.bot), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PublicCog(bot))
