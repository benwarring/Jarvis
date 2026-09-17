"""Presentation for the confirmation step, and nothing else.

plan.md section 3 puts this file in Phase 4 for exactly one reason: a calendar
write has to be shown to the user before it runs. The list renderers stay private
inside `agent/tools.py` — putting them here would make `agent/` import from
`bot/`, which is the section 3 arrow backwards.
"""

from __future__ import annotations

import discord

from Jarvis.agent.tools import DEFAULT_DURATION
from Jarvis.router.intents import Intent
from Jarvis.utils.dates import parse_when

CHECK = "\N{WHITE HEAVY CHECK MARK}"
CROSS = "\N{CROSS MARK}"


def _line(intent: Intent) -> str:
    """What one pending write will do, in one line."""
    args = intent.args
    if intent.name == "calendar.create":
        when = parse_when(args.get("when") or "")
        shown = f"{when:%a %b %d %I:%M%p}" if when else f"couldn't read a time out of '{args.get('when')}'"
        return f"**{args.get('title', '')}** - {shown}, {args.get('duration') or DEFAULT_DURATION} min"
    # ponytail: the intent name plus its arguments, not a hand-written phrasing per
    # tool - that would be a second name table to keep in step with TOOLS. Prettify
    # it when a line actually reads badly to the user.
    return f"**{intent.name}** - " + ", ".join(str(v) for v in args.values() if v is not None)


def confirmation_embed(intents: tuple[Intent, ...]) -> discord.Embed:
    """The ✅/❌ prompt shown before a write executes - one line per write.

    plan.md section 4 names two things that need it, a calendar write and a
    multi-item batch, and both render the same way: a single write is a batch of
    one, so there is no second layout to keep in step. All or nothing - one ✅
    runs every line shown here, a ❌ runs none of them.
    """
    embed = discord.Embed(
        title="Do this?" if len(intents) == 1 else f"Do all {len(intents)}?",
        description="\n".join(_line(i) for i in intents),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text=f"{CHECK} confirm  ·  {CROSS} cancel")
    return embed
