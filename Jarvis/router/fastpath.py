"""Layer 1: deterministic regex matching. Free, and it should catch most daily traffic.

Patterns come from plan.md section 4, limited to the intents that exist so far
(weather.read is a later phase).

READ-ONLY. This layer may never emit calendar.create - a regex is not allowed to book
time on a real calendar. Event creation is slash-command only, and then behind a check.
"""

from __future__ import annotations

import re

from Jarvis.router.intents import Intent

# --- list -------------------------------------------------------------------
_LEAD = r"(?:(?:what'?s|whats)\s+on\s+|show\s+(?:me\s+)?|list\s+)?(?:the\s+|my\s+)?"
_GROCERY_LIST = re.compile(_LEAD + r"(?:grocery|groceries|shopping)(?:\s+list)?\s*\??$")
_TASK_LIST = re.compile(_LEAD + r"(?:to-?\s*do|task)s?(?:\s+list)?\s*\??$")

# Day words parse_when understands. Shared by the task due-date split and the agenda tail.
_DAY_TOKENS = (
    r"today|tonight|tomorrow"
    r"|next\s+\w+"
    r"|(?:mon|tues?|wed(?:nes)?|thur?s?|fri|satur?|sun)(?:day)?"
    r"|\d{4}-\d{2}-\d{2}"
)

# --- agenda (read only) -----------------------------------------------------
_AGENDA = re.compile(
    r"^(?:"
    r"(?:what'?s|whats)\s+on\s+(?:my|the)\s+(?:plate|calendar|agenda|schedule)"
    r"|what\s+(?:do\s+i\s+have|have\s+i\s+got|am\s+i\s+doing)"
    r"|(?:show|list)(?:\s+me)?(?:\s+my)?\s+(?:agenda|calendar|schedule)"
    r"|(?:my\s+)?(?:agenda|calendar|schedule)"
    r")"
    r"(?:\s+(?:for|on))?"
    r"(?:\s+(?P<day>" + _DAY_TOKENS + r"))?"
    r"\s*\??$"
)

# --- add --------------------------------------------------------------------
_GROCERY_ADD = (
    re.compile(r"^add\s+(?P<item>.+?)\s+to\s+(?:the\s+|my\s+)?(?:grocery|groceries|shopping)(?:\s+list)?\s*$"),
    re.compile(r"^(?:buy|pick\s+up)\s+(?P<item>.+?)\s*$"),
    re.compile(r"^(?:we|i)\s+need\s+(?:to\s+buy\s+)?(?!to\s)(?P<item>.+?)\s*$"),
)
_TASK_ADD = (
    re.compile(r"^remind\s+me\s+to\s+(?P<name>.+?)\s*$"),
    re.compile(r"^to-?\s*do\s*[:\-]\s*(?P<name>.+?)\s*$"),
    re.compile(r"^add\s+(?P<name>.+?)\s+to\s+(?:the\s+|my\s+)?(?:to-?\s*do|task)s?(?:\s+list)?\s*$"),
)

# --- done -------------------------------------------------------------------
_TASK_DONE = re.compile(r"^(?:done|finished|completed)\s*[:\-]?\s+(?P<query>.+?)\s*$")
_GROCERY_GOT = re.compile(r"^(?:got|bought|picked\s+up)\s*[:\-]?\s+(?P<query>.+?)\s*$")

# --- fragments --------------------------------------------------------------
_QTY = re.compile(
    r"^(?P<qty>\d+(?:\.\d+)?\s*(?:lbs?|pounds?|oz|ounces?|gal(?:lons?)?|cans?|bags?|boxes|"
    r"bottles?|bunch(?:es)?|dozen|packs?|pints?|quarts?|li?ters?|ml|kgs?|gs?)?)"
    r"\s+(?:of\s+)?(?P<item>.+)$",
    re.IGNORECASE,
)
# The first date-ish token in a task splits the name from the raw due text.
_DUE = re.compile(
    r"\s+\b(?:by|on|at|due|before)?\s*\b("
    + _DAY_TOKENS
    + r"|in\s+\d+\s*(?:mins?|minutes?|hrs?|hours?|days?|weeks?)"
    r"|\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?"
    r"|\d{1,2}:\d{2}"
    r")\b.*$",
    re.IGNORECASE,
)


def _arg(raw: str, m: re.Match[str], group: str) -> str:
    """Slice the ORIGINAL text: we match lowercased, but args keep the casing the user typed."""
    return raw[m.start(group):m.end(group)]


def _split_qty(item: str) -> tuple[str, str | None]:
    m = _QTY.match(item)
    return (m.group("item").strip(), m.group("qty").strip()) if m else (item, None)


def _split_due(name: str) -> tuple[str, str | None]:
    m = _DUE.search(name)
    if not m:
        return name, None
    head = name[: m.start()].strip()
    # A bare "tomorrow" with nothing before it is the task, not the due date.
    return (head, name[m.start():].strip()) if head else (name, None)


