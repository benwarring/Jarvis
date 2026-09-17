"""Layer 2: the LLM tool loop, the spend guard and the transport.

NOTHING here may reach OpenAI. Every test stubs at the `Jarvis.integrations.llm`
boundary, and conftest's autouse fixture makes `_client()` raise on top of that, so
a forgotten stub costs nothing and no real key is in the process anyway.

Model output is untrusted input, so the assertions are about what was *called*, not
just what came back: a test that only checked the return value would pass while the
model silently booked a real calendar event.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from Jarvis.agent import tools
from Jarvis.config import get_config
from Jarvis.integrations import gcal
from Jarvis.integrations import llm as transport
from Jarvis.router import llm as router
from Jarvis.router.intents import Intent
from Jarvis.storage.models import record_llm_spend, spend_today
from Jarvis.utils.dates import now_local


def today() -> str:
    """Resolved per call, not at import: config only exists inside a test."""
    return now_local().strftime("%Y-%m-%d")


def reply(text=None, *tool_calls, prompt=10, completion=5):
    return transport.LLMReply(
        text=text, tool_calls=list(tool_calls), prompt_tokens=prompt, completion_tokens=completion
    )


def stub_complete(monkeypatch, *replies):
    """Queue of replies. Asking for one more round than queued fails the test."""
    seen: list[tuple[list, list]] = []

    def fake_complete(messages, tool_schemas):
        seen.append((messages, tool_schemas))
        if len(seen) > len(replies):
            raise AssertionError(f"round {len(seen)} was not budgeted for by this test")
        nxt = replies[len(seen) - 1]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(router, "complete", fake_complete)
    return seen


def stub_always(monkeypatch, one_reply):
    """A transport that never stops asking for tools - for the loop bound."""
    seen: list[list] = []

    def fake_complete(messages, tool_schemas):
        seen.append(messages)
        return one_reply

    monkeypatch.setattr(router, "complete", fake_complete)
    return seen


def raises(exc):
    def _raise(*a, **kw):
        raise exc

    return _raise


@pytest.fixture(autouse=True)
def spend_not_blind(monkeypatch):
    """`router._SPEND_BLIND` is a module global that latches until restart — by design.

    Without this reset the spend-write-failure tests below would latch it for the whole
    pytest process and every later test would get the fallback string instead of an
    answer. Deliberately NOT a fix to the module: latching is what makes the write side
    fail closed.
    """
    monkeypatch.setattr(router, "_SPEND_BLIND", False)


def proposal(out) -> list[Intent]:
    """Unwrap `handle`'s proposal arm, asserting it really is one.

    `handle` returns a tuple either way — `(message, external_id)` or a tuple of
    Intents — so a test that only checked `isinstance(out, tuple)` would pass on the
    answer arm too and quietly stop testing anything.
    """
    assert isinstance(out, tuple) and out, "expected a proposal, got nothing"
    assert all(isinstance(i, Intent) for i in out), f"expected Intents, got {out!r}"
    return list(out)


@pytest.fixture
def dispatched(monkeypatch):
    """Records every Intent that reached the agent manager."""
    calls: list[Intent] = []

    def fake_dispatch(intent):
        calls.append(intent)
        return f"ran {intent.name}", None

    monkeypatch.setattr(router, "dispatch", fake_dispatch)
    return calls


@pytest.fixture
def created(monkeypatch):
    """Google Calendar at the integrations boundary. Never a real calendar."""
    calls: list[tuple] = []
    monkeypatch.setattr(gcal, "create_event", lambda *a, **kw: calls.append((a, kw)))
    return calls


# --- 1. a proposed calendar write is NEVER executed -------------------------


def test_a_model_proposed_calendar_write_is_handed_back_not_dispatched(monkeypatch, dispatched):
    """The load-bearing property of Phase 5. plan.md section 4 / CLAUDE.md: the layer
    that parsed a calendar write never runs it. Delete the PROPOSAL_ONLY branch and
    this fails on `dispatched`, not on the return value."""
    stub_complete(
        monkeypatch, reply(None, ("create_calendar_event", {"title": "dentist", "when": "tomorrow 3pm"}))
    )

    out = router.handle("book me the dentist tomorrow at 3")

    held = proposal(out)
    assert [i.name for i in held] == ["calendar.create"]
    assert held[0].args == {"title": "dentist", "when": "tomorrow 3pm"}
    assert held[0].source == "llm"
    assert dispatched == [], "a calendar write the MODEL parsed must not reach dispatch"


def test_a_proposed_write_does_not_touch_the_calendar_end_to_end(monkeypatch, created):
    """Same property with the real dispatch in the loop: only gcal is stubbed, so a
    proposal that leaked through to its tool would show up here."""
    stub_complete(
        monkeypatch, reply(None, ("create_calendar_event", {"title": "dentist", "when": "tomorrow 3pm"}))
    )

    out = router.handle("book me the dentist tomorrow at 3")

    assert [i.name for i in proposal(out)] == ["calendar.create"]
    assert created == [], "nothing may reach Google Calendar without a confirmation"


def test_two_writes_in_one_reply_confirm_together(monkeypatch, dispatched):
    """plan.md section 4 wants a ✅ on multi-item batches, not just calendar writes.

    Both writes are held, and they are held as ONE batch: the user confirms the pair
    or neither happens. Before this, a single reply carrying three add_task calls ran
    all three unasked — no earlier layer could emit more than one write per message,
    so Layer 2 is where the batch rule first bites.
    """
    stub_complete(
        monkeypatch,
        reply(
            None,
            ("create_calendar_event", {"title": "dentist", "when": "3pm"}),
            ("add_task", {"name": "floss"}),
        ),
    )

    out = router.handle("dentist at 3 and remind me to floss")

    assert [i.name for i in proposal(out)] == ["calendar.create", "task.add"]
    assert dispatched == [], "neither half of a batch runs before the ✅"


def test_a_batch_of_plain_writes_is_held_even_with_no_calendar_write(monkeypatch, dispatched):
    """The batch rule is about COUNT, not about the calendar. Two Notion writes with
    no `calendar.create` anywhere still wait for a ✅."""
    stub_complete(
        monkeypatch,
        reply(None, ("add_task", {"name": "floss"}), ("add_grocery_item", {"item": "milk"})),
    )

    out = router.handle("remind me to floss and add milk")

    assert [i.name for i in proposal(out)] == ["task.add", "grocery.add"]
    assert dispatched == [], "two writes is a batch, calendar or not"


def test_a_lone_write_still_runs_without_asking(monkeypatch, dispatched):
    """The bargain the fast-path already strikes: one write per message, nothing asked.
    Only a batch — or the calendar — needs the ✅."""
    stub_complete(monkeypatch, reply(None, ("add_task", {"name": "floss"})), reply("added floss."))

    out = router.handle("remind me to floss")

    assert out == ("added floss.", None)
    assert [i.name for i in dispatched] == ["task.add"]


def test_a_read_alongside_a_held_write_still_runs(monkeypatch, dispatched):
    """A proposal is not a blanket abort: reads are harmless, and round two answers
    from their output."""
    stub_complete(
        monkeypatch,
        reply(
            None,
            ("list_tasks", {}),
            ("create_calendar_event", {"title": "dentist", "when": "3pm"}),
        ),
    )

    out = router.handle("what's on my list, and book the dentist at 3")

    assert [i.name for i in proposal(out)] == ["calendar.create"]
    assert [i.name for i in dispatched] == ["task.list"], "the read runs; only the write waits"


# --- 2. the spend guard -----------------------------------------------------


def no_api_calls(monkeypatch):
    """A transport that fails the test loudly if the guard lets a request out.

    AssertionError, not LLMError: `handle` swallows LLMError into the same FALLBACK
    the guard returns, so an LLMError here would be indistinguishable from a pass.
    """
    calls: list[str] = []

    def boom(messages, tool_schemas):
        calls.append("called")
        raise AssertionError("over budget: no request may leave the process")

    monkeypatch.setattr(router, "complete", boom)
    return calls


@pytest.mark.parametrize("over_by", [0.0, 0.01, 5.0], ids=["exactly at", "just over", "far over"])
def test_at_or_over_the_daily_limit_layer2_makes_zero_api_calls(monkeypatch, over_by):
    limit = get_config().llm_daily_spend_limit_usd
    record_llm_spend(today(), 1000, 1000, limit + over_by)
    calls = no_api_calls(monkeypatch)

    assert router.handle("what's on today?") == (router.FALLBACK, None)
    assert calls == [], "the guard runs BEFORE the request, so tripping it is free"


def test_under_the_limit_layer2_still_answers(monkeypatch):
    """Guards the other direction: an off-by-one would close Layer 2 permanently."""
    record_llm_spend(today(), 10, 10, get_config().llm_daily_spend_limit_usd - 0.01)
    stub_complete(monkeypatch, reply("you have nothing on."))

    assert router.handle("what's on today?") == ("you have nothing on.", None)


def test_a_spend_counter_that_cannot_be_read_is_treated_as_over_budget(monkeypatch):
    """A spend counter we cannot read is a spend guard that does not exist."""
    monkeypatch.setattr(router, "spend_today", raises(RuntimeError("db gone")))
    calls = no_api_calls(monkeypatch)

    assert router.handle("what's on today?") == (router.FALLBACK, None)
    assert calls == [], "an unreadable counter must fail closed, not open"


def test_every_round_is_billed_before_the_answer_comes_back(monkeypatch, dispatched):
    """Spend is recorded per call, so a crash later still leaves it counted."""
    assert spend_today(today()) == 0.0
    stub_complete(monkeypatch, reply(None, ("list_tasks", {})), reply("nothing due."))

    router.handle("anything due?")

    assert spend_today(today()) == pytest.approx(2 * transport.cost_usd(10, 5))


def test_a_spend_write_that_fails_does_not_take_the_answer_down(monkeypatch):
    """Billing is best effort; losing the counter must not lose the user's reply."""
    monkeypatch.setattr(router, "record_llm_spend", raises(RuntimeError("disk full")))
    stub_complete(monkeypatch, reply("still here."))

    assert router.handle("hi") == ("still here.", None)


