"""OpenAI transport for Layer 2. Nothing here knows what a task or an event is.

Same shape as notion.py / gcal.py: the call is wrapped, a failure is logged by
exception TYPE NAME only and re-raised as an LLMError carrying a user-safe message.
The API key, the prompt and the raw response body never reach a log line, an
exception string, or a Discord message. Auth happens on first real call, never at
import, so this module is importable with no key in the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from openai import OpenAI

from Jarvis.config import get_config
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

# ponytail: hand-maintained price table, USD per 1M tokens. There is no pricing API,
# so this drifts when OpenAI reprices - re-check it when you change OPENAI_MODEL.
# Verified 2026-09-11 against https://developers.openai.com/api/docs/pricing
# (standard processing; batch and cached-input rates deliberately ignored - we use
# neither). gpt-4.1-mini is the configured model; the other two are here so a model
# swap in .env does not silently fall off the table.
_PRICES: dict[str, tuple[float, float]] = {  # model -> (input, output) per 1M tokens
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-nano": (0.10, 0.40),
}

# An unpriced model must over-charge, never under-charge: a zero would silently
# disable the spend guard, which is the one failure nobody would notice. Element-wise,
# not max(_PRICES.values()) — tuples compare lexicographically, so a model with a high
# input and a low output rate would set the ceiling and under-charge on output tokens.
_FALLBACK_PRICE = (
    max(p[0] for p in _PRICES.values()),
    max(p[1] for p in _PRICES.values()),
)


class LLMError(Exception):
    """An LLM call failed. The message is safe to show the user."""


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    tool_calls: list[tuple[str, dict]]  # (tool name, args) - UNTRUSTED, validate before use
    prompt_tokens: int
    completion_tokens: int


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    """Built once, lazily - reading the key at import time would break a keyless import."""
    return OpenAI(api_key=get_config().openai_api_key)


def cost_usd(prompt_tokens: int, completion_tokens: int, model: str | None = None) -> float:
    """Price one call's usage. Defaults to the configured model."""
    rate_in, rate_out = _PRICES.get(model or get_config().openai_model, _FALLBACK_PRICE)
    return (prompt_tokens * rate_in + completion_tokens * rate_out) / 1_000_000


def complete(messages: list[dict], tools: list[dict]) -> LLMReply:
    """One round trip. Single-turn, no streaming, no retries (Phase 9)."""
    try:
        response = _client().chat.completions.create(
            model=get_config().openai_model,
            messages=messages,
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
        )
    except Exception as exc:  # noqa: BLE001 - every provider failure reads the same to us
        # Type name only: an OpenAI error body echoes the request, and the request is
        # sent with the key attached.
        log.error("LLM call failed (%s)", type(exc).__name__)
        raise LLMError("I couldn't reach the language model just now. Try a slash command.") from None

    choice = response.choices[0].message
    usage: Any = response.usage
    return LLMReply(
        text=choice.content or None,
        tool_calls=_tool_calls(choice.tool_calls),
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def _tool_calls(raw: Any) -> list[tuple[str, dict]]:
    """Model output is untrusted: unparseable arguments are dropped, not guessed at."""
    calls = []
    for call in raw or []:
        try:
            args = json.loads(call.function.arguments or "{}")
        except (ValueError, AttributeError) as exc:
            log.warning("Discarded a malformed tool call (%s)", type(exc).__name__)
            continue
        if isinstance(args, dict):
            calls.append((call.function.name, args))
    return calls


if __name__ == "__main__":  # smallest check that fails if the money math or parsing breaks
    from types import SimpleNamespace

    assert round(cost_usd(1_000_000, 0, "gpt-4.1-mini"), 6) == 0.40
    assert round(cost_usd(0, 1_000_000, "gpt-4.1-mini"), 6) == 1.60
    assert round(cost_usd(1000, 500, "gpt-4.1-mini"), 8) == 0.0012
    assert cost_usd(0, 0, "gpt-4.1-mini") == 0.0
    # an unknown model must cost MORE than the cheapest, never zero
    assert cost_usd(1000, 1000, "some-model-launched-next-tuesday") >= cost_usd(1000, 1000, "gpt-4.1")

    def _fn(name, arguments):
        return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))

    assert _tool_calls(None) == []
    assert _tool_calls([_fn("list_tasks", "{}")]) == [("list_tasks", {})]
    assert _tool_calls([_fn("add_task", '{"name":"x"}')]) == [("add_task", {"name": "x"})]
    assert _tool_calls([_fn("add_task", "{not json"), _fn("list_tasks", "{}")]) == [("list_tasks", {})]
    assert _tool_calls([_fn("add_task", "[1,2]")]) == []  # valid JSON, wrong shape
    print("llm transport ok")
