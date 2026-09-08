# Standards Review — 2026-09-07

Scope: Phases 1 & 2 only (skeleton; Agent Manager + Notion + fast-path). Google
Calendar, LLM, weather, reminders, scheduler are later phases and are not judged
for absence. Reviewed all of `Jarvis/` and `tests/`, `plan/plan.md`, `CLAUDE.md`,
and `contract.md`. Ran `python -m pytest tests/ -q`: 142 passed, 4 failed (all four
are pre-known bugs, confirmed below, not review artifacts).

---

## Findings

### HIGH

**H1 — `parse_when("in N hours/minutes", ...)` is off by an hour across a DST
transition.**
File: `Jarvis/utils/dates.py:106-108`.
```python
m = _RELATIVE.search(low)
if m:
    return now + timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
```
`now` is a `zoneinfo`-aware datetime. Adding a `timedelta` to it does **wall-clock**
field arithmetic, not elapsed-real-time arithmetic — the `ZoneInfo` offset is
re-derived from the *new* wall-clock fields, so a fall-back or spring-forward
inside the interval silently gains or loses an hour. Confirmed failing:
`tests/unit/test_dates.py::test_in_n_hours_is_n_real_hours_across_a_dst_boundary`
(both fall-back and spring-forward cases).
Fix: route minute/hour deltas through UTC before adding, e.g.
```python
unit = _UNITS[m.group(2)]
delta = timedelta(**{unit: int(m.group(1))})
if unit in ("minutes", "hours"):
    return (now.astimezone(timezone.utc) + delta).astimezone(_tz())
return now + delta   # day/week: wall-clock semantics is the correct behavior here
```
Keep day/week on wall-clock arithmetic ("in 3 days" should mean the same
time-of-day three calendar days later, not exactly 72 hours) — only hours/minutes
need the UTC round-trip.

---

### MEDIUM

**M1 — `router/fastpath.py` matches "got a minute?" / "got any plans?" as a
grocery check-off.**
File: `Jarvis/router/fastpath.py:32, 92-99`. `_GROCERY_GOT` / `_TASK_DONE` have no
trailing-`?` guard, unlike the bare-line branch two lines below which explicitly
excludes `"?" in raw`. Confirmed failing:
`tests/unit/test_fastpath.py::test_a_question_is_never_a_check_off`.
Fix: reuse the same guard already used for the bare-line branch, e.g.
```python
m = _GROCERY_GOT.match(low) if "?" not in raw else None
...
m = _TASK_DONE.match(low) if "?" not in raw else None
```

**M2 — `TOOLS` callables return only `str`, so `external_id` is always `None`.**
File: `Jarvis/agent/tools.py:34-59`, consumed at `Jarvis/bot/handlers.py:61`
(`record_message(message.id, intent.name, intent.args, None)` — hard-coded).
`grocery_add`/`task_add` call `notion.add_grocery`/`notion.add_task`, both of
which return the created page id, and then discard it. Plan §6 says the
`messages.external_id` column is what "powers undo and idempotency," and it's
being populated with `None` on every write today, in Phase 2, not just once undo
(Phase 9) ships.
**Ruling:** the `Callable[..., str]` contract is too narrow to carry this; it has
to change. Smallest fix that doesn't regress the plain-string ergonomics used
everywhere else: return `tuple[str, str | None]` (message, external_id) from the
handful of tools that create a Notion page (`grocery_add`, `task_add`), keep the
rest returning a bare `str`, and have `dispatch`/`handlers._run` unwrap a tuple
when they get one. Whoever owns the contract should make this call explicitly
rather than each tool inventing its own convention.