# --- 3. untrusted model output ----------------------------------------------


def test_a_hallucinated_tool_name_never_reaches_dispatch(monkeypatch, dispatched):
    stub_complete(monkeypatch, reply(None, ("delete_everything", {"path": "/"})))

    assert router.handle("do the thing") == (router.FALLBACK, None)
    assert dispatched == [], "a name outside LLM_NAMES cannot reach any code"


def test_a_dotted_registry_key_is_not_a_valid_tool_name(monkeypatch, dispatched):
    """The model is shown `add_task`, never `task.add`. The allowlist is keyed on
    what the model was told, so the internal name must not be a second door."""
    stub_complete(monkeypatch, reply(None, ("task.add", {"name": "x"})))

    router.handle("add a task")
    assert dispatched == []


def test_a_known_tool_still_runs_when_a_hallucinated_one_is_alongside_it(monkeypatch, dispatched):
    stub_complete(monkeypatch, reply(None, ("sudo_rm", {}), ("list_tasks", {})), reply("that's the lot."))

    router.handle("what's on my list?")
    assert [i.name for i in dispatched] == ["task.list"]


def test_an_invented_argument_is_dropped_before_the_intent_is_built(monkeypatch, dispatched):
    stub_complete(monkeypatch, reply(None, ("add_task", {"name": "floss", "sudo": True})), reply("done."))

    router.handle("remind me to floss")

    assert dispatched[0].args == {"name": "floss"}, "an undeclared argument must never reach a tool"


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("task.add", {"name": "x", "sudo": True}, {"name": "x"}),
        ("task.add", {"name": "x", "due": "friday"}, {"name": "x", "due": "friday"}),
        ("grocery.list", {"item": "milk"}, {}),
        ("calendar.create", {"title": "t", "when": "3pm", "attendees": ["a"]}, {"title": "t", "when": "3pm"}),
        ("task.list", {}, {}),
    ],
)
def test_filter_args_keeps_only_what_the_tool_declares(name, args, expected):
    assert tools.filter_args(name, args) == expected


