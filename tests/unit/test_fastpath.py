"""Layer 1 regexes: plan.md section 4. The near-misses matter more than the hits."""

from __future__ import annotations

import pytest

from Jarvis.router.fastpath import parse
from Jarvis.router.intents import NAMES

# (text, intent name, args) -- every pattern in plan.md section 4 that Phase 2 owns.
HITS = [
    ("add milk to the grocery list", "grocery.add", {"item": "milk", "qty": None}),
    ("add 2 lbs of chicken to my shopping list", "grocery.add", {"item": "chicken", "qty": "2 lbs"}),
    ("buy bananas", "grocery.add", {"item": "bananas", "qty": None}),
    ("pick up eggs", "grocery.add", {"item": "eggs", "qty": None}),
    ("we need paper towels", "grocery.add", {"item": "paper towels", "qty": None}),
    ("i need dish soap", "grocery.add", {"item": "dish soap", "qty": None}),
    ("i need to buy milk", "grocery.add", {"item": "milk", "qty": None}),
    ("BUY MILK", "grocery.add", {"item": "MILK", "qty": None}),  # matching is case-insensitive, the item keeps its casing
    ("  buy milk  ", "grocery.add", {"item": "milk", "qty": None}),
    ("remind me to file taxes", "task.add", {"name": "file taxes", "due": None}),
    ("remind me to call mom tomorrow at 3pm", "task.add", {"name": "call mom", "due": "tomorrow at 3pm"}),
    ("remind me to pick up the kids on friday", "task.add", {"name": "pick up the kids", "due": "on friday"}),
    ("todo: pay rent", "task.add", {"name": "pay rent", "due": None}),
    ("to-do - water plants", "task.add", {"name": "water plants", "due": None}),
    ("add dishes to my todo list", "task.add", {"name": "dishes", "due": None}),
    ("done: pay rent", "task.complete", {"query": "pay rent"}),
    ("finished the laundry", "task.complete", {"query": "the laundry"}),
    ("completed taxes", "task.complete", {"query": "taxes"}),
    ("got milk", "grocery.check", {"query": "milk"}),
    ("bought eggs", "grocery.check", {"query": "eggs"}),
    ("picked up the dry cleaning", "grocery.check", {"query": "the dry cleaning"}),
    ("grocery list", "grocery.list", {}),
    ("groceries", "grocery.list", {}),
    ("what's on my grocery list", "grocery.list", {}),
    ("show me the shopping list", "grocery.list", {}),
    ("todo", "task.list", {}),
    ("my todos", "task.list", {}),
    ("what's on my todo list", "task.list", {}),
    ("task list", "task.list", {}),
    # agenda — Phase 3. Reads only; there is deliberately no calendar.create pattern.
    ("what's on my plate", "calendar.agenda", {"day": None}),
    ("whats on my calendar", "calendar.agenda", {"day": None}),
    ("what's on my schedule for tomorrow", "calendar.agenda", {"day": "tomorrow"}),
    ("what do i have today", "calendar.agenda", {"day": "today"}),
    ("what do i have today?", "calendar.agenda", {"day": "today"}),  # the normal way to ask
    ("what do i have tomorrow", "calendar.agenda", {"day": "tomorrow"}),
    ("what have i got on friday", "calendar.agenda", {"day": "friday"}),
    ("what am i doing on 2026-12-25", "calendar.agenda", {"day": "2026-12-25"}),
    ("show me my agenda", "calendar.agenda", {"day": None}),
    ("list my calendar for next monday", "calendar.agenda", {"day": "next monday"}),
    ("agenda", "calendar.agenda", {"day": None}),
    ("my schedule", "calendar.agenda", {"day": None}),
    ("What Do I Have Tomorrow", "calendar.agenda", {"day": "Tomorrow"}),  # casing survives
]

MISSES = [
    "i need to call mom",            # "need to <verb>" is a task, never a grocery item
    "we need to talk about the trip",
    "i need to think",
    "are we done here?",
    "did you buy the tickets?",
    "how was your day?",
    "nobody knows",
    "the meeting is done",
    "i got a new job offer today",
    "buy",                           # a bare verb with no object
    "done",
    "done!",
    "remind me",
    "remind me to",
    "add milk to the list",          # no "grocery"/"todo" -- ambiguous, leave it to Layer 2
    "what do i have for lunch",      # "lunch" is not a day word, so it is not an agenda
    "is it going to rain?",          # no weather capability; must stay a miss
    "weather",
    "/todo add milk",                # slash commands are Layer 0
    "!ping",
    "",
    "   ",
    "bananas",                       # a bare line only means an item in #groceries
    "buy milk\nbuy eggs",            # multi-line needs a human, not a regex
]


@pytest.mark.parametrize("text, name, args", HITS, ids=[h[0] for h in HITS])
def test_pattern_hits(text, name, args):
    intent = parse(text)
    assert intent is not None, "expected a fast-path hit"
    assert (intent.name, intent.args, intent.source) == (name, args, "fastpath")


@pytest.mark.parametrize("text", MISSES, ids=[repr(m) for m in MISSES])
def test_near_misses_do_not_match(text):
    assert parse(text) is None


def test_every_hit_uses_a_registered_intent_name():
    assert {name for _, name, _ in HITS} <= NAMES


# --- the fast-path may never book time (contract addendum, "Fast-path — read only") ---

WRITE_ATTEMPTS = [
    "schedule dentist tomorrow at 3pm",
    "book a meeting friday",
    "create an event friday at 2pm",
    "add dentist to my calendar",
    "put lunch on my calendar tomorrow",
    "book gym 6pm",
    "new event: standup 9am",
    "make an appointment on monday",
]


@pytest.mark.parametrize("text", [h[0] for h in HITS] + MISSES + WRITE_ATTEMPTS, ids=repr)
@pytest.mark.parametrize("in_grocery_channel", [False, True])
def test_a_regex_can_never_produce_a_calendar_write(text, in_grocery_channel):
    """A regex is not allowed to book time on a real calendar — every write goes
    through /event and a ✅, so no input on any channel may reach calendar.create."""
    intent = parse(text, in_grocery_channel=in_grocery_channel)
    assert intent is None or intent.name != "calendar.create"


@pytest.mark.parametrize("text", WRITE_ATTEMPTS, ids=repr)
def test_phrasings_that_sound_like_a_booking_do_not_match_at_all(text):
    assert parse(text) is None


# --- #groceries channel -----------------------------------------------------


@pytest.mark.parametrize(
    "text, args",
    [
        ("bananas", {"item": "bananas", "qty": None}),
        ("2 lbs ground beef", {"item": "ground beef", "qty": "2 lbs"}),
    ],
)
def test_bare_line_in_grocery_channel_is_an_item(text, args):
    intent = parse(text, in_grocery_channel=True)
    assert (intent.name, intent.args) == ("grocery.add", args)


@pytest.mark.parametrize("text", ["is it going to rain?", "hey, are we out of milk?", "milk\neggs", "x" * 105])
def test_grocery_channel_ignores_questions_and_essays(text):
    assert parse(text, in_grocery_channel=True) is None


def test_done_in_grocery_channel_checks_off_an_item():
    assert parse("done: milk", in_grocery_channel=True).name == "grocery.check"


def test_done_outside_grocery_channel_completes_a_task():
    assert parse("done: milk").name == "task.complete"


@pytest.mark.parametrize("text", ["got a minute?", "got any plans?"])
def test_a_question_is_never_a_check_off(text):
    """A trailing '?' is a question, so no branch — bare-line, got or done —
    may turn it into a write."""
    assert parse(text) is None
