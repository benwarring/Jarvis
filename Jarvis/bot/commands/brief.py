"""/brief — builds an Intent and dispatches. Zero business logic."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.bot.handlers import respond
from Jarvis.config import get_config
from Jarvis.router.intents import Intent


@app_commands.command(name="brief", description="The day's brief: calendar, tasks, and free time.")
@app_commands.describe(day="Which day, e.g. 'tomorrow' or 'friday'. Defaults to today.")
async def brief(interaction: discord.Interaction, day: str | None = None) -> None:
    # A read, so it runs immediately — only writes wait for a ✅. Deliberately NOT
    # gated by the `briefs` claim either: that guard is there to stop the 07:00 job
    # posting twice, and someone typing /brief is asking for it on purpose.
    await respond(interaction, Intent(name="brief.read", args={"day": day}, source="slash"))


def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(brief, guild=discord.Object(id=get_config().discord_guild_id))
