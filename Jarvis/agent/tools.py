"""The tool registry — the ONLY implementation of each capability.

Slash commands, the regex fast-path and (Phase 5) the LLM all reach Notion and
Google Calendar through this dict. Every callable takes the intent's arg keys as
keyword arguments and returns one short user-facing line — plus the external id,
as `(line, external_id)`, for the tools that create something (a Notion page id
or a Google Calendar event id).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import DEFAULT_DURATION_MINUTES as DEFAULT_DURATION
from Jarvis.integrations.gcal import Event
from Jarvis.integrations.notion import Grocery, Task
from Jarvis.utils.dates import parse_when, to_local

T = TypeVar("T")

# ponytail: Discord's message cap is 2000 chars; we truncate instead of paginating.
# Add paging when a list realistically runs past ~40 items.
MAX_LEN = 1900


def _when(dt: datetime) -> str:
    local = to_local(dt)
    return local.strftime("%a %b %d") if (local.hour, local.minute) == (0, 0) else local.strftime("%a %b %d %I:%M%p")


def _format_tasks(tasks: list[Task]) -> str:
    if not tasks:
        return "No open tasks."
    lines = []
    for n, t in enumerate(tasks, 1):
        line = f"{n}. {t.name}"
        if t.priority:
            line += f" [{t.priority}]"
        if t.due:
            line += f" (due {_when(t.due)})"
        lines.append(line)
    return "\n".join(lines)[:MAX_LEN]


def _format_groceries(items: list[Grocery]) -> str:
    if not items:
        return "Grocery list is empty."
    lines = []
    for n, g in enumerate(items, 1):
        line = f"{n}. {g.item}"
        if g.qty:
            line += f" — {g.qty}"
        if g.category:
            line += f" ({g.category})"
        lines.append(line)
    return "\n".join(lines)[:MAX_LEN]


def _format_events(events: list[Event]) -> str:
    if not events:
        return "Nothing on the calendar."
    lines = []
    for e in events:
        span = "all day" if e.all_day else f"{to_local(e.start):%I:%M%p}-{to_local(e.end):%I:%M%p}"
        line = f"- {span}  {e.title}"
        if e.location:
            line += f" @ {e.location}"
        lines.append(line)
    return "\n".join(lines)[:MAX_LEN]


def _pick(query: str, items: list[T], label: Callable[[T], str]) -> tuple[T | None, str]:
    """Case-insensitive substring match. Never guesses between two candidates."""
    q = query.strip().lower()
    hits = [i for i in items if q in label(i).lower()]
    if not hits:
        return None, f"Nothing on the list matches '{query}'."
    exact = [i for i in hits if label(i).strip().lower() == q]
    if len(exact) == 1:
        return exact[0], ""
    if len(hits) > 1:
        return None, f"'{query}' matches {len(hits)}: " + ", ".join(label(i) for i in hits[:5]) + ". Be more specific."
    return hits[0], ""


def grocery_add(item: str, qty: str | None = None) -> tuple[str, str | None]:
    category = notion.categorize(item)
    page_id = notion.add_grocery(item, qty=qty, category=category)
    return f"Added {qty + ' ' if qty else ''}{item} to groceries ({category}).", page_id


def grocery_list() -> str:
    return _format_groceries(notion.list_groceries())


def grocery_check(query: str) -> str:
    hit, problem = _pick(query, notion.list_groceries(), lambda g: g.item)
    if hit is None:
        return problem
    notion.check_off_grocery(hit.id)
    return f"Got it: {hit.item}."


def task_add(name: str, due: str | None = None) -> tuple[str, str | None]:
    when = parse_when(due) if due else None
    page_id = notion.add_task(name, due=when)
    if when:
        return f"Added task: {name} (due {when.strftime('%a %b %d %I:%M%p')}).", page_id
    if due:
        return f"Added task: {name} — couldn't read a date out of '{due}', so it has no due date.", page_id
    return f"Added task: {name}.", page_id


def task_list() -> str:
    return _format_tasks(notion.list_open_tasks())


def task_complete(query: str) -> str:
    hit, problem = _pick(query, notion.list_open_tasks(), lambda t: t.name)
    if hit is None:
        return problem
    notion.complete_task(hit.id)
    return f"Done: {hit.name}."


def calendar_agenda(day: str | None = None) -> str:
    when = parse_when(day) if day else None
    if day and when is None:
        return f"Couldn't read a day out of '{day}'."
    header = f"{when:%a %b %d}" if when else "Today"
    return f"**{header}**\n" + _format_events(gcal.list_events(when.date() if when else None))


def calendar_create(title: str, when: str, duration: int | None = None) -> tuple[str, str | None]:
    start = parse_when(when)
    if start is None:
        return f"Couldn't read a time out of '{when}' - nothing was created.", None
    event = gcal.create_event(title, start, duration_minutes=duration or DEFAULT_DURATION)
    return f"Booked: {event.title} - {_when(event.start)}.", event.id


# Page-creating tools return (message, external_id); the rest return a bare
# message. `manager.dispatch` normalises both to a tuple for its callers.
TOOLS: dict[str, Callable[..., str | tuple[str, str | None]]] = {
    "grocery.add": grocery_add,
    "grocery.list": grocery_list,
    "grocery.check": grocery_check,
    "task.add": task_add,
    "task.list": task_list,
    "task.complete": task_complete,
    "calendar.agenda": calendar_agenda,
    # Never dispatched by a slash command or the fast-path directly: a calendar
    # write only runs from the confirmation flow, after a human ✅.
    "calendar.create": calendar_create,
}


# --- Layer 2: what the model is allowed to ask for ---------------------------
# The schemas live here, next to the implementations they describe, so a renamed
# parameter breaks the self-check below instead of surfacing as a confused bot.

# The model never sees the dotted registry keys - OpenAI's function-name grammar
# has no dot in it. These are plan.md section 4's names, one per TOOLS entry, and
# this dict is the ALLOWLIST the router validates model output against: a name
# that is not a key here never reaches dispatch.
LLM_NAMES: dict[str, str] = {
    "add_grocery_item": "grocery.add",
    "list_groceries": "grocery.list",
    "check_off_grocery": "grocery.check",
    "add_task": "task.add",
    "list_tasks": "task.list",
    "complete_task": "task.complete",
    "list_calendar_events": "calendar.agenda",
    "create_calendar_event": "calendar.create",
}

# plan.md section 4 / CLAUDE.md: a calendar write is never executed by the layer
# that parsed it. The model may PROPOSE this one; only a human's ✅ runs it.
PROPOSAL_ONLY = frozenset({"calendar.create"})

# The other half of the same rule - "calendar writes AND multi-item batches" need
# a ✅ - so Layer 2 can count the writes in one reply. The reads (grocery.list,
# task.list, calendar.agenda) are left out on purpose.
WRITE_NAMES = frozenset(
    {"grocery.add", "grocery.check", "task.add", "task.complete", "calendar.create"}
)

_STR = {"type": "string"}


def _fn(name: str, description: str, properties: dict[str, dict], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Dates and times are passed through as the user's own words: `utils/dates.parse_when`
# resolves them, so the model never has to know what day it is (and we never have to
# spend tokens telling it).
TOOL_SCHEMAS: list[dict] = [
    _fn(
        "add_grocery_item",
        "Add one item to the grocery list.",
        {"item": _STR | {"description": "What to buy, e.g. 'milk'."},
         "qty": _STR | {"description": "How much, e.g. '2 lbs'. Omit if unsaid."}},
        ["item"],
    ),
    _fn("list_groceries", "Read the grocery list back.", {}, []),
    _fn(
        "check_off_grocery",
        "Tick one item off the grocery list.",
        {"query": _STR | {"description": "Part of the item's name."}},
        ["query"],
    ),
    _fn(
        "add_task",
        "Add one task to the to-do list.",
        {"name": _STR | {"description": "What needs doing."},
         "due": _STR | {"description": "When it is due, in the user's own words, e.g. 'friday 5pm'."}},
        ["name"],
    ),
    _fn("list_tasks", "Read the open tasks back.", {}, []),
    _fn(
        "complete_task",
        "Mark one open task done.",
        {"query": _STR | {"description": "Part of the task's name."}},
        ["query"],
    ),
    _fn(
        "list_calendar_events",
        "Read the calendar for one day.",
        {"day": _STR | {"description": "Which day, in the user's own words. Omit for today."}},
        [],
    ),
    _fn(
        "create_calendar_event",
        "Propose a new calendar event. The user has to confirm it before it is created.",
        {"title": _STR | {"description": "What the event is."},
         "when": _STR | {"description": "Start time in the user's own words, e.g. 'tomorrow 3pm'."},
         "duration": {"type": "integer", "description": "Length in minutes. Omit for the default."}},
        ["title", "when"],
    ),
]


def filter_args(name: str, args: dict) -> dict:
    """Drop arguments the tool does not declare. Model output is untrusted input."""
    allowed = inspect.signature(TOOLS[name]).parameters
    return {key: value for key, value in args.items() if key in allowed}


def _check_schemas() -> None:
    """Every tool has a schema and every schema has a tool - checked, not hoped for.

    A drifted name or parameter is otherwise silent until a user trips over it.
    """
    named = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert named == set(LLM_NAMES), f"schema/name mismatch: {named ^ set(LLM_NAMES)}"
    assert set(LLM_NAMES.values()) == set(TOOLS), (
        f"tool/name mismatch: {set(LLM_NAMES.values()) ^ set(TOOLS)}"
    )
    assert PROPOSAL_ONLY <= WRITE_NAMES <= set(TOOLS), "a proposal-only or write name that is not a tool"
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        params = inspect.signature(TOOLS[LLM_NAMES[fn["name"]]]).parameters
        declared = set(fn["parameters"]["properties"])
        assert declared <= set(params), f"{fn['name']}: schema invents {declared - set(params)}"
        needed = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
        assert needed == set(fn["parameters"]["required"]), f"{fn['name']}: wrong required list"


_check_schemas()  # cheap, and a mismatch is a bug that ships silently otherwise


if __name__ == "__main__":
    _check_schemas()
    assert filter_args("task.add", {"name": "x", "sudo": True}) == {"name": "x"}
    assert not WRITE_NAMES & {"grocery.list", "task.list", "calendar.agenda"}, "a read is not a write"
    print(f"tools ok - {len(TOOLS)} tools, {len(TOOL_SCHEMAS)} schemas")