# --- 4. the loop bound ------------------------------------------------------


def test_the_loop_stops_at_max_rounds(monkeypatch, dispatched):
    """An unbounded loop is a runaway bill. A model that always asks for another
    tool must not buy a third round."""
    seen = stub_always(monkeypatch, reply(None, ("list_tasks", {})))

    out = router.handle("what's on my list?")

    assert router.MAX_ROUNDS == 2
    assert len(seen) == router.MAX_ROUNDS, "exactly MAX_ROUNDS API calls, never more"
    assert out == ("ran task.list", None), "the last tool output answers, rather than a third round"


def test_a_failed_spend_write_shuts_layer_2_until_restart(monkeypatch, dispatched):
    """The write side fails closed too, not just the read side.

    The guard compares against a counter that only grows if these writes land, so a
    broken write would leave Layer 2 spending against a frozen number — unbounded,
    and invisible. The asymmetry mattered: reads already failed closed, and the one
    direction still open was the one that kept spending.
    """
    monkeypatch.setattr(router, "record_llm_spend", raises(RuntimeError("disk full")))
    stub_complete(monkeypatch, reply("answered anyway."))

    # The call is already paid for, so the user still gets this one.
    assert router.handle("first") == ("answered anyway.", None)
    assert router._SPEND_BLIND is True

    # The NEXT one is refused, and buys nothing: this transport fails the test if called.
    monkeypatch.setattr(router, "complete", raises(AssertionError("spent while blind")))
    assert router.handle("second") == (router.FALLBACK, None)