def parse(text: str, *, in_grocery_channel: bool = False) -> Intent | None:
    """Return a fast-path Intent, or None if nothing matched (Layer 2 is Phase 5)."""
    raw = text.strip()
    low = raw.lower()
    if not raw or raw[0] in "/!":
        return None

    if _GROCERY_LIST.match(low):
        return Intent("grocery.list", {}, "fastpath")
    if _TASK_LIST.match(low):
        return Intent("task.list", {}, "fastpath")

    m = _AGENDA.match(low)
    if m:
        # Reads only. There is deliberately no calendar.create branch in this file.
        day = _arg(raw, m, "day") if m.group("day") else None
        return Intent("calendar.agenda", {"day": day}, "fastpath")

    for pattern in _GROCERY_ADD:
        m = pattern.match(low)
        if m:
            item, qty = _split_qty(_arg(raw, m, "item"))
            return Intent("grocery.add", {"item": item, "qty": qty}, "fastpath")

    for pattern in _TASK_ADD:
        m = pattern.match(low)
        if m:
            name, due = _split_due(_arg(raw, m, "name"))
            return Intent("task.add", {"name": name, "due": due}, "fastpath")

    if "?" not in raw:  # "got a minute?" is a question, not a check-off
        m = _GROCERY_GOT.match(low)
        if m:
            return Intent("grocery.check", {"query": _arg(raw, m, "query")}, "fastpath")

        m = _TASK_DONE.match(low)
        if m:
            name = "grocery.check" if in_grocery_channel else "task.complete"
            return Intent(name, {"query": _arg(raw, m, "query")}, "fastpath")

    # A bare line in #groceries is an item. Questions are not.
    if in_grocery_channel and "?" not in raw and "\n" not in raw and len(raw) <= 100:
        item, qty = _split_qty(raw)
        return Intent("grocery.add", {"item": item, "qty": qty}, "fastpath")
    return None


if __name__ == "__main__":  # smallest check that fails if a pattern breaks
    assert parse("add milk to the grocery list") == Intent("grocery.add", {"item": "milk", "qty": None}, "fastpath")
    assert parse("buy 2 lbs chicken").args == {"item": "chicken", "qty": "2 lbs"}
    assert parse("we need paper towels").name == "grocery.add"
    assert parse("remind me to call mom tomorrow at 3pm").args == {"name": "call mom", "due": "tomorrow at 3pm"}
    assert parse("todo: pay rent").args == {"name": "pay rent", "due": None}
    assert parse("add dishes to my todo list").name == "task.add"
    assert parse("got milk").name == "grocery.check"
    assert parse("done: pay rent").name == "task.complete"
    assert parse("done: pay rent", in_grocery_channel=True).name == "grocery.check"
    assert parse("grocery list").name == "grocery.list"
    assert parse("what's on my todo list").name == "task.list"
    assert parse("how was your day?") is None
    assert parse("bananas", in_grocery_channel=True).args == {"item": "bananas", "qty": None}
    assert parse("bananas") is None
    assert parse("Buy Milk").args == {"item": "Milk", "qty": None}  # casing survives
    assert parse("Remind me to Call Mom Tomorrow").args == {"name": "Call Mom", "due": "Tomorrow"}
    assert parse("Got Milk").args == {"query": "Milk"}
    assert parse("Bananas", in_grocery_channel=True).args == {"item": "Bananas", "qty": None}
    assert parse("got a minute?") is None
    assert parse("done already?") is None
    assert parse("i need to call mom") is None  # "need to <verb>" is not a grocery
    assert parse("is it going to rain?", in_grocery_channel=True) is None
    # agenda: read-only, and the one intent allowed to keep a trailing "?"
    assert parse("what's on my plate") == Intent("calendar.agenda", {"day": None}, "fastpath")
    assert parse("what do i have today?").args == {"day": "today"}
    assert parse("what do i have tomorrow").args == {"day": "tomorrow"}
    assert parse("whats on my calendar for friday").args == {"day": "friday"}
    assert parse("show me my schedule").args == {"day": None}
    assert parse("agenda").name == "calendar.agenda"
    assert parse("what am i doing on 2026-12-25").args == {"day": "2026-12-25"}
    assert parse("What Do I Have Tomorrow").args == {"day": "Tomorrow"}  # casing survives
    assert parse("what do i have for lunch") is None  # not a day word
    # a regex may never book time: no input reaches calendar.create
    assert parse("schedule dentist tomorrow at 3pm") is None
    assert parse("book a meeting friday") is None
    assert all(i is None or i.name != "calendar.create" for i in (
        parse("schedule lunch tomorrow"), parse("create an event friday at 2pm"),
        parse("add dentist to my calendar"), parse("book gym 6pm")))
    print("fastpath ok")
