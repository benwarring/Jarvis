"""/agenda — builds an Intent and dispatches. Zero business logic."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.bot.handlers import respond
from Jarvis.config import get_config
from Jarvis.router.intents import Intent


@app_commands.command(name="agenda", description="What's on the calendar.")
@app_commands.describe(day="Which day, e.g. 'tomorrow' or 'friday'. Defaults to today.")
async def agenda(interaction: discord.Interaction, day: str | None = None) -> None:
    # A read, so it runs immediately — only writes wait for a ✅.
    await respond(interaction, Intent(name="calendar.agenda", args={"day": day}, source="slash"))


def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(agenda, guild=discord.Object(id=get_config().discord_guild_id))
