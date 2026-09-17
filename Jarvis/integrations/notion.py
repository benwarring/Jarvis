"""Notion: the system of record for tasks and groceries.

Every call is wrapped: a failure is logged by exception type only and re-raised as a
NotionError carrying a user-safe message. The token and raw response bodies never
reach a log line, an exception string, or a return value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable

from notion_client import Client

from Jarvis.config import get_config
from Jarvis.utils.dates import to_local, to_utc
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

HIGH = "High"  # plan.md section 6: a value of the Notion Priority select
_PRIORITY_RANK = {HIGH: 0, "Medium": 1, "Low": 2}

# plan.md section 6 categories. Longest matching keyword wins, so "ice cream" beats
# "cream" and "toilet paper" beats the "oil" hiding inside "toilet".
_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Produce": (
        "apple", "banana", "lettuce", "spinach", "kale", "tomato", "onion", "garlic",
        "potato", "carrot", "celery", "pepper", "cucumber", "broccoli", "avocado",
        "lemon", "lime", "orange", "berry", "berries", "strawberr", "blueberr",
        "grape", "mushroom", "cilantro", "basil", "salad", "greens", "ginger",
        "eggplant", "zucchini", "squash", "corn", "peach", "pear", "melon",
    ),
    "Dairy": (
        "milk", "cheese", "yogurt", "butter", "cream", "egg", "half and half",
        "cottage", "sour cream", "creamer",
    ),
    "Meat": (
        "chicken", "beef", "steak", "pork", "bacon", "sausage", "turkey", "ham",
        "salmon", "shrimp", "fish", "tuna", "ground beef", "deli", "hot dog",
    ),
    "Pantry": (
        "bread", "rice", "pasta", "flour", "sugar", "salt", "oil", "vinegar", "bean",
        "cereal", "oats", "peanut butter", "jelly", "jam", "coffee", "tea", "honey",
        "sauce", "soup", "chips", "cracker", "spice", "broth", "tortilla", "canned",
        "nuts", "granola", "syrup", "ketchup", "mustard", "mayo", "bagel", "juice",
    ),
    "Frozen": ("frozen", "ice cream", "pizza", "waffle", "popsicle"),
    "Household": (
        "paper towel", "toilet", "toilet paper", "detergent", "soap", "shampoo",
        "toothpaste", "trash bag", "sponge", "bleach", "dish soap", "cleaner",
        "foil", "ziploc", "battery", "batteries", "tissue", "napkin",
        "plastic wrap", "laundry", "razor", "deodorant",
    ),
}
_KEYWORDS: dict[str, str] = {kw: cat for cat, kws in _CATEGORIES.items() for kw in kws}


class NotionError(Exception):
    """A Notion call failed. The message is safe to show the user."""


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    status: str
    due: datetime | None
    priority: str | None
    estimate: int | None


@dataclass(frozen=True)
class Grocery:
    id: str
    item: str
    qty: str | None
    category: str | None
    got_it: bool


@lru_cache(maxsize=1)
def _client() -> Client:
    return Client(auth=get_config().notion_token)


def _call(what: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - every provider failure reads the same to us
        # Type name only: a Notion error body can quote the request, which can quote a token.
        log.error("Notion call failed: %s (%s)", what, type(exc).__name__)
        raise NotionError(
            f"Couldn't {what} in Notion. Check the integration is connected to the "
            "database, then try again."
        ) from None


def _title(props: dict[str, Any], key: str) -> str:
    return "".join(part["plain_text"] for part in props.get(key, {}).get("title", []))


def _text(props: dict[str, Any], key: str) -> str | None:
    parts = props.get(key, {}).get("rich_text", [])
    return "".join(part["plain_text"] for part in parts) or None


def _select(props: dict[str, Any], key: str) -> str | None:
    value = props.get(key, {}).get("select") or props.get(key, {}).get("status")
    return value["name"] if value else None


def _date(props: dict[str, Any], key: str) -> datetime | None:
    value = props.get(key, {}).get("date")
    if not value or not value.get("start"):
        return None
    start = value["start"]
    parsed = datetime.fromisoformat(start)
    if len(start) == 10:  # date-only: a local calendar day, not a UTC instant
        return to_local(to_utc(parsed))
    return to_local(parsed)


# --- property names ---------------------------------------------------------
#
# Notion property names are data, not code. These match plan.md section 6, but the
# live database is the authority: renaming a property in Notion breaks every call
# that hard-codes it. They live here, once, rather than sprinkled through the
# functions below, so a rename in Notion is a one-line change here.

TASK_TITLE = "Name"
TASK_DUE = "Due"
TASK_STATUS = "Status"
TASK_PRIORITY = "Priority"
TASK_ESTIMATE = "Estimate"
TASK_NOTES = "Notes"
TASK_SOURCE = "Source"

GROCERY_TITLE = "Item"
GROCERY_QTY = "Qty"
GROCERY_CATEGORY = "Category"
GROCERY_GOT = "Got it"


# --- tasks ------------------------------------------------------------------


def add_task(
    name: str,
    *,
    due: datetime | None = None,
    priority: str | None = None,
    estimate: int | None = None,
    notes: str | None = None,
) -> str:
    props: dict[str, Any] = {
        TASK_TITLE: {"title": [{"text": {"content": name}}]},
        TASK_SOURCE: {"select": {"name": "discord"}},
    }
    if due is not None:
        props[TASK_DUE] = {"date": {"start": to_local(due).isoformat()}}
    if priority:
        props[TASK_PRIORITY] = {"select": {"name": priority}}
    if estimate is not None:
        props[TASK_ESTIMATE] = {"number": estimate}
    if notes:
        props[TASK_NOTES] = {"rich_text": [{"text": {"content": notes}}]}

    page = _call(
        "add that task",
        _client().pages.create,
        parent={"data_source_id": get_config().notion_tasks_db_id},
        properties=props,
    )
    return page["id"]


def list_open_tasks(limit: int = 25) -> list[Task]:
    result = _call(
        "read your tasks",
        _client().data_sources.query,
        data_source_id=get_config().notion_tasks_db_id,
        filter={"property": TASK_STATUS, "status": {"does_not_equal": "Done"}},
        page_size=limit,
    )
    tasks = [
        Task(
            id=row["id"],
            name=_title(row["properties"], TASK_TITLE),
            status=_select(row["properties"], TASK_STATUS) or "Not started",
            due=_date(row["properties"], TASK_DUE),
            priority=_select(row["properties"], TASK_PRIORITY),
            estimate=(row["properties"].get(TASK_ESTIMATE) or {}).get("number"),
        )
        for row in result["results"]
    ]
    # ponytail: sorted here rather than by Notion, because a Select sorts
    # alphabetically (High/Low/Medium). Only orders the first page - fine at limit<=100,
    # switch to a Notion sort plus pagination if the list ever outgrows one page.
    tasks.sort(
        key=lambda t: (
            _PRIORITY_RANK.get(t.priority or "", 3),
            t.due.timestamp() if t.due else float("inf"),
        )
    )
    return tasks


def _set_property(what: str, page_id: str, prop: dict[str, Any]) -> None:
    _call(what, _client().pages.update, page_id=page_id, properties=prop)


def complete_task(page_id: str) -> None:
    _set_property("close that task", page_id, {TASK_STATUS: {"status": {"name": "Done"}}})


# --- groceries --------------------------------------------------------------


def add_grocery(item: str, *, qty: str | None = None, category: str | None = None) -> str:
    props: dict[str, Any] = {
        GROCERY_TITLE: {"title": [{"text": {"content": item}}]},
        GROCERY_CATEGORY: {"select": {"name": category or categorize(item)}},
        GROCERY_GOT: {"checkbox": False},
    }
    if qty:
        props[GROCERY_QTY] = {"rich_text": [{"text": {"content": qty}}]}

    page = _call(
        "add that item",
        _client().pages.create,
        parent={"data_source_id": get_config().notion_groceries_db_id},
        properties=props,
    )
    return page["id"]


def list_groceries(limit: int = 50) -> list[Grocery]:
    result = _call(
        "read your grocery list",
        _client().data_sources.query,
        data_source_id=get_config().notion_groceries_db_id,
        filter={"property": GROCERY_GOT, "checkbox": {"equals": False}},
        page_size=limit,
    )
    return [
        Grocery(
            id=row["id"],
            item=_title(row["properties"], GROCERY_TITLE),
            qty=_text(row["properties"], GROCERY_QTY),
            category=_select(row["properties"], GROCERY_CATEGORY),
            got_it=bool((row["properties"].get(GROCERY_GOT) or {}).get("checkbox")),
        )
        for row in result["results"]
    ]


def check_off_grocery(page_id: str) -> None:
    _set_property("check that item off", page_id, {GROCERY_GOT: {"checkbox": True}})


def categorize(item: str) -> str:
    """Static keyword map -> a plan.md section 6 category, else 'Other'. No API call."""
    low = item.lower()
    hits = [kw for kw in _KEYWORDS if kw in low]
    return _KEYWORDS[max(hits, key=len)] if hits else "Other"
