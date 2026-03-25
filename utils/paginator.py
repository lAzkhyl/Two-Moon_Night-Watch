# =====
# MODULE: utils/paginator.py
# =====
# Architecture Overview:
# Handles logical memory chunking for large payload lists (like leaderboards)
# to prevent Discord's embed character limit from shattering the message.
# It automatically generates interactive UI buttons to navigate between chunks.
# =====

import discord


class Paginator(discord.ui.View):
    # -----
    # A persistent, interactive Next/Prev button panel view that cycles 
    # through a pre-generated array of Discord Embed chunks.
    # CVE-2M-026: Added owner_id check so only the command invoker can navigate.
    # -----
    def __init__(self, pages: list[discord.Embed], timeout: int = 120, owner_id: int | None = None):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.page  = 0
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # Prevent ghost integrations after the timeout by disabling the buttons natively
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _update_buttons(self):
        self.prev_btn.disabled    = self.page == 0
        self.next_btn.disabled    = self.page >= len(self.pages) - 1
        self.counter_btn.label    = f"{self.page + 1} / {len(self.pages)}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        self.page = min(len(self.pages) - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)


def build_pages(
    title: str,
    items: list[str],
    per_page: int = 10,
    color: int = 0xF0C040,
) -> list[discord.Embed]:
    # -----
    # Ingests a raw list of strings, slices them by 'per_page', and recursively
    # transforms them into an array of Embeds ready to be supplied to the Paginator.
    # -----
    pages: list[discord.Embed] = []
    chunks = [items[i : i + per_page] for i in range(0, max(len(items), 1), per_page)]
    total  = len(chunks)
    
    for idx, chunk in enumerate(chunks):
        e = discord.Embed(title=title, description="\n".join(chunk), color=color)
        e.set_footer(text=f"Page {idx + 1} of {total}")
        pages.append(e)
        
    return pages