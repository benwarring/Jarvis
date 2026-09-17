"""The Intent schema every router layer produces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NAMES = frozenset(
    {
        "grocery.add",
        "grocery.list",
        "grocery.check",
        "task.add",
        "task.list",
        "task.complete",
        "calendar.agenda",
        "calendar.create",
        "brief.read",
    }
)


@dataclass(frozen=True)
class Intent:
    name: str
    args: dict[str, Any]
    source: str  # "slash" | "fastpath" | "llm" | "schedule"