**M3 — `agent/tools.py` imports `bot/formatting.py`, inverting the architecture.**
File: `Jarvis/agent/tools.py:13`. Plan §3's runtime diagram is one-directional:
`bot -> agent -> router -> integrations`. `agent/tools.py` reaching up into
`bot/formatting.py` for `format_tasks`/`format_groceries` runs that arrow
backwards — the capability layer now depends on the presentation layer.
**Ruling on the specific question asked:** `bot/client.py`'s function-local
imports (`Jarvis/bot/client.py:44-45`) are the *right* call, but for an unrelated
reason — they break a real cycle (`client -> commands.{todo,grocery} ->
handlers -> client`, from `build_bot` needing to register command groups that
themselves need `handlers.respond`, which needs `client.is_owner`). That cycle
exists independent of `formatting.py` and is correctly resolved with local
imports; no change needed there. Separately, and this is the actual bug:
**`formatting.py` is in the wrong package.** `format_tasks`/`format_groceries`
are plain-string renderers of `Task`/`Grocery` — capability-layer concerns, not
Discord-embed concerns (the docstring even says "No embeds until Phase 4 needs
them"). Move them next to the dataclasses they render (`integrations/notion.py`)
or a neutral `utils/format.py`, and leave `bot/formatting.py` empty for the
embed-specific work Phase 4 actually needs.

**M4 — Plan §4's "confirmed by reaction, not by prose" is stale.**
File: `Jarvis/bot/handlers.py:48-54` vs. `plan/plan.md:187-189`. The plan states
fast-path parses are confirmed by ✅ reaction "rather than replying." The code
reacts ✅ and *also* replies with the result for every intent except `.add`
(there's a `ponytail:` comment explaining why: a silent check-off that matched
nothing, or matched the wrong item, would otherwise be invisible). This is a
reasonable, deliberate divergence — but CLAUDE.md requires the plan be updated in
the same commit as the code that diverges from it, and that didn't happen.
**Ruling:** keep the code, update the plan. Reaction-only for adds; reaction +
one-line reply for list/check/complete is more correct than the plan's blanket
rule (a list intent can't be conveyed by an emoji at all — the plan simply didn't
account for it).

**M5 — Notion Tasks `Project` property is entirely unimplemented.**
Plan §6 (`plan/plan.md:268`) specifies `Project` (Select) on the Tasks database.
It is absent from the contract's `add_task` signature, the `Task` dataclass, and
`Jarvis/integrations/notion.py` end to end — not read, not written, not even a
dead field. This has no runtime symptom yet (nothing feeds it), but it's a silent
plan/contract divergence per this review's instructions to flag those explicitly.
Fix: add `project: str | None = None` to `Task` and to `add_task`'s keyword args,
set `props["Project"] = {"select": {"name": project}}` when provided, and read it
back in `list_open_tasks` via the existing `_select` helper.

**M6 — Plan §4's `/todo done <n>` / `/grocery got <n>` (numeric index) vs. actual
free-text substring query.**
`Jarvis/bot/commands/todo.py:27-30`, `Jarvis/bot/commands/grocery.py:27-30`, and
`Jarvis/agent/tools.py:20-31` (`_pick`) all implement and thoroughly test a
case-insensitive substring match with explicit ambiguity refusal, matching the
contract exactly.
**Ruling:** change the plan, not the code. A numeric index is fragile (shifts
under you the moment something else on the list gets checked off between
`/list` and `/done`) and inconsistent with the fast-path's own `done: <text>` /
`got <text>` phrasing. The free-text-with-refuse-to-guess design already
implemented is the better one. Update plan §4's command table to
`/todo done <query>` / `/grocery got <query>`.

**M7 — `.env` ships `DATABASE_PATH`, the contract names `DB_PATH`.**
`Jarvis/config.py:81`:
```python
db_path=os.getenv("DB_PATH", "").strip() or os.getenv("DATABASE_PATH", "").strip() or "jarvis.db",
```
**Ruling:** standardize on `DB_PATH` — it's what the contract specifies, what
`Config`'s docstring comment leads with, and what `tests/conftest.py` sets.
Rename the key in `.env` to `DB_PATH` and delete the `DATABASE_PATH` fallback
so there's exactly one accepted name, per "config comes from `Jarvis/config.py`"
and not a guessing game between two env files.

---

### LOW

**L1 — `bot/formatting.py`'s `_when()` contradicts `to_local()`'s documented
contract for naive input.**
`Jarvis/utils/dates.py:49-53` states plainly: "Naive input is assumed to be UTC,
because that is how we store it." `Jarvis/bot/formatting.py:15-17`:
```python
def _when(dt: datetime) -> str:
    local = to_local(dt) if dt.tzinfo else dt
    return local.strftime(...)
```
skips the conversion for naive input, formatting it as if it were *already*
local — the opposite assumption. **Ruling: `to_local()` is right** (matches
"store UTC, render local" and its own docstring); `_when` is wrong.
Currently harmless in practice — the only producer of `Task.due`/`Grocery`
datetimes is `notion._date()`, which always routes through `to_local()` and so
always hands `_when` a tz-aware value — but it's a trap for whenever Phase 3's
Google Calendar integration adds a second datetime source. Fix: delete the
branch, always call `to_local(dt)`.

**L2 — `complete_task`/`check_off_grocery` are genuine duplication; the rest of
`notion.py`'s pairs are not.**
`Jarvis/integrations/notion.py:189-196, 239-245` are byte-for-byte the same
shape — a single-property `pages.update` wrapped in `_call`, differing only in
the literal property patch. A two-line `_set_property(what, page_id, prop)`
helper would remove that duplication cheaply. By contrast, `add_task`/
`add_grocery` and `list_open_tasks`/`list_groceries` only *look* similar —
their property schemas genuinely differ (Task has five optional properties,
Grocery has one) — extracting those would trade six clear lines for a generic
props-builder that's harder to read at the call site. Judgment: fix the first,
leave the second two alone. Not blocking either way.

**L3 — `config.py`'s docstring overstates the `os.environ` rule by one exception.**
`Jarvis/config.py:1`: "Reads .env once; no other module touches os.environ."
`Jarvis/utils/logging.py:15-18` does, deliberately and for a good reason (see
Rule E below). Harmless, but the docstring reads as a guarantee it doesn't quite
keep. Low-value fix: add a three-word parenthetical pointing at the logging
exception, or don't bother — this is the kind of thing not worth a diff on its
own.

**L4 — No unit test exercises `bot/handlers.on_message` end-to-end.**
`parse`, `dispatch`, and `record_message` are each well-tested in isolation
(`test_fastpath.py`, `test_manager.py`, `test_storage.py`), but nothing threads
them together through `on_message` itself — the owner/bot-ignore guard
(`Jarvis/bot/handlers.py:37`) and the reaction/reply split (M4) have no test
double covering them. A duck-typed message stub (author id, channel id,
`.content`, `.add_reaction`, `.reply` as `AsyncMock`s) would cover this cheaply.
Not blocking for Phase 2 — the pieces it wires together are already solid.

---

## Explicit rulings on the pre-flagged items

- **`/todo done <n>` vs free-text query:** plan changes, code stays (M6).
- **`TOOLS` returning only `str` / `external_id` always `None`:** real defect,
  contract needs to grow a way to carry an id back (M2).
- **`DATABASE_PATH` vs `DB_PATH`:** pick `DB_PATH`, delete the fallback (M7).
- **`to_local()` vs `formatting.py`'s naive-value assumption:** `to_local()` is
  correct; `formatting.py` is the one to fix (L1).
- **`utils/logging.py`'s `os.getenv("LOG_LEVEL")` — should `Config` gain a
  `log_level` field?** No. `get_logger()` runs at *import* time in several
  modules (`notion.py`, `manager.py`, `client.py`, `handlers.py` all call it at
  module scope), which in `main.py`'s own import chain happens **before**
  `get_config()` is ever invoked in `main()`. Routing `LOG_LEVEL` through
  `Config` would make logger creation trigger full env validation at arbitrary,
  early import points — breaking test collection (`tests/conftest.py`'s
  autouse fixture sets env vars per-test, *after* pytest has already imported
  every test module at collection time) and risking a confusing `SystemExit`
  from deep inside an unrelated import in production. The narrow,
  `ponytail:`-commented exception reading only `LOG_LEVEL` with a safe default
  is the correct, deliberate design. Leave it.
- **`requirements.txt` pins `anthropic`, no OpenAI client:** not blocking.
  Confirmed nothing under `Jarvis/` imports `anthropic` or `openai` — Phase 2
  never touches the LLM. Correctly called out in plan §10 as "resolve before
  Phase 5," not before.

---

## Clean areas (no finding)

- **Directory layout** matches plan §3 exactly for everything built; no files
  exist outside the Phase 1/2 assignment (confirmed via full directory listing).
- **Every Phase-2 fast-path pattern in plan §4's table** exists in
  `router/fastpath.py` and is tested, including the intentionally-deferred rows
  (`agenda.read`, `weather.read`) correctly appearing only in the MISSES list.
- **Notion property names** (`Name`/`Status`/`Due`/`Priority`/`Estimate`/`Notes`/
  `Source` for Tasks; `Item`/`Qty`/`Category`/`Got it` for Groceries) match plan
  §6 exactly, aside from `Project` (M5).
- **One implementation per capability:** `bot/commands/todo.py` and
  `bot/commands/grocery.py` contain zero business logic — each command builds an
  `Intent` and calls `respond` -> `dispatch` -> `TOOLS[...]`. No second
  implementation of any operation exists anywhere in `bot/` or `router/`.
- **`todo.py` / `grocery.py`'s near-identical shape is genuine parallel
  structure, not duplication worth extracting.** Different domains, different
  arg shapes (`text`/`due` vs. `item`/`qty`), and discord.py's `app_commands`
  decorators want to be literal, introspectable functions — a generic factory
  would fight the framework to save about ten lines. Leave it.
- **The six `TOOLS` callables** already share their one real piece of common
  logic — substring resolution — through the extracted `_pick` helper
  (`agent/tools.py:20-31`), used identically by `grocery_check` and
  `task_complete`. This is exactly the kind of repetition that should have been
  extracted, and it was.
- **Test coverage** matches plan §8's Phase 1/2-relevant high-value targets:
  every fast-path pattern plus the near-misses, DST boundary conversion (both
  the wall-clock-preserving case and the elapsed-time case that catches H1),
  and grocery keyword-collision cases. Plus solid coverage beyond the spec:
  config validation (all-keys-named-at-once, no-echo-on-bad-value), dispatch
  safety (never raises, never leaks an internal exception string), and storage
  idempotency across a simulated restart.

---

## Verdict on the four review questions

1. **Unnecessarily repeated blocks of code?** One small one (L2,
   `complete_task`/`check_off_grocery`), optional. Everything else that looks
   repeated is parallel structure across genuinely different domains and
   shouldn't be collapsed.
2. **Helper functions for modularity?** Yes, used well — `_pick` in
   `agent/tools.py`, `_call`/`_title`/`_text`/`_select`/`_date` in
   `integrations/notion.py`, `_split_qty`/`_split_due` in `router/fastpath.py`.
   The one modularity miss is layering, not helper-function discipline:
   `formatting.py` sits in the wrong package (M3).
3. **Does the code do what it promises?** Mostly yes. Two concrete breaks:
   H1 (DST) and M1 ("got a minute?"), both already covered by failing tests.
   One docstring is slightly optimistic (L3).
4. **Does the project match the plan?** Close, with six documented gaps
   (M2, M4, M5, M6, M7, plus H1/M1 as bugs rather than gaps) — none of them
   contradict the Phase 1/2 scope, and three of the six should be resolved by
   editing the plan rather than the code (M4, M6, and the reaction-vs-prose
   wording), since the code's behavior is the more defensible one.

---

# Review Pass 2 — 2026-09-07

Scope: same as pass 1 (Phases 1 & 2 only). Read `plan/plan.md` fresh (it moved
since pass 1 — §4, §6, and the §3 module map all changed), the revised
`contract.md`, `security-log.md`'s findings, and all of `Jarvis/` and `tests/`.
Ran `python -m pytest tests/ -q`: **151 passed**, 0 failed.

## Pass-1 findings, verified one by one

**H1 (DST) — RESOLVED.** `Jarvis/utils/dates.py:111-117`: the `_RELATIVE` branch
now checks `unit in ("minutes", "hours")` and routes through
`now.astimezone(timezone.utc) + delta` before converting back with
`.astimezone(_tz())`; day/week fall through to plain wall-clock `now + delta`,
with a comment explaining why that's still correct. Both DST tests pass
(`test_in_n_hours_is_n_real_hours_across_a_dst_boundary`,
`test_wall_clock_time_survives_a_dst_boundary`), and the module's own
`__main__` self-check (lines 142-147) independently re-derives the same
fall-back/spring-forward cases. Confirmed fixed at the root — the fix lives in
the one function every caller of relative-time parsing goes through, not
patched per call site.

**M1 (question guard) — RESOLVED.** `Jarvis/router/fastpath.py:99`: a single
`if "?" not in raw:` now wraps both the `_GROCERY_GOT` and `_TASK_DONE` match
attempts (lines 100-107), replacing the old ungated pair. `test_a_question_is_never_a_check_off`
passes for both `"got a minute?"` and `"got any plans?"`. One guard, both
branches — this is the shared-function fix the finding asked for, not two
separate patches.

**M2 (`external_id` always NULL) — RESOLVED, and matches the authorized
contract exactly.** `Jarvis/agent/manager.py:13` is
`dispatch(intent: Intent) -> tuple[str, str | None]`; it unwraps a tuple result
or normalizes a bare `str` to `(result, None)` at line 31. `Jarvis/agent/tools.py`:
`grocery_add` (line 73) and `task_add` (line 91) return `tuple[str, str | None]`
carrying the real Notion page id; `grocery_list`, `grocery_check`, `task_list`,
`task_complete` return a bare `str`. `Jarvis/bot/handlers.py:48` destructures
`result, external_id = await _run(intent)` and passes the real value into
`record_message` at line 65 — no hard-coded `None` remains anywhere in that
path. `tests/unit/test_manager.py`'s four id-specific tests
(`test_grocery_add_carries_the_notion_page_id`,
`test_task_add_carries_the_notion_page_id`,
`test_a_read_has_no_external_id`, `test_a_check_off_has_no_external_id`) each
monkeypatch a distinct return value and assert on it — these are not
placeholder tests, they'd fail immediately if `dispatch` reverted to discarding
the id or if any tool forgot to plumb it through.

**M3 (layering inversion) — RESOLVED, and better than the fix I proposed.**
I had suggested relocating `format_tasks`/`format_groceries` to
`integrations/notion.py` or a new `utils/format.py`. The ruling instead deleted
`bot/formatting.py` outright and made `_when`/`_format_tasks`/`_format_groceries`
private functions inside `Jarvis/agent/tools.py` (lines 26-56), called only by
`grocery_list`/`task_list` in the same file. I judged whether this "merely
relocates" the inversion, per the brief's instruction to check: it does not.
The original bug was `agent/tools.py` importing *from* `bot/formatting.py`,
running the `bot -> agent` arrow backwards. After the fix there is no import in
either direction between those two files at all — `bot/handlers.py` never
touches formatting, it just receives an already-built string back from
`dispatch`. Grepped the whole tree for `formatting` and `bot.formatting`:
zero hits outside the plan and contract prose. This is a strictly better
outcome than my original suggestion, since it avoids inventing a new module for
two 15-line private helpers with exactly one caller each — no speculative
`utils/format.py` needed.

**LOW (`_when` naive datetimes) — RESOLVED.** `Jarvis/agent/tools.py:26-28`:
`_when` now calls `local = to_local(dt)` unconditionally, with the branch that
special-cased naive input removed entirely. Matches `to_local`'s documented
"naive input is assumed UTC" contract with no exception left to contradict it.

**LOW (notion duplication) — RESOLVED, matches the ruling exactly.**
`Jarvis/integrations/notion.py:189-190` adds `_set_property(what, page_id,
prop)`, and `complete_task`/`check_off_grocery` (lines 193-194, 238-239) are
now one-line calls into it. `add_task`/`add_grocery` and the two list functions
were correctly left alone — their property schemas still genuinely differ (five
optional Task properties vs. one Grocery flag), so merging them would still
trade clear code for a generic props-builder. No new duplication introduced.

**M7 (`DB_PATH` vs `DATABASE_PATH`) — RESOLVED.** `Jarvis/config.py:90`:
`db_path=os.getenv("DB_PATH", "").strip() or "jarvis.db"` — the `DATABASE_PATH`
fallback is gone, exactly one accepted key name remains, matching the contract
and `tests/conftest.py`'s `DB_PATH` env var. Per the brief, the user's `.env`
still needing a rename is a user action, not re-reported here.

### Plan edits, checked against the code they describe

**M6 — RESOLVED.** `plan/plan.md:169-170,187-190` now reads `/todo done <query>`
/ `/grocery got <query>` with the fragility-of-an-index rationale inline. Matches
`Jarvis/bot/commands/todo.py`'s `done(interaction, query: str)` and
`grocery.py`'s `got(interaction, query: str)` exactly, and matches
`agent/tools.py`'s `_pick`-based substring resolution. No drift in either
direction.

**M4 — RESOLVED.** `plan/plan.md:192-198` now states adds are reaction-only
while list/check-off/complete react ✅ *and* reply one line, with the rationale
("an emoji cannot carry a list... exactly the failure you need to see").
`Jarvis/bot/handlers.py:58` (`if not intent.name.endswith(".add"):`) implements
precisely that split. Plan and code agree.

**M5 — RESOLVED.** `plan/plan.md:278` marks `Project` "Set by hand in Notion —
Jarvis neither reads nor writes it yet." Checked the code end to end again:
no `project` field in `Task` (`Jarvis/integrations/notion.py:64-71`), not in
`add_task`'s signature (line 128), not read in `list_open_tasks`. Plan now
accurately describes what the code does, rather than describing a feature that
doesn't exist — this is the "plan edited to match reality" case done correctly,
not the "plan edited to paper over a bug" case the brief warned against, since
the code genuinely never claims to touch `Project`.

**Module map (`bot/formatting.py` "arrives in Phase 4") — RESOLVED.**
`plan/plan.md:127` now says exactly that, and the file does not exist under
`Jarvis/bot/` (confirmed via directory listing — only `client.py`, `handlers.py`,
`commands/todo.py`, `commands/grocery.py`, `__init__.py`). Plan and directory
listing agree.

### Findings carried forward, not mandated for this pass

- **L3** (config.py docstring overstates the `os.environ` rule by the
  `utils/logging.py` exception) — still present verbatim
  (`Jarvis/config.py:1`), unchanged. Pass 1 explicitly ruled this "not worth a
  diff on its own"; it wasn't in the fix-wave list, and it's still true today.
  Not re-flagged as a defect, just noted as intentionally left.
- **L4** (no end-to-end test of `bot/handlers.on_message`) — still absent;
  grepped `tests/` for `handlers`/`on_message`, no hits. Pass 1 ruled this
  "not blocking for Phase 2." It remains the one named §8 target area
  (owner-gate + reaction/reply split) with no direct test double, though both
  halves it would cover are now individually well-tested (`is_owner` in
  `test_config.py`, the reaction/reply split's underlying `.add`-suffix
  condition is simple enough that `test_manager.py`'s tuple tests plus manual
  reading cover its logic, if not the discord.py wiring itself). Optional,
  still not blocking.

## New findings this pass

None. I looked specifically for: regressions introduced by the fix wave itself
(re-checked `_QTY`/`_DUE` gaining `re.IGNORECASE` against the fact that they
now match un-lowercased text sliced via `_arg`'s match-span trick — correct,
since `str.lower()` preserves both length and character-index alignment for
the ASCII text this router handles); a lingering `bot/formatting` import
anywhere (grepped, zero); whether `Config.__repr__`'s redaction set matches
the actual two secret fields (it does — `discord_bot_token`, `notion_token`);
and whether the tuple-normalization in `dispatch` could silently swallow a
tuple of the wrong shape from a future third tool (it can't today — only two
tools return tuples, and `isinstance(result, tuple)` is the only branch,
correctly matching the contract's exact two-shape design). Nothing turned up.
The codebase is clean of the pass-1 list.

## Verdict on the four review questions (pass 2)

1. **Unnecessarily repeated blocks of code?** No open findings. L2's fix
   removed the one real duplication; everything else remains parallel
   structure across genuinely different domains, as judged in pass 1.
2. **Helper functions for modularity?** Yes. `_set_property` closes the gap
   pass 1 found; `_pick`, `_call`/`_title`/`_text`/`_select`/`_date`/`_arg`/
   `_split_qty`/`_split_due` are all still doing real work with no dead
   abstractions added during the fix wave.
3. **Does the code do what it promises?** Yes — H1 and M1 were the two
   concrete promise-breaks found in pass 1, both fixed and tested; nothing new
   surfaced.
4. **Does the project match the plan?** Yes, in both directions. Every code
   change from the fix wave has a corresponding, accurate plan passage (M2,
   M3 area, the module map), and every plan-only edit (M4, M5, M6) describes
   what the code actually does rather than aspirational or stale behavior.

## Phase-3 readiness

**Ready.** All seven pass-1 findings (H1, M1, M2, M3, M7, plus the two LOW
items) are resolved with evidence, not just asserted; the three plan-only
edits (M4, M5, M6) accurately describe current code rather than papering over
it; 151/151 tests pass, including four new tests
(`test_grocery_add_carries_the_notion_page_id`,
`test_task_add_carries_the_notion_page_id`, `test_a_read_has_no_external_id`,
`test_a_check_off_has_no_external_id`) and `test_repr_redacts_the_tokens` that
are genuinely load-bearing — each would fail if the fix it guards regressed.
No new findings. Phase 3 (Calendar read) can build on this without first
touching Phase 1/2 code.

---

# Cycle 2 Review — Phases 3 & 4 — 2026-09-07

Scope: Phase 3 (Google Calendar read — `/agenda`) and Phase 4 (Google Calendar
write — `/event` behind ✅/❌). Read `plan/plan.md` fresh (§3 and §4 both edited
this cycle), the Phase 3/4 contract addendum, and all new/changed code:
`integrations/gcal.py`, `bot/handlers.py`, `bot/formatting.py` (back),
`bot/commands/agenda.py` + `event.py`, `bot/client.py`, `agent/tools.py`,
`agent/manager.py`, `config.py`, `router/fastpath.py`, `router/intents.py`,
`utils/dates.py`, plus `tests/unit/test_gcal.py`, `test_confirmation.py`, and
the calendar additions to `test_tools.py` / `test_manager.py`. Ran
`python -m pytest tests/ -q`: **376 passed**, 0 failed.

## Findings

### MEDIUM

**C2-M1 — Two independent literal defaults for the event duration; only one of
them is actually reachable in production.**
`Jarvis/integrations/gcal.py:112-118` (`create_event(..., duration_minutes: int
= 60, ...)`) and `Jarvis/agent/tools.py:27` (`DEFAULT_DURATION = 60`) both hard-code
`60`. In practice, `gcal.create_event`'s own default is dead code on every path
Jarvis actually runs: its one caller, `calendar_create`
(`Jarvis/agent/tools.py:142`), always passes an explicit
`duration_minutes=duration or DEFAULT_DURATION` — never omits the argument — so
`gcal`'s default only fires when something calls `gcal.create_event` directly
(as `test_create_event_defaults_to_an_hour` does). `bot/formatting.py:38`'s
confirmation embed also reads `DEFAULT_DURATION` from `tools.py`, not from
`gcal`. The comment at `tools.py:26` ("Mirrors gcal.create_event's own default")
shows the author already saw the coupling and hand-synced it instead of
removing it.
**Ruling:** `gcal.py` should own this value — it is the integration that
ultimately decides what "no duration specified" means to Google. The contract
fixes `create_event`'s signature at `duration_minutes: int = 60`, and that is
correctly left alone rather than changed on my own judgment, but nothing in
the contract forbids naming the literal: add `DEFAULT_DURATION_MINUTES = 60`
as a module-level constant in `gcal.py`, use it as the parameter default
(`inspect.signature` still reports `= 60`, so the contracted interface is
unchanged), and have `agent/tools.py` do
`from Jarvis.integrations.gcal import DEFAULT_DURATION_MINUTES as DEFAULT_DURATION`
instead of re-declaring it. That leaves exactly one literal `60` in the
codebase; today there are two, hand-synced by a comment instead of the
language. Not blocking Phase 5 — nothing about it is broken today — but it's
the kind of duplication that drifts silently the day someone changes one
without grepping for the other.

### LOW

**C2-L1 — Two module docstrings/comments say "Notion page id" where the value
can now also be a Google Calendar event id.**
`Jarvis/agent/tools.py:3-6` ("Slash commands, the regex fast-path and (Phase 5)
the LLM all reach Notion through this dict... returns one short user-facing
line — plus the created Notion page id") and `Jarvis/agent/manager.py:17-18`
("Page-creating tools hand back the Notion page id") both predate
`calendar.create`, which now also returns a tuple whose second element is a
Google event id (`Jarvis/agent/tools.py:143`,
`test_calendar_create_carries_the_google_event_id` in `test_manager.py`). The
in-dict comment at `tools.py:146-147` was correctly generalized to
"Page-creating tools" with no mention of Notion specifically — only the two
top-of-file docstrings were missed. Harmless (no runtime effect, no test
depends on the docstring text), but worth a one-line wording fix the next time
either file is touched: "Notion" -> "Notion or Google Calendar", "Notion page
id" -> "external id (a Notion page id or Google Calendar event id)".

## Explicit rulings on the pre-flagged items

**Item 4 — repeated code introduced this cycle.** Two shapes to judge
separately, and they come out differently:
- `agent/tools.py`'s three `_format_*` helpers (`_format_tasks`,
  `_format_groceries`, `_format_events`) look alike (loop, build a numbered/
  bulleted line, join, truncate) but render three domain types with genuinely
  different fields — priority/due for tasks, qty/category for groceries,
  span/location for events, and only events sort all-day-first. A generic
  `_format_list(items, line_fn)` would save perhaps four lines total and cost
  a level of indirection at every call site. Same judgment as pass 1's ruling
  on `todo.py`/`grocery.py`: parallel structure across different domains,
  leave it.
- `bot/commands/{todo,grocery,agenda,event}.py`'s four modules are the same
  shape (`app_commands` decorator, build an `Intent`, call `respond` or
  `confirm`) for the same reason as pass 1: discord.py wants literal,
  introspectable functions for its decorator/typing-based UI generation, and
  `agenda.py`/`event.py` verified to carry zero business logic (grepped for
  any call into `integrations/` or `notion`/`gcal` from `bot/commands/` —
  none; every function's body is one `Intent(...)` construction and one
  `respond`/`confirm` call). Not duplication worth extracting.
- The one real repetition worth naming is C2-M1 above, and it isn't in
  `tools.py`'s eight callables or `bot/commands/` — it's the duration default
  split across `gcal.py` and `tools.py`.

**Item 6 — the `DEFAULT_DURATION` / `duration_minutes=60` split.** Ruled
above (C2-M1): acceptable today (nothing is broken, both are 60, and one call
site is directly tested against gcal's own default), but it is a duplication,
not a coincidence, and `gcal.py` should own it via an exported constant
`tools.py` imports rather than re-declares. Fix is small enough that there's
no reason to defer it, but it does not block Phase 5.

## Verdict on the four review questions

1. **Unnecessarily repeated blocks of code?** One real instance, already
   covered above (C2-M1) — a duplicated literal, not a duplicated block. The
   two places that look like repeated *code* (`_format_*`, `bot/commands/*`)
   are parallel structure across genuinely different domains/framework
   requirements and are correctly left alone, consistent with pass 1's
   judgment on the same shapes.
2. **Does `gcal.py` mirror `notion.py`'s shape?** Yes, everywhere it matters
   (a `_call` wrapper that logs the exception type only and raises a
   user-safe domain error; a frozen dataclass; a `@lru_cache`d client/resource
   built lazily, never at import) and diverges only where the underlying APIs
   genuinely differ: `gcal._call(what, method, **kwargs)` calls
   `getattr(_events(), method)(**kwargs).execute()` because the Google client
   needs an explicit `.execute()` after every request object is built, where
   `notion._call(what, fn, **kwargs)` takes an already-bound, already-callable
   method because notion-client calls execute synchronously. That's a real
   API-shape difference, not sloppiness — forcing gcal's wrapper into
   notion's exact signature would mean either a fake no-op `.execute()`
   shim or losing the method-name logging context. Judged as the "genuinely
   differ" case, not "gratuitous divergence."
3. **Layering — has `agent/` importing `bot/` come back?** No. Grepped the
   whole `Jarvis/` tree for `from Jarvis.bot` / `import Jarvis.bot`: every
   hit lives inside `bot/` itself (`bot/client.py`'s two function-local
   imports, already ruled correct in pass 1 for breaking a real cycle;
   `bot/handlers.py` importing `bot/client` and `bot/formatting`;
   `bot/commands/*.py` importing `bot/handlers`; `main.py` importing
   `bot/client`). `bot/formatting.py` imports `DEFAULT_DURATION` *from*
   `agent/tools.py` — that's `bot -> agent`, the correct direction per §3's
   one-way arrow, not the reverse. The Phase-4 return of `bot/formatting.py`
   did not reintroduce the pass-1 inversion.
4. **Plan conformance, both directions.** `/agenda [day]` and
   `/event <title> <when> [duration]` match §4's command table exactly
   (`bot/commands/agenda.py:16`, `event.py:20-22`). The confirmation behavior
   matches §4's "calendar writes... render as an embed with ✅/❌ reactions
   before executing. Reads execute immediately" precisely: `/agenda` calls
   `respond` (dispatches immediately), `/event` calls `confirm` (parks the
   intent, never dispatches until a reaction). The two module-map edits
   (`gcal.py`'s `find_free_slots`-deferred-to-Phase-6 note, and
   `formatting.py`'s "confirmation embed... list rendering is NOT here"
   note) both describe the code as it stands, not as it's hoped to be —
   confirmed `find_free_slots` doesn't exist anywhere in `gcal.py`, and
   confirmed `agent/tools.py` still owns every list renderer with zero
   presentation-layer helpers moved into `bot/formatting.py`.

## Test quality (item 7)

`test_confirmation.py`'s guard tests assert on the `created` list populated by
a monkeypatched `gcal.create_event`, not on `on_raw_reaction_add`'s return
value (it has none — the function's contract is side effects only). Every one
of the six guard tests (bot's-own-reaction, no-member/DM, non-owner,
wrong-requester, unknown-message-id, unrelated-emoji) asserts `created == []`
*and* that `MESSAGE_ID in handlers.PENDING` still holds where relevant, so a
regression that let the wrong reaction through would fail the test even if it
happened to also refuse to dispatch for some unrelated reason. The
double-fire test (`test_a_second_check_does_not_book_the_event_twice`) and the
cross-then-check test (`test_a_check_after_a_cross_creates_nothing`) both
assert `len(created) == 1` / `created == []` rather than trusting a reply
string. These are load-bearing: deleting the `.bot` guard, the owner check, or
the `del PENDING[...]` line each make a specific test fail, not just look
different. Confirmed by inspection, not just by reading the docstring's claim
to that effect.

## Phase-5 readiness

**Ready.** No HIGH or blocking findings. One MEDIUM (C2-M1, a duplicated
literal with a clear one-line fix, not urgent) and one LOW (C2-L1, two stale
doc comments) are the entire list — both are optional cleanups, not defects,
and neither touches behavior Phase 5 would build on top of. Layering holds
(`agent/` still does not import `bot/`), the one-implementation-per-capability
rule holds for both new commands, `gcal.py`'s divergence from `notion.py`'s
shape is judged justified rather than sloppy, and the plan's Phase 3/4 edits
describe the code accurately in both directions. 376/376 tests pass, and the
new confirmation-flow tests are genuinely load-bearing rather than
tautological. Phase 5 (the LLM tool-use loop) can be built on this without
first returning to Phase 3/4 code.
