"""Layer 2: the LLM tool-use loop — everything the regex fast-path missed.

The only part of Jarvis that costs money, so it is guarded twice: a daily spend
ceiling checked BEFORE the request goes out, and a hard cap on how many rounds
one message may buy.

This layer owns no implementations. It picks a tool out of `agent/tools.TOOLS` —
the same dict the slash commands and the fast-path use — and runs it through
`agent/manager.dispatch`, with two exceptions, both plan.md section 4's: a calendar
write, and a reply asking for more than one write, are never executed here. The
model proposes them; `bot/handlers` puts them behind the ✅ that section requires,
because a small model misparses more often than a human does.

Everything the model sends back is untrusted input: a tool name is looked up in
an allowlist, and arguments the tool does not declare are dropped.
"""

from __future__ import annotations

from Jarvis.agent.manager import dispatch
from Jarvis.agent.tools import LLM_NAMES, PROPOSAL_ONLY, TOOL_SCHEMAS, WRITE_NAMES, filter_args
from Jarvis.config import get_config
from Jarvis.integrations.llm import LLMError, complete, cost_usd
from Jarvis.router.intents import Intent
from Jarvis.storage.models import record_llm_spend, spend_today
from Jarvis.utils.dates import now_local
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

# plan.md section 11's wording, used for every Layer 2 dead end: over budget, a
# provider outage, or a model that produced nothing usable. The user gets one
# honest line and a working alternative, never a stack trace.
FALLBACK = "I didn't catch that — try a slash command."

# Two is enough for this tool set: one round to pick a tool, one to say what it
# found. An unbounded loop is both a runaway bill and the failure mode the
# security review audits for by name. Only the first round is handed tools, so "say
# what it found" is enforced rather than hoped for - see the loop in `handle`.
MAX_ROUNDS = 2

# Set when a spend write fails. The guard reads a counter that only grows if those
# writes land, so a broken write would leave Layer 2 spending against a frozen
# number: the write side fails closed too, not just the read side.
# ponytail: in-memory and one-way, so a restart is the reset. A retry or an
# N-consecutive-failures count would buy nothing a restart does not.
_SPEND_BLIND = False

SYSTEM = (
    "You are Jarvis, a terse personal secretary. Use a tool when one fits, "
    "otherwise answer in one short line. Pass dates and times through as the "
    "user's own words ('tomorrow 3pm'); do not convert them. Never invent list "
    "contents or calendar entries — read them with a tool."
)


def handle(text: str) -> tuple[str, str | None] | tuple[Intent, ...] | None:
    """Answer `text` with the LLM, or propose writes for confirmation.

    Returns `(message, external_id)` like `dispatch` for anything answered here,
    a tuple of `Intent` for writes the caller must put behind a ✅, or None if there was
    nothing to say. Never raises: Layer 2 failing must not take the bot down.

    NOTE (contract deviation, reported deliberately): the addendum types this as
    `tuple[str, str | None] | None`. That shape cannot carry the proposal the same
    addendum requires it to "hand back" — an `(message, id)` pair has nowhere to
    put the intents, and the router may not import `bot/` to raise the prompt
    itself. The `(message, external_id)` case is unchanged; callers tell the two
    tuple arms apart by the type of the first element.
    """
    if _SPEND_BLIND:
        log.warning("LLM spend cannot be recorded; Layer 2 stays shut until restart")
        return FALLBACK, None

    day = now_local().strftime("%Y-%m-%d")
    try:
        spent = spend_today(day)
    except Exception:
        # A spend counter we cannot read is a spend guard that does not exist.
        log.exception("Couldn't read today's LLM spend; keeping Layer 2 shut")
        return FALLBACK, None
    limit = get_config().llm_daily_spend_limit_usd
    if spent >= limit:
        # Checked before the request, so tripping the guard costs nothing.
        log.warning("Daily LLM budget spent; Layer 2 is closed until tomorrow")
        return FALLBACK, None

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": text},
    ]
    results: list[str] = []
    for round_number in range(MAX_ROUNDS):
        # Tools on the first round only. Round 2 is handed round 1's tool output as a
        # plain user turn, and that text can carry a Notion item title written by
        # anyone with access to the database - with tools still attached, a crafted
        # title could drive a write with no human ✅. No tools, no write: prose only.
        try:
            reply = complete(messages, TOOL_SCHEMAS if round_number == 0 else [])
        except LLMError as exc:
            log.error("Layer 2 gave up: %s", exc)
            return FALLBACK, None
        _record(day, reply.prompt_tokens, reply.completion_tokens)

        if not reply.tool_calls:
            return (reply.text or FALLBACK), None

        proposals, results = _run_tools(reply.tool_calls)
        if proposals:
            return proposals  # NOT executed - the ✅ flow decides.
        if not results:
            return FALLBACK, None
        # ponytail: results go back as a plain user turn, not a `tool` message.
        # LLMReply carries no tool_call ids, and single-turn work does not need
        # them. Thread the real ids through if multi-turn context ever lands.
        messages.append({"role": "user", "content": "Tool results:\n" + "\n".join(results)})

    # Budget spent with the model still asking for tools. Tool output is already a
    # user-facing line, so hand that over rather than buying a third round.
    log.warning("Layer 2 hit the %d-round cap; answering with the last tool output", MAX_ROUNDS)
    return ("\n".join(results) or FALLBACK), None


def _record(day: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Bill the call immediately, so a crash later still leaves the spend counted."""
    global _SPEND_BLIND
    try:
        record_llm_spend(day, prompt_tokens, completion_tokens, cost_usd(prompt_tokens, completion_tokens))
    except Exception:
        # This call is already paid for and the user still gets their answer; the
        # flag is what stops the NEXT one spending against a counter that stopped.
        _SPEND_BLIND = True
        log.exception("Couldn't record LLM spend for %s; closing Layer 2 until restart", day)


def _run_tools(calls: list[tuple[str, dict]]) -> tuple[tuple[Intent, ...], list[str]]:
    """Run the tools the model asked for. Returns (proposals, lines).

    plan.md section 4 wants a ✅ on calendar writes and on multi-item batches, so a
    reply's writes are handed back unexecuted in either case: any `calendar.create`,
    or two or more writes at once (they confirm together, or not at all). A lone
    non-calendar write runs, which is the bargain the fast-path already strikes -
    one write per message, nothing asked. Reads always run: their output is what
    round 2 answers from.
    """
    intents: list[Intent] = []
    for raw_name, raw_args in calls:
        # Allowlist lookup. Not getattr, not eval, not a string the model chose:
        # a name that is not a key here cannot reach any code.
        name = LLM_NAMES.get(raw_name)
        if name is None:
            log.warning("Model asked for a tool that does not exist; ignored")
            continue
        intents.append(Intent(name=name, args=filter_args(name, raw_args), source="llm"))

    writes = tuple(i for i in intents if i.name in WRITE_NAMES)
    held = writes if len(writes) > 1 or any(i.name in PROPOSAL_ONLY for i in writes) else ()
    # The ONLY line in this layer that executes anything, and every held write is
    # filtered out of it.
    lines = [dispatch(i)[0] for i in intents if not (held and i.name in WRITE_NAMES)]
    return held, lines
