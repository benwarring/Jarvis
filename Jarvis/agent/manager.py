"""Agent Manager: the single dispatch point from Intent to tool."""

from __future__ import annotations

from Jarvis.agent.tools import TOOLS
from Jarvis.integrations.gcal import CalendarError
from Jarvis.integrations.notion import NotionError
from Jarvis.router.intents import Intent
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)


def dispatch(intent: Intent) -> tuple[str, str | None]:
    """Run the tool for `intent`. Never raises — always returns (message, external_id).

    Creating tools hand back an external id (a Notion page id, or a Google
    Calendar event id); everything else returns a bare string, normalised to
    `(message, None)` so callers see one shape.
    """
    tool = TOOLS.get(intent.name)
    if tool is None:
        log.warning("No tool registered for intent %r (source=%s)", intent.name, intent.source)
        return "I don't know how to do that yet.", None
    try:
        result = tool(**intent.args)
    except NotionError as exc:
        log.error("Notion failed on %s: %s", intent.name, exc)
        return f"Notion wouldn't play along: {exc}", None
    except CalendarError as exc:
        log.error("Google Calendar failed on %s: %s", intent.name, exc)
        return f"Google Calendar wouldn't play along: {exc}", None
    except Exception:
        log.exception("Tool %s failed (source=%s)", intent.name, intent.source)
        return "Something went wrong on my end — that didn't go through.", None
    return result if isinstance(result, tuple) else (result, None)
