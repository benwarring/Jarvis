"""/event — builds an Intent and asks for a ✅. Zero business logic."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.bot.handlers import confirm
from Jarvis.config import get_config
from Jarvis.router.intents import Intent


@app_commands.command(name="event", description="Put something on the calendar.")
@app_commands.describe(
    title="What it is",
    when="When it starts, e.g. 'tomorrow 3pm'",
    duration="How many minutes it runs. Defaults to 60.",
)
async def event(
    interaction: discord.Interaction, title: str, when: str, duration: int | None = None
) -> None:
    # Deliberately `confirm`, not `respond`: a calendar write only runs after ✅.
    await confirm(
        interaction,
        Intent(name="calendar.create", args={"title": title, "when": when, "duration": duration}, source="slash"),
    )


def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(event, guild=discord.Object(id=get_config().discord_guild_id))
