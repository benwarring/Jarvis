"""/grocery — builds an Intent and dispatches. Zero business logic."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.bot.handlers import respond
from Jarvis.config import get_config
from Jarvis.router.intents import Intent

grocery = app_commands.Group(name="grocery", description="Your grocery list.")


@grocery.command(name="add", description="Add an item.")
@app_commands.describe(item="What to buy", qty="How much, e.g. '2 lbs'")
async def add(interaction: discord.Interaction, item: str, qty: str | None = None) -> None:
    await respond(interaction, Intent(name="grocery.add", args={"item": item, "qty": qty}, source="slash"))


@grocery.command(name="list", description="Show what's still on the list.")
async def list_items(interaction: discord.Interaction) -> None:
    await respond(interaction, Intent(name="grocery.list", args={}, source="slash"))


@grocery.command(name="got", description="Check an item off.")
@app_commands.describe(query="Part of the item's name")
async def got(interaction: discord.Interaction, query: str) -> None:
    await respond(interaction, Intent(name="grocery.check", args={"query": query}, source="slash"))


def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(grocery, guild=discord.Object(id=get_config().discord_guild_id))
