# =====
# MODULE: cogs/gamenight.py
# =====
# Architecture Overview:
# This module drives the core 'Gamenight' event loop. It allows authorized 
# hosts to start events, which dynamically creates Discord Voice Channels and 
# text threads. It handles user voting at the end of events and distributes
# economy points to participants based on voice channel activity.
#
# Developer Note:
# The `vote_resolver` is an asynchronous background loop. Instead of hanging 
# a sleep() timer in memory when an event ends, the event is marked 'ended' 
# in PostgreSQL. The loop constantly checks the database for ended events 
# whose voting window has expired, making it resilient to bot restarts.
# =====
import asyncio
import logging
import secrets
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db.pool import get_pool
from config.manager import get_config_or_none, get_all_config
from economy.host import compute_event_rating, compute_host_reputation, get_host_tier, award_host_points, update_elite_board
from economy.points import distribute_event_points
from utils.embeds import PRIMARY, WARNING, error_embed, success_embed
from guards.checks import require_active

logger = logging.getLogger(__name__)


def gen_id():
    # -----
    # Generates a short, readable 8-character hex ID (e.g., A1B2C3D4) used
    # as the primary key for Gamenight Events.
    # -----
    return secrets.token_hex(4).upper()


class VoteView(discord.ui.View):
    # -----
    # The interactive voting panel sent to the thread when an event concludes.
    # Timeout is set to None so the buttons remain clickable permanently, while  
    # validation logic prevents late votes.
    # -----
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id

    async def _handle_vote(self, i: discord.Interaction, val: int):
        # CVE-2M-006: Wrap vote in transaction with ON CONFLICT to prevent race conditions
        uid = str(i.user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                evt = await conn.fetchrow(
                    "SELECT is_valid FROM events WHERE event_id=$1 FOR UPDATE",
                    self.event_id
                )
                if not evt or evt["is_valid"] != 0:
                    return await i.response.send_message("Voting is closed for this event.", ephemeral=True)

                result = await conn.execute(
                    "INSERT INTO votes (event_id, voter_id, vote_value) VALUES ($1, $2, $3) "
                    "ON CONFLICT (event_id, voter_id) DO NOTHING",
                    self.event_id, uid, int(val)
                )
                if result == "INSERT 0 0":
                    return await i.response.send_message("You have already voted!", ephemeral=True)

        await i.response.send_message("Vote recorded. Thank you!", ephemeral=True)

    @discord.ui.button(label="👍 Good", style=discord.ButtonStyle.success)
    async def b1(self, i: discord.Interaction, _: discord.ui.Button):
        await self._handle_vote(i, 5)

    @discord.ui.button(label="😐 Neutral", style=discord.ButtonStyle.secondary)
    async def b2(self, i: discord.Interaction, _: discord.ui.Button):
        await self._handle_vote(i, 3)

    @discord.ui.button(label="👎 Bad", style=discord.ButtonStyle.danger)
    async def b3(self, i: discord.Interaction, _: discord.ui.Button):
        await self._handle_vote(i, 1)


class StartEventModal(discord.ui.Modal, title="Start Event"):
    # Pops up a form prompting the host to define the event namespace.
    title_inp = discord.ui.TextInput(label="Event Title", max_length=100)
    game_inp = discord.ui.TextInput(label="Game Name", max_length=100)

    def __init__(self, view: "GameNightMainView"):
        super().__init__()
        self.parent_view = view

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        title = self.title_inp.value.strip()
        game = self.game_inp.value.strip()
        gid = str(i.guild_id)
        host_id = str(i.user.id)
        
        ctg_id = await get_config_or_none(gid, "channel.vc_category_id")
        txt_id = await get_config_or_none(gid, "channel.gamenight_id")
        
        if not ctg_id or not txt_id:
            return await i.followup.send("Gamenight channels not fully configured.", ephemeral=True)
            
        tg = i.guild.get_channel(int(txt_id))
        cg = i.guild.get_channel(int(ctg_id))
        
        eid = gen_id()
        pool = await get_pool()
        async with pool.acquire() as conn:
            # CVE-2M-003: Auto-upsert user before event creation to satisfy FK
            await conn.execute(
                "INSERT INTO users (discord_id, guild_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                host_id, gid
            )
            # CVE-2M-001: Fixed column name game_name → game
            await conn.execute(
                """
                INSERT INTO events (event_id, guild_id, host_id, title, game, started_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                """,
                eid, gid, host_id, title, game
            )
        
        # CVE-2M-023: Replace bare except with proper logging
        try:
            vc = await i.guild.create_voice_channel(name=f"🎮 {title}", category=cg)
            th = await tg.create_thread(name=f"{title} - {game}", type=discord.ChannelType.public_thread)
            async with pool.acquire() as conn:
                # CVE-2M-001: Fixed column names discord_vc_id → vc_id, discord_thread_id → thread_id
                await conn.execute("UPDATE events SET vc_id=$1, thread_id=$2 WHERE event_id=$3", str(vc.id), str(th.id), eid)
        except discord.HTTPException as e:
            logger.error(f"Failed to create VC/thread for event {eid}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating VC/thread for event {eid}: {e}")
            
        await i.followup.send(f"Event `{eid}` started! VC created.", ephemeral=True)


class GameNightMainView(discord.ui.View):
    # The persistent dashboard allowing users to manage their host status.
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="▶️ Start Event", style=discord.ButtonStyle.primary)
    async def btn_start(self, i: discord.Interaction, _: discord.ui.Button):
        gid = str(i.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            act = await conn.fetchrow(
                "SELECT event_id FROM events WHERE guild_id=$1 AND host_id=$2 AND ended_at IS NULL", 
                gid, str(i.user.id)
            )
        if act:
            return await i.response.send_message(f"You already have an active event: `{act['event_id']}`. End it first.", ephemeral=True)

        # CVE-2M-009: Host cooldown enforcement
        async with pool.acquire() as conn:
            last_event = await conn.fetchrow(
                "SELECT ended_at FROM events WHERE guild_id=$1 AND host_id=$2 "
                "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
                gid, str(i.user.id)
            )
        cooldown_h = int(await get_config_or_none(gid, "host.cooldown_hours") or 12)
        if last_event and last_event["ended_at"]:
            ended = last_event["ended_at"]
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - ended).total_seconds() / 3600
            if elapsed < cooldown_h:
                remaining = cooldown_h - elapsed
                return await i.response.send_message(
                    f"Cooldown active. Try again in {remaining:.1f} hours.", ephemeral=True
                )

        await i.response.send_modal(StartEventModal(self))

    @discord.ui.button(label="⏹️ End Event", style=discord.ButtonStyle.danger)
    async def btn_end(self, i: discord.Interaction, _: discord.ui.Button):
        gid = str(i.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            evt = await conn.fetchrow(
                "SELECT * FROM events WHERE guild_id=$1 AND host_id=$2 AND ended_at IS NULL", 
                gid, str(i.user.id)
            )
        if not evt:
            return await i.response.send_message("You don't have an active event.", ephemeral=True)
        
        eid = evt["event_id"]
        async with pool.acquire() as conn:
            await conn.execute("UPDATE events SET ended_at=NOW() WHERE event_id=$1", eid)
            # Force-close any open voice chat sessions to finalize point tallies.
            await conn.execute("UPDATE vc_sessions SET leave_time=NOW() WHERE event_id=$1 AND leave_time IS NULL", eid)
            
        await i.response.send_message(f"Event `{eid}` ended. Vote window opening in the thread...", ephemeral=True)
        
        # CVE-2M-001: Fixed column names discord_thread_id → thread_id, game_name → game
        tid = evt.get("thread_id")
        if tid:
            th = i.guild.get_thread(int(tid))
            if th:
                vv = VoteView(eid)
                e = discord.Embed(title="Event Ended!", description=f"Please rate **{evt['game']}** hosted by <@{evt['host_id']}>.", color=PRIMARY)
                await th.send(embed=e, view=vv)

        # Destroys the temporary Voice Channel.
        # CVE-2M-001: Fixed column name discord_vc_id → vc_id
        vid = evt.get("vc_id")
        if vid:
            vc = i.guild.get_channel(int(vid))
            if vc:
                try:
                    await vc.delete()
                except discord.HTTPException as e:
                    logger.error(f"Failed to delete VC for event {eid}: {e}")

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary)
    async def btn_stats(self, i: discord.Interaction, _: discord.ui.Button):
        gid = str(i.guild_id)
        uid = str(i.user.id)
        pool = await get_pool()
        rep = await compute_host_reputation(pool, gid, uid)
        
        t_defs = await get_config_or_none(gid, "host.tier_definitions")
        tier = await get_host_tier(rep, t_defs or "[]")
        t_lbl = tier["label"] if tier else "Unrated"
        
        e = discord.Embed(title="Host Stats", description=f"Reputation: **⭐ {rep:.2f}**\nTier: **{t_lbl}**", color=PRIMARY)
        await i.response.send_message(embed=e, ephemeral=True)


class GamenightCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.vote_resolver.start()
        self.afk_checker.start()

    def cog_unload(self):
        self.vote_resolver.cancel()
        self.afk_checker.cancel()

    @tasks.loop(minutes=1)
    async def vote_resolver(self):
        # -----
        # CVE-2M-004: Leader gate — only the LEADER instance processes events
        # -----
        if not getattr(self.bot, '_was_leader', False):
            return

        # -----
        # Resolves events that have ended but haven't received point payouts yet.
        # It waits until the 'vote.window_minutes' configuration passes, then
        # tallies the votes, awards economy points, and marks the event 'is_valid'.
        # -----
        pool = await get_pool()
        async with pool.acquire() as conn:
            # CVE-2M-001: Fixed column name discord_thread_id → thread_id
            evts = await conn.fetch("SELECT event_id, guild_id, ended_at, thread_id FROM events WHERE ended_at IS NOT NULL AND is_valid=0")
            
        now = datetime.now(timezone.utc)
        for e in evts:
            gid = e["guild_id"]
            win_m = int(await get_config_or_none(gid, "vote.window_minutes") or 10)
            
            end_t = e["ended_at"]
            if end_t.tzinfo is None:
                end_t = end_t.replace(tzinfo=timezone.utc)
                
            elapsed = (now - end_t).total_seconds() / 60.0
            if elapsed >= win_m:
                eid = e["event_id"]
                rating = await compute_event_rating(pool, eid)

                # CVE-2M-004: If not enough voters, mark as invalid and skip payouts
                if rating is None:
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE events SET is_valid=-1 WHERE event_id=$1", eid)
                else:
                    await distribute_event_points(eid)
                    await award_host_points(pool, eid)
                    await update_elite_board(self.bot, pool, gid)
                
                tid = e["thread_id"]
                if tid:
                    guild = self.bot.get_guild(int(gid))
                    if guild:
                        th = guild.get_thread(int(tid))
                        if th:
                            msg = f"Voting closed! Event rating: **{rating:.2f}**" if rating else "Voting closed! Not enough votes to rate."
                            try:
                                await th.send(msg)
                            except discord.HTTPException as ex:
                                logger.error(f"Failed to send vote result to thread {tid}: {ex}")

    @vote_resolver.before_loop
    async def before_vote(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def afk_checker(self):
        # CVE-2M-004: Leader gate for background tasks
        if not getattr(self.bot, '_was_leader', False):
            return
        # Placeholder for dynamic thread checking to validate AFK participation.
        pass

    @afk_checker.before_loop
    async def before_afk(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        # -----
        # Startup Re-conciliation
        # If the bot crashed during an active event, some voice connection sessions 
        # might be 'left hanging' with a null leave_time. This block closes them 
        # to prevent users from racking up infinite points while the bot was offline.
        pool = await get_pool()
        async with pool.acquire() as conn:
            open_sessions = await conn.fetch("SELECT event_id, user_id FROM vc_sessions WHERE leave_time IS NULL")
            if not open_sessions:
                pass # Handled below
            else:
                for s in open_sessions:
                    # CVE-2M-001: Fixed column name discord_vc_id → vc_id
                    evt = await conn.fetchrow("SELECT vc_id, guild_id FROM events WHERE event_id=$1", s["event_id"])
                    if not evt or not evt["vc_id"]:
                        await conn.execute("UPDATE vc_sessions SET leave_time=NOW() WHERE event_id=$1 AND user_id=$2 AND leave_time IS NULL", s["event_id"], s["user_id"])
                        continue
                    guild = self.bot.get_guild(int(evt["guild_id"]))
                    if not guild: continue
                    vc = guild.get_channel(int(evt["vc_id"]))
                    member = guild.get_member(int(s["user_id"]))
                    
                    # If they aren't physically in the channel right now, close their session.
                    if not vc or not member or not member.voice or member.voice.channel != vc:
                        await conn.execute("UPDATE vc_sessions SET leave_time=NOW() WHERE event_id=$1 AND user_id=$2 AND leave_time IS NULL", s["event_id"], s["user_id"])

        # -----
        # Bug #3 Fix: Re-register persistent VoteViews for events that ended
        # but haven't been resolved yet
        # -----
        async with pool.acquire() as conn:
            pending_votes = await conn.fetch("SELECT event_id FROM events WHERE is_valid=0 AND ended_at IS NOT NULL AND thread_id IS NOT NULL")
            for row in pending_votes:
                self.bot.add_view(VoteView(row["event_id"]))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # -----
        # Injects and terminates records into 'vc_sessions' seamlessly when users  
        # join or leave tracked event Voice Channels.
        # -----
        if member.bot: return
        
        pool = await get_pool()
        uid = str(member.id)
        
        # Joined a VC
        # CVE-2M-001: Fixed column name discord_vc_id → vc_id
        if after.channel is not None and (before.channel != after.channel):
            async with pool.acquire() as conn:
                evt = await conn.fetchrow("SELECT event_id FROM events WHERE vc_id=$1 AND ended_at IS NULL", str(after.channel.id))
            if evt:
                async with pool.acquire() as conn:
                    # CVE-2M-014: ON CONFLICT DO NOTHING prevents duplicate open sessions
                    await conn.execute(
                        "INSERT INTO vc_sessions (event_id, user_id, join_time) VALUES ($1, $2, NOW()) ON CONFLICT DO NOTHING",
                        evt["event_id"], uid
                    )
                    
        # Left a VC
        # CVE-2M-001: Fixed column name discord_vc_id → vc_id
        if before.channel is not None and (before.channel != after.channel):
            async with pool.acquire() as conn:
                evt = await conn.fetchrow("SELECT event_id FROM events WHERE vc_id=$1 AND ended_at IS NULL", str(before.channel.id))
            if evt:
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE vc_sessions SET leave_time=NOW() WHERE event_id=$1 AND user_id=$2 AND leave_time IS NULL", evt["event_id"], uid)

    @app_commands.command(name="gamenight", description="Gamenight Host Dashboard")
    async def gamenight_cmd(self, i: discord.Interaction):
        if not await require_active(i): return
        e = discord.Embed(title="🎮 Gamenight Dashboard", description="Manage your gamenight sessions.", color=PRIMARY)
        await i.response.send_message(embed=e, view=GameNightMainView(self.bot), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(GamenightCog(bot))