def test_round_two_is_handed_no_tools_at_all(monkeypatch, dispatched):
    """Indirect prompt injection, and the reason round 2 is prose-only.

    Round 1's tool output is replayed to the model, and it can carry a Notion task or
    grocery title — text anyone with access to that database can write, not just the
    person who sent the message. With schemas still attached, a crafted title could
    talk the model into a write (`grocery.add`, `task.complete` — none of them
    proposal-only) that executes with no ✅. No tools on round 2, no write.
    """
    seen = stub_complete(monkeypatch, reply(None, ("list_tasks", {})), reply("one thing: floss."))

    router.handle("what's on my list?")

    assert seen[0][1] == tools.TOOL_SCHEMAS, "round one still needs tools to be useful"
    assert seen[1][1] == [], "round two must not be able to call anything"


def test_a_text_answer_ends_the_loop_early(monkeypatch):
    seen = stub_complete(monkeypatch, reply("you're free all day."))

    assert router.handle("busy today?") == ("you're free all day.", None)
    assert len(seen) == 1, "no tool call means no second round to pay for"


def test_tool_output_is_fed_back_for_the_second_round(monkeypatch, dispatched):
    seen = stub_complete(monkeypatch, reply(None, ("list_tasks", {})), reply("one thing: floss."))

    assert router.handle("what's on my list?") == ("one thing: floss.", None)
    assert len(seen) == 2
    assert "ran task.list" in seen[1][0][-1]["content"], "round two has to see what round one found"


# --- 5. failure degrades, it does not propagate -----------------------------


def test_an_llm_error_degrades_to_the_plain_fallback(monkeypatch):
    stub_complete(monkeypatch, transport.LLMError("provider is down"))

    assert router.handle("anything?") == (router.FALLBACK, None)


def test_an_llm_error_on_the_second_round_degrades_too(monkeypatch, dispatched):
    stub_complete(monkeypatch, reply(None, ("list_tasks", {})), transport.LLMError("provider is down"))

    assert router.handle("what's on my list?") == (router.FALLBACK, None)


def test_an_empty_answer_becomes_the_fallback(monkeypatch):
    stub_complete(monkeypatch, reply(None))

    assert router.handle("...") == (router.FALLBACK, None)


def test_the_system_prompt_and_the_user_text_are_what_goes_out(monkeypatch):
    seen = stub_complete(monkeypatch, reply("ok"))

    router.handle("add milk to the list")

    messages, schemas = seen[0]
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "add milk to the list"}
    assert schemas is tools.TOOL_SCHEMAS, "the model only ever sees the registry's own schemas"


# --- 6. cost arithmetic -----------------------------------------------------


@pytest.mark.parametrize(
    "prompt_tokens, completion_tokens, model, expected",
    [
        (1_000_000, 0, "gpt-4.1-mini", 0.40),
        (0, 1_000_000, "gpt-4.1-mini", 1.60),
        (1000, 500, "gpt-4.1-mini", 0.0012),
        (0, 0, "gpt-4.1-mini", 0.0),
        (1_000_000, 1_000_000, "gpt-4.1", 10.00),
    ],
)
def test_cost_usd(prompt_tokens, completion_tokens, model, expected):
    assert transport.cost_usd(prompt_tokens, completion_tokens, model) == pytest.approx(expected)


def test_an_unpriced_model_over_charges_rather_than_costing_nothing():
    """A zero here would silently disable the spend guard - the one failure nobody
    would notice until the bill arrived."""
    unknown = transport.cost_usd(1000, 1000, "gpt-9-launched-next-tuesday")
    assert unknown > 0
    assert unknown >= transport.cost_usd(1000, 1000, "gpt-4.1"), "an unknown model prices at the ceiling"


def test_cost_usd_defaults_to_the_configured_model(fake_env):
    assert transport.cost_usd(1000, 500) == transport.cost_usd(1000, 500, fake_env["OPENAI_MODEL"])


# --- the transport itself ---------------------------------------------------


def fake_openai(content=None, tool_calls=None, prompt_tokens=7, completion_tokens=3):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )
    sent: dict = {}

    def create(**kwargs):
        sent.update(kwargs)
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, sent


def openai_tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def test_complete_parses_a_text_answer(monkeypatch):
    client, sent = fake_openai(content="you're free.")
    monkeypatch.setattr(transport, "_client", lambda: client)

    out = transport.complete([{"role": "user", "content": "busy?"}], tools.TOOL_SCHEMAS)

    assert out == transport.LLMReply(text="you're free.", tool_calls=[], prompt_tokens=7, completion_tokens=3)
    assert sent["model"] == get_config().openai_model, "the model id comes from config, never a literal"
    assert sent["tool_choice"] == "auto"


def test_complete_parses_tool_calls(monkeypatch):
    client, _ = fake_openai(tool_calls=[openai_tool_call("add_task", '{"name": "floss"}')])
    monkeypatch.setattr(transport, "_client", lambda: client)

    out = transport.complete([], tools.TOOL_SCHEMAS)
    assert out.tool_calls == [("add_task", {"name": "floss"})]
    assert out.text is None


@pytest.mark.parametrize(
    "arguments",
    ["{not json", "[1, 2]", '"a string"', "null"],
    ids=["unparseable", "valid json wrong shape", "json scalar", "json null"],
)
def test_complete_drops_a_malformed_tool_call_instead_of_guessing(monkeypatch, arguments):
    client, _ = fake_openai(
        tool_calls=[openai_tool_call("add_task", arguments), openai_tool_call("list_tasks", "{}")]
    )
    monkeypatch.setattr(transport, "_client", lambda: client)

    assert transport.complete([], tools.TOOL_SCHEMAS).tool_calls == [("list_tasks", {})]


def test_absent_arguments_mean_no_arguments_not_a_dropped_call(monkeypatch):
    """OpenAI omits `arguments` for a no-parameter tool, so this is well-formed."""
    client, _ = fake_openai(tool_calls=[openai_tool_call("list_tasks", None)])
    monkeypatch.setattr(transport, "_client", lambda: client)

    assert transport.complete([], tools.TOOL_SCHEMAS).tool_calls == [("list_tasks", {})]


def test_complete_sends_no_tools_key_when_there_are_none(monkeypatch):
    client, sent = fake_openai(content="hi")
    monkeypatch.setattr(transport, "_client", lambda: client)

    transport.complete([], [])
    assert "tools" not in sent and "tool_choice" not in sent


def test_a_provider_failure_becomes_a_user_safe_llm_error(monkeypatch, caplog, fake_env):
    """An OpenAI error body echoes the request, and the request carries the key.
    Neither the key nor the prompt may reach the exception or a log line."""
    secret = fake_env["OPENAI_API_KEY"]

    def explode(**kwargs):
        raise RuntimeError(f"401 unauthorized for key {secret} on prompt 'my private calendar'")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    monkeypatch.setattr(transport, "_client", lambda: client)

    with caplog.at_level("ERROR"):
        with pytest.raises(transport.LLMError) as exc:
            transport.complete([{"role": "user", "content": "my private calendar"}], [])

    assert secret not in str(exc.value)
    assert "my private calendar" not in str(exc.value)
    assert secret not in caplog.text and "my private calendar" not in caplog.text
    assert exc.value.__cause__ is None, "the provider traceback would carry the request body"
    assert "slash command" in str(exc.value), "the user gets a working alternative"


def test_missing_usage_counts_as_zero_rather_than_crashing(monkeypatch):
    """A response with no usage block must not take the bot down mid-answer."""
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))],
                    usage=None,
                )
            )
        )
    )
    monkeypatch.setattr(transport, "_client", lambda: client)

    out = transport.complete([], [])
    assert (out.prompt_tokens, out.completion_tokens) == (0, 0)
