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

---

# Phase 5 Review — Layer 2

Scope: the LLM tool-use loop only — `router/llm.py`, `integrations/llm.py`,
`agent/tools.py`, `bot/handlers.py`, `config.py`, `storage/models.py`,
`utils/logging.py`, `requirements.txt`, `tests/unit/test_llm.py` — plus
plan.md §3, §6, §10, §11 (edited this cycle). Phases 1-4 are not re-audited.
Did not run pytest and made no OpenAI call, per the review's own constraints.

## Findings

### HIGH

**H1 — Multi-item batches from Layer 2 execute unconfirmed, contradicting
plan.md §4 and CLAUDE.md.** File: `Jarvis/router/llm.py:115-134`
(`_run_tools`). Both plan.md §4 ("Calendar writes and any multi-item batch
render as an embed with ✅/❌ reactions before executing") and CLAUDE.md
("Calendar writes and multi-item batches require ✅ confirmation before
executing") name two categories that need confirmation. Only one is
implemented: `PROPOSAL_ONLY = frozenset({"calendar.create"})`
(`agent/tools.py:183`) intercepts calendar writes; everything else in
`_run_tools`'s loop dispatches immediately, with no batch detection at all.
A single Layer 2 reply that emits three `add_task`/`add_grocery_item` calls
— entirely plausible phrasing: "add milk and eggs, and remind me to call
mom" — runs all three Notion writes with zero confirmation. This is new
exposure Phase 5 introduces: no earlier layer can produce more than one
write Intent per message (a slash command or a fast-path regex match always
yields exactly one), so this failure mode did not exist before Layer 2.
The gap is not an oversight the implementer missed — `bot/formatting.py:24-25`
says outright "`calendar.create` is the only write that reaches here in
Phase 4; the multi-item batches plan.md section 4 also names arrive with
Layer 2," i.e. the code's own comment predicts this and it was not built.
`confirmation_embed` (`bot/formatting.py:21-40`) also only knows how to
render a `calendar.create` intent (title/when/duration fields) — there is no
data shape yet for "these three writes are queued, confirm all or none."
Fix: extend `PROPOSAL_ONLY`-style handling so any round producing more than
one non-read tool call collects them into a batch proposal (new `Intent`
shape, or a list of intents) routed through the same `_prompt` confirmation
door single calendar writes already use, with a batch-rendering branch in
`confirmation_embed`. Given the model in `.env` is explicitly a small one
("a cheaper model misparses more often," plan.md §4), this is exactly the
scenario the confirmation guardrail exists for.

### MEDIUM

None.

### LOW

**L1 — `storage/db.py:1` docstring is stale.** "One table for now; the rest
arrive with their phases" — `llm_spend` was added this cycle (§6), so there
are two. Harmless, no runtime effect; fix the next time the file is touched.

**L2 — `conftest.py`'s comment about `utils/logging.py` describes the
pre-fix behavior.** `tests/conftest.py:21-22` says "the logging one fires on
any `get_logger` call," but the code it is describing
(`Jarvis/utils/logging.py:17`) now calls `load_dotenv()` once at import, not
per call — that's the fix this cycle made. The conftest safeguard (patching
`dotenv.load_dotenv` before the first Jarvis import) still holds either way,
so nothing is actually broken, but the comment now documents a bug that no
longer exists in the file it's pointing at. Worth a one-line correction next
time either file is touched.

## Plan honesty (item 1)

Checked §3, §6, §10, §11 against the code rather than against memory of
prior passes:

- **§3** — `router/llm.py` (Layer 2 loop), `integrations/llm.py` (transport +
  cost accounting), and the claim that tool schemas "live in `agent/tools.py`
  beside the implementations they describe, so the two cannot drift apart"
  all match: `TOOL_SCHEMAS` sits in `tools.py` next to `TOOLS`, and
  `integrations/llm.py` contains no schema. `bot/formatting.py`'s "List
  rendering is NOT here" claim still holds — `_format_tasks`/`_format_groceries`/
  `_format_events` are still private to `tools.py`. Accurate.
- **§6** — `llm_spend` exists exactly as described (`storage/db.py:19-24`,
  `storage/models.py:42-60`), one row per local day, backing the spend guard.
  `conversations` is genuinely absent — no such table, and `router/llm.py`
  builds a fresh two-message list per call with no persisted context. The
  "not built" framing is accurate, not flattering.
- **§10** — "RESOLVED": `requirements.txt` pins `openai>=1.109.0` with no
  `anthropic` anywhere in the file. Accurate.
- **§11** — the $0.40 / $1.60 per-1M-token figures in the plan match
  `integrations/llm.py:31` (`gpt-4.1-mini`) exactly, including the source URL
  and verification date in both places. Accurate.
- **§4 vs `handlers.py`** — still matches: slash commands go through Discord
  interactions directly (not `on_message`), `on_message` runs the fast-path
  and falls through to `_layer2` on a miss, and Layer 2's only write path
  (`calendar.create`) is routed through the same `_prompt` confirmation
  `/event` uses. The one place §4 is *not* fully matched is the multi-item
  batch sentence in the same section — see H1. That's a code gap, not a plan
  misstatement; the plan states the requirement correctly and the code
  doesn't meet it yet.

No instance of the plan being softened to flatter the code was found in the
sections edited this cycle. If anything §10/§11 undersell nothing, and H1
shows the opposite failure mode didn't happen either (the plan wasn't quietly
narrowed to drop the batch requirement once it turned out inconvenient — it's
still there, unmet).

## One implementation per capability (item 2)

Traced all three doors onto `calendar.create` and confirmed no second
implementation exists:

- Slash command: `bot/commands/event.py:26` builds `Intent(name="calendar.create", ...)`
  and calls `confirm()`.
- Fast-path: does not produce `calendar.create` (not in its pattern table —
  correct, calendar creation was never a Layer 1 capability).
- Layer 2: `router/llm.py:130-132` builds the same `Intent(name="calendar.create", ...)`
  shape and returns it instead of dispatching it.

`confirm()` and `_layer2()` both call the same `_prompt(send, intent,
requester_id)` (`bot/handlers.py:55-78`, called at lines 87 and 157) — one
function, two callers differing only in how the confirmation embed gets
posted (`interaction.response.send_message` vs `message.reply`). This is
genuinely shared, not duplicated: reading `_prompt`'s body top to bottom,
there is nothing Layer-2-specific or slash-specific inside it. All reads and
non-calendar writes route through `agent.manager.dispatch`, which looks the
tool up in the single `TOOLS` dict (`agent/tools.py:148-159`) — confirmed by
reading `manager.py` in full; it contains no second tool table and no
capability-specific branching. Clean.

## Ruling on item 3 — the `handle` return-type deviation

`handle` returns `tuple[str, str | None] | Intent | None` instead of the
addendum's contracted two-arm shape. The stated reasons are both real
constraints, not excuses: a `(message, external_id)` tuple has no slot to
carry a full `Intent` (name + args + source), and `router/` importing from
`bot/` would point the plan's §3 dependency arrow backwards (`router` sits
below `bot` in the layering: `bot -> agent -> router -> integrations`).

Given those two constraints, the alternative the implementer says they
rejected — a second exported function — would have made the router's public
surface *wider* (two entry points a caller has to know to call in the right
circumstances) for the same information a third return type carries in one
call. The deviation also doesn't introduce a foreign type: `Intent` is
already the currency the fast-path and slash commands hand to `dispatch`, so
Layer 2 handing one back for a write is reusing an existing, well-understood
shape rather than inventing a new one. The caller
(`bot/handlers.py:154-160`) discriminates with one `isinstance(outcome,
Intent)` check, which reads plainly at the call site.

**Ruling: right call.** A tuple that could secretly mean "here's a proposal"
via some sentinel value would be worse — more implicit, not less. The one
thing worth tightening: the union's docstring (`router/llm.py:48-61`)
explains *why* the shape deviates, but a one-line note in plan.md §3 or §4
pointing at the actual signature would save the next reader from re-deriving
this from the docstring alone. Not a blocker.

## Ruling on item 5 — `llm.py`'s inline try/except vs. `_call` wrappers

Checked call-site counts, since that's what a wrapper buys: `notion._call`
is invoked from at least six sites (`add_task`, `list_open_tasks`,
`_set_property` used by `complete_task`, `add_grocery`, `list_groceries`,
plus category lookups), and `gcal._call` from at least two
(`list_events`, `create_event`), all sharing byte-identical
try/log-type-name/raise-domain-error logic. Factoring that out is what
"one implementation per capability" is for — six copies of the same
except-block would itself be the repeated-code finding.

`integrations/llm.py` has exactly one call site for the OpenAI client:
`complete()`. There is nothing to de-duplicate. A `_call` wrapper here would
be an interface with one implementation and one caller — the same shape
CLAUDE.md's "one implementation per capability" rule and the standards
persona both argue against, just pointed the other direction (an
abstraction with no second user, rather than a second implementation of one
capability). The inline try/except in `complete()`
(`integrations/llm.py:72-82`) preserves every property that matters —
type-name-only logging, no key/prompt/response body reaching a log line or
exception, `LLMError` as the one exception type callers see — it's just not
extracted into a named function nobody else calls.

**Ruling: not a smell, correctly not wrapped.** Wrapping it would be
gratuitous symmetry with `notion.py`/`gcal.py` for its own sake, not for a
second call site. If `integrations/llm.py` grows a second network call
(e.g. embeddings, or a moderation pre-check) before Phase 9's retry/backoff
work lands, that's the trigger to extract `_call` — not before.

## Ruling on item 7 — `load_dotenv()` moved to import time in `utils/logging.py`

The bug it replaces was real: `get_logger()` previously called
`load_dotenv()` on every invocation, and since `get_logger(__name__)` runs at
import time in roughly every module in the tree, that meant many
re-invocations per test session, each one able to pull real `.env` values
into `os.environ` at an unpredictable point relative to `monkeypatch`
fixtures — `tests/conftest.py:20-27` documents this exact failure ("whichever
module imported first pulled the real .env... into os.environ process-wide,
where monkeypatch cannot undo it, and every later test in the session saw
them"). Moving the call to module level cuts that from N call sites to one,
which is a real reduction in surface, not just a relocation.

It does not eliminate the side effect, though — it changes it from "mutates
`os.environ` on a schedule nobody controls" to "mutates `os.environ`
unconditionally, once, the first time anything imports `Jarvis.utils.logging`
(which is nearly everything, transitively)." That single mutation still
happens before `Config` is ever validated, and `config.py`'s own docstring
("Reads .env once; no other module touches os.environ") is no longer
literally true — `.env` is now read at both `utils/logging.py` import time
and inside `config.get_config()`. `python-dotenv`'s default `override=False`
makes the double-read harmless in practice (the second call cannot clobber
values already present), and `conftest.py`'s belt-and-braces patch (stubbing
`dotenv.load_dotenv` at the source before the first `Jarvis` import, plus
re-stubbing both module-level names in the autouse fixture) neutralizes it
fully for the test suite specifically. So functionally this is closed.

**Ruling: the right fix for the bug it targets, but it trades one smell for
a smaller one rather than for nothing** — an import-time side effect in a
module every other module pulls in is still surprising to a future reader,
and `config.py`'s "no other module touches os.environ" line is now
technically inaccurate (L2 above already flags the adjacent conftest comment
going stale in the other direction). Given `LOG_LEVEL` genuinely needs to be
readable before `Config` can fail loudly, and given the alternative (reading
`os.environ` directly for just `LOG_LEVEL`, sidestepping `dotenv` entirely in
this one file) would avoid the second `load_dotenv` call site without losing
anything `.env`-file support currently provides — that's a smaller fix than
what shipped, but not one this review is blocking Phase 5 on. Worth a
one-line amendment to `config.py`'s docstring acknowledging the second call
site exists and why.

## Test quality (item 6)

Spot-checked the two named guards, and both assert on calls, not just
returns:

- **Proposal-only guard** — `test_a_model_proposed_calendar_write_is_handed_back_not_dispatched`
  and `test_a_proposal_stops_everything_after_it_in_the_same_reply`
  (`test_llm.py:99-143`) both patch `router.dispatch` via the `dispatched`
  fixture and assert `dispatched == []`, not just that the return value is an
  `Intent`. `test_a_proposed_write_does_not_touch_the_calendar_end_to_end`
  goes one step further and patches `gcal.create_event` directly, so a leak
  through any path other than `dispatch` would still be caught.
- **Spend guard** — `test_at_or_over_the_daily_limit_layer2_makes_zero_api_calls`
  uses `no_api_calls`, which doesn't just record calls — the stub raises
  `AssertionError` if invoked at all, and the test's own comment
  (`test_llm.py:170-171`) explains why it's `AssertionError` and not
  `LLMError`: `handle()` swallows `LLMError` into the same `FALLBACK` string
  the guard itself returns, so an `LLMError`-based stub would make "the guard
  never fired" indistinguishable from "the guard fired correctly." Using an
  exception type `handle()` does not catch is what makes this test
  load-bearing rather than cosmetic.

Both hold up under the "assert what was called" bar the file's own docstring
sets. No gap found in these two.

## Verdict on the four review questions

1. **Unnecessarily repeated code?** None found in the reviewed files. The
   inline try/except in `integrations/llm.py` looking different from
   `notion.py`/`gcal.py` is deliberate and correct (item 5) — one call site
   doesn't earn a wrapper.
2. **Helper-function use / modularity?** Yes — `_prompt` is genuinely shared
   between the slash-command and Layer 2 confirmation doors, `_run_tools` and
   `filter_args` cleanly separate "what did the model ask for" from "what is
   it allowed to touch," and `_check_schemas()` is load-bearing self-checked
   coupling between `TOOLS` and `TOOL_SCHEMAS` (verified all four failure
   modes the review asked about: a missing schema and an extra schema both
   trip the `named == set(LLM_NAMES)` assert, an invented parameter trips
   `declared <= set(params)`, and a wrong `required` list trips the
   `needed == set(required)` assert).
3. **Does the code do what it promises?** Mostly. Every promise checked
   holds except one: plan.md §4's multi-item-batch confirmation requirement
   is not implemented for Layer 2, and the code's own comment
   (`bot/formatting.py:24-25`) shows this was a known, named gap rather than
   an accidental one. See H1.
4. **Does the code match plan.md?** Yes on §3, §6, §10, §11 — all four
   sections edited this cycle describe the code as it actually is, not as a
   flattering approximation of it. The one mismatch (H1) is the code falling
   short of an accurate plan, not the plan being loosened to match the code.

## Phase 6 readiness

**Not blocked, but H1 should be fixed before Phase 6 (or explicitly
deferred in plan.md with a reason) rather than carried silently.** The daily
brief (Phase 6) doesn't touch Layer 2's tool loop or the confirmation flow at
all — it's a scheduled job reading Calendar/Notion and making its own single
LLM call for prose, a different code path entirely — so nothing in Phase 6
depends on H1 being fixed first. Everything else reviewed (spend guard,
schema/tool coupling, the allowlist on tool names and arguments, and the
proposal-only gate for calendar writes specifically) is solid and gives
Phase 6 a correct foundation to build the "one LLM call, deterministic
fallback on failure" pattern plan.md §5a already commits to.

---

# Phase 5 Fix Wave — verification — 2026-09-12

Scope: verifying the fix wave that closes my own H1 (multi-item batches from
Layer 2 executing unconfirmed), plus the two items from the same finding
(round 2's tool schemas, the spend-write latch) and plan conformance for both
sections named in the brief. Read fresh: `router/llm.py`, `bot/handlers.py`,
`bot/formatting.py`, `agent/tools.py`, `agent/manager.py`, `router/intents.py`,
`storage/db.py`, `storage/models.py`, `plan/plan.md` §4 and §6, and both
`tests/unit/test_llm.py` and `tests/unit/test_confirmation.py` in full. Ran
`python -m pytest tests/ -q`: **465 passed**, 0 failed. No OpenAI call made,
`.env`/`secrets/` untouched, no git command run, and — per this review's own
constraint — nothing under `Jarvis/` or `tests/` was edited; the only write is
this entry.

Also noted for the record: this fix wave was implemented and its tests
written by the same actor (the Agent Manager, hand-verifying code after the
fix agent hit a spend limit), so the normal write/test separation didn't hold
this round. The brief asks me to audit that specifically; see "Test-change
audit" below.

## H1 — RESOLVED

Evidence, traced end to end rather than taken on the diff's word:

- **`PENDING` is now keyed to a tuple.** `Jarvis/bot/handlers.py:36`:
  `dict[int, tuple[tuple[Intent, ...], int, float]]`. A comment at lines 34-35
  states the intent plainly ("one write, or the whole batch... confirmed
  together"), and it holds — see the single expression below.
- **One decision computes both the calendar case and the batch case.**
  `Jarvis/router/llm.py:156-157`:
  ```python
  writes = tuple(i for i in intents if i.name in WRITE_NAMES)
  held = writes if len(writes) > 1 or any(i.name in PROPOSAL_ONLY for i in writes) else ()
  ```
  `held` is always either *all* of `writes` or *none* of it — there is no
  partial-hold state, so the exclusion filter two lines down
  (`if not (held and i.name in WRITE_NAMES)`) cannot accidentally let one
  write in a held batch slip through while blocking another. A lone
  non-calendar write (`len(writes) == 1`, not in `PROPOSAL_ONLY`) is the only
  case that dispatches immediately — matching the fast-path's existing
  one-write-per-message bargain, as the docstring at lines 139-144 claims.
  `calendar.create` is held at count 1 too, via the `any(... PROPOSAL_ONLY
  ...)` arm — confirmed by
  `test_a_model_proposed_calendar_write_is_handed_back_not_dispatched`.
- **`WRITE_NAMES` exists and is exactly what it should be.**
  `Jarvis/agent/tools.py:188-190`: the five actual writes
  (`grocery.add`, `grocery.check`, `task.add`, `task.complete`,
  `calendar.create`) — reads are excluded on purpose, and that's not just
  claimed, it's checked: `assert not WRITE_NAMES & {"grocery.list",
  "task.list", "calendar.agenda"}` runs in `tools.py`'s own `__main__`
  self-check (line 292), and `assert PROPOSAL_ONLY <= WRITE_NAMES <=
  set(TOOLS)` runs unconditionally at import time via `_check_schemas()`
  (line 276, called at line 286). A drifted set here would fail on the next
  test run, not just look wrong on inspection.
- **The confirmation mechanism itself did not fork.** `_prompt`
  (`Jarvis/bot/handlers.py:62-85`) takes `intents: tuple[Intent, ...]`
  unconditionally; `confirm()` (line 96) wraps a single slash-command intent
  in a 1-tuple before calling it, `_layer2()` (line 175) passes Layer 2's
  tuple straight through. `on_raw_reaction_add` (lines 138-149) loops over
  whatever tuple it finds and runs every entry — a 1-tuple loops once. I
  grepped the whole tree for other consumers of `PROPOSAL_ONLY`/`WRITE_NAMES`
  and for any other `len(...)`-gated branch near this code (see "genuinely
  one path" below) and found none.
- **465/465 tests pass**, including four new/adapted `test_llm.py` cases that
  exercise exactly this decision from the outside
  (`test_two_writes_in_one_reply_confirm_together`,
  `test_a_batch_of_plain_writes_is_held_even_with_no_calendar_write`,
  `test_a_lone_write_still_runs_without_asking`,
  `test_a_read_alongside_a_held_write_still_runs`), plus two new
  `test_confirmation.py` cases exercising the handler side
  (`test_one_check_runs_every_write_in_a_batch`,
  `test_a_cross_on_a_batch_runs_none_of_it`). All assert on `dispatched`/
  `created` — the actual side effect — not on a return value, consistent with
  this file's own stated bar.

**Verdict: H1 is closed, correctly, at the root.** The fix is one arithmetic
decision (`held`) consumed by one execution filter and one confirmation
mechanism, not a batch-shaped patch bolted alongside the old single-intent
path.

## Judging the fix, not just its presence (item 1's four questions)

**Is this genuinely one code path, or a second one in disguise?** One path.
I grepped for every count-based branch touching this feature
(`len(intents)`, `len(held)`, `len(writes)`, `len(proposals)`) across all of
`Jarvis/` and found exactly two hits in the whole tree: the `held =`
assignment above, and `Jarvis/bot/formatting.py:43`
(`title="Do this?" if len(intents) == 1 else f"Do all {len(intents)}?"`) —
a cosmetic string choice, not a structural fork. `Jarvis/agent/manager.py`'s
`dispatch()` (unchanged by this fix wave, read in full) has zero knowledge of
confirmation policy — it is exactly as dumb as it was before H1, taking one
`Intent` and running it. The policy — what needs a ✅ — lives in exactly one
place, `router/llm.py`'s `_run_tools`. That is the definition of one path.

**Is `WRITE_NAMES` in the right module?** Yes. It sits in `Jarvis/agent/tools.py`
next to `PROPOSAL_ONLY`, which this review's own Phase-5 pass already judged
correctly placed there (Cycle-2/Phase-5 findings, "one implementation per
capability," item 2). Both constants are properties of the tool *registry* —
which tools mutate state, which of those are calendar-risky enough to always
hold — not of the router consuming them. `router/llm.py` imports both
(`line 21`); nothing in `agent/tools.py` imports anything from `router/`, so
the plan §3 dependency arrow (`bot -> agent -> router -> integrations`) isn't
touched by where this metadata lives — `router` reading a constant out of
`agent` is the same direction the file already reads `TOOL_SCHEMAS` and
`LLM_NAMES` from, not a new one.

**Does `confirmation_embed` read well for both a single write and a batch, or
has the single case regressed?** No regression, and the reason is worth
stating plainly: the single-write case was *always* `calendar.create`, both
before and after this fix. I checked every slash command
(`Jarvis/bot/commands/{todo,grocery,agenda,event}.py`): only `event.py` calls
`confirm()`; `todo.py` and `grocery.py`'s adds/completes call `respond()`,
which dispatches immediately and never reaches `confirmation_embed` at all.
So the lone-item branch of `confirmation_embed` was never rendering a plain
Notion write pre-fix, and still isn't — the function's single-item input
shape hasn't changed, only its rendering (fields -> one joined description
string) and the fact that it can now also be handed 2+ items. The rendering
itself (`Jarvis/bot/formatting.py:21-31`, `_line`) still special-cases
`calendar.create` with a proper title/time/duration line, and falls back to
`f"**{intent.name}** - " + ", ".join(...)` for anything else — used for the
first time in a batch, since a lone non-calendar write never reaches this
function. That generic line is honestly plain (`"**task.add** - floss"`
rather than "Add task: floss"), but it's the disclosed kind of plain — the
comment at lines 28-30 names the tradeoff and its trigger ("Prettify it when
a line actually reads badly to the user") rather than hiding it. Not a
regression; a minor cosmetic rough edge on genuinely new code, correctly
flagged in-line. `test_the_embed_shows_every_line_of_a_batch` exercises this
exact generic branch (`task.add`/`grocery.add`, neither is `calendar.create`)
and checks an exact line count (`len(embed.description.splitlines()) == 2`),
which is a stronger assertion than "the words appear somewhere."

## Item 2 — round 2 handed `[]` instead of `TOOL_SCHEMAS` — RESOLVED

`Jarvis/router/llm.py:99`: `complete(messages, TOOL_SCHEMAS if round_number ==
0 else [])`. The comment above it (lines 94-97) names the actual threat
correctly — round 1's tool output is replayed as a plain user turn, and that
text can carry a Notion title written by anyone with database access, so
leaving tools attached on round 2 would let a crafted title talk the model
into an unconfirmed write. `test_round_two_is_handed_no_tools_at_all`
(`test_llm.py:366-380`) asserts `seen[1][1] == []` directly against the
captured call, not against behavior that would only indirectly imply it.

## Item 3 — `_SPEND_BLIND` latch — RESOLVED

`Jarvis/router/llm.py:47` declares the module-level flag with a `ponytail:`
comment naming its ceiling (in-memory, one-way, reset by restart) and why
that's sufficient (a retry or N-consecutive-failure count "would buy nothing
a restart does not"). `handle()` checks it first, before even the spend read
(lines 71-73); `_record()` sets it on a failed write (lines 126-133), with a
comment explaining the asymmetry it closes: reads already failed closed
(an unreadable counter refuses service), but a failed *write* previously left
the counter frozen while Layer 2 kept spending against it — unbounded and
invisible. `test_a_failed_spend_write_shuts_layer_2_until_restart`
(`test_llm.py:346-363`) proves both halves: the call that fails to record
still answers the user (billing is best-effort), and the *next* call is
refused before any request goes out, backed by a transport stub that raises
`AssertionError` if it's invoked at all. The autouse `spend_not_blind`
fixture (`test_llm.py:75-84`) resets the module global per test, with a
comment correctly distinguishing "test isolation" from "fixing the module" —
latching for the life of the process is the point.

## Plan conformance (item 4)

**§4's confirmation wording.** "Calendar writes and any multi-item batch
render as an embed with ✅/❌ reactions before executing. Reads execute
immediately." (`plan/plan.md:213-214`) — this is not contradicted by the
code; it's a two-category enumeration (calendar writes, batches) and the
code implements exactly those two plus one uncontested default (a lone
non-calendar write runs, which is the complement of both named categories,
not a third category the plan forgot). CLAUDE.md's own wording ("Calendar
writes and multi-item batches require ✅ confirmation before executing") draws
the identical boundary. **Ruling: no move required.** Both governing
documents are consistent with the code as built. The one thing I'd still do
opportunistically (not blocking, not a finding) is add a half-sentence
spelling out the third case explicitly, since `router/llm.py`'s own comments
already articulate the "matches the fast-path's bargain" rationale better
than the plan does — a future reader of §4 alone wouldn't get that
justification without reading the code.

**§6's `messages` table vs. the batch-recording `ponytail:` note — new
finding, see FW-M1 below.** This one *is* a real divergence, and it's
new — introduced by this fix wave's own mechanism, not a pre-existing gap.

## New findings

### MEDIUM

**FW-M1 — A confirmed batch leaves only its last write in the `messages`
table; `plan.md` §6 still promises the table "powers undo and idempotency"
without qualification.**
`Jarvis/storage/db.py:12`: `discord_message_id INTEGER PRIMARY KEY`.
`Jarvis/storage/models.py:19-20`: `record_message` is `INSERT OR REPLACE`
keyed on that column. `Jarvis/bot/handlers.py:138-149`:
```python
for intent in intents:
    line, external_id = await _run(intent)
    lines.append(line)
    try:
        # ponytail: one row per Discord message id (it is the primary key), so
        # a batch leaves only its last write recorded. Fine while the row is
        # just idempotency bookkeeping; needs its own table for ❌-undo.
        record_message(payload.message_id, intent.name, intent.args, external_id)
    except Exception:
        log.exception("Couldn't record confirmed %s", intent.name)
```
This loop calls `record_message` once per intent in the batch, every call
keyed by the *same* `payload.message_id`. Because that column is the primary
key and the write is `INSERT OR REPLACE`, each call overwrites the last —
after a 3-write batch confirms, `find_message(payload.message_id)` returns
only the third write's `(intent, args, external_id)`. The other two writes
genuinely happened (Notion/GCal both got real rows/events — nothing is lost
*there*), but the local bookkeeping table plan.md §6 describes as powering
"undo and idempotency" (`plan/plan.md:304`) cannot recover them. This is
**not a pre-existing condition** — before this fix wave, `PENDING` held one
`Intent`, so this loop ran exactly once per confirmation and the overwrite
scenario could not arise; generalizing `PENDING` to a tuple is what makes
one message id correspond to N writes for the first time.

The comment is honest and correctly named (both the ceiling and the upgrade
path, per this codebase's own `ponytail:` convention), which is why this
isn't rated HIGH — nothing breaks today, since nothing currently reads
`messages` at runtime to gate behavior (`find_message` is called only from
tests; grepped to confirm). But two things are missing that CLAUDE.md's own
rule calls for: (1) `plan.md` §6 was not updated in the same commit to
mention the limitation the code itself now documents — a reader of the plan
alone would believe every write in a confirmed batch is individually
recoverable, and it isn't; (2) no test locks in or even exercises this
behavior — `test_one_check_runs_every_write_in_a_batch` checks `created`
(what reached Google Calendar) but never calls `find_message` afterward, so
there is nothing that would fail if the overwrite got worse (e.g., silently
started raising, or started recording the *first* write instead of the last,
which would be a strictly worse bug for idempotency-on-redelivery).

**Fix:** add a clause to §6's `messages` row along the lines of "one row per
Discord message id; a confirmed batch (§4) currently keeps only its last
write — the earlier ones still land in Notion/GCal, just without local
idempotency/undo bookkeeping. A real fix needs one row per intent (or a JSON
list column) and is deferred to Phase 9's undo work, which needs that
restructuring anyway." This is the same style of disclosure this review
already approved for M5's `Project` column — document the gap where the plan
makes the promise, rather than silently letting the plan overclaim. Not
blocking Phase 6, which never touches `messages`.

### LOW

**FW-L1 — `_line`'s generic (non-calendar) rendering in a batch is honestly
plain, not polished.** `Jarvis/bot/formatting.py:31`: a batch line for e.g.
`task.add` reads `"**task.add** - floss"` — the raw dotted registry name, not
"Add task: floss." Already covered above under item 1's embed question;
listed here only so it's tracked as a named, low-priority item rather than
folded silently into the H1 verdict. The comment at `formatting.py:28-30`
already names the tradeoff and the trigger for revisiting it ("when a line
actually reads badly to the user") — no action needed unless that trigger
fires.

## Test-change audit (write/test separation was off this round)

Checked the two specific claims in the brief against the actual assertions,
not against the fact that tests exist and pass.

**The two replaced `test_llm.py` tests.** Grepped for the old names
(`test_a_proposal_stops_everything_after_it_in_the_same_reply`,
`test_a_tool_before_a_proposal_still_runs`) — zero hits anywhere in `tests/`,
confirming they're gone, not renamed-with-old-bodies. Their old claims don't
translate 1:1 to the new design because the property they tested no longer
exists in this shape: the old loop dispatched in order and stopped the
instant it hit a proposal, so "stops everything after it" and "a tool before
it still runs" were both about *position within the call list*. The new
`_run_tools` (`router/llm.py:146-161`) builds the full `intents` list first
with no early return, then partitions by write/read membership — position is
structurally irrelevant, which I confirmed by reading the filter
(`if not (held and i.name in WRITE_NAMES)`) rather than assuming it: nothing
in it, or in the loop building `intents`, branches on index or order.
Given that, the replacement tests cover the *successor* properties correctly:
`test_a_read_alongside_a_held_write_still_runs` proves a call earlier in the
list (`list_tasks`) still runs when a later call is held — direct
successor to "a tool before a proposal still runs." No replacement directly
puts a read *after* a proposal in the call order, but since the code has no
order-dependent branch at all (verified by inspection, not assumed), a
mirrored test would exercise the identical code path and add no coverage —
this is a non-gap, not an overlooked one. `test_two_writes_in_one_reply_confirm_together`
and `test_a_batch_of_plain_writes_is_held_even_with_no_calendar_write` are
net-new coverage for a property the old tests couldn't have expressed at all
(batching), so the replacement set is broader than the pair it replaced, not
narrower. **No case was quietly dropped.**

**The embed tests (fields -> description).** `test_the_embed_shows_the_title_the_time_and_the_duration`
and `test_the_embed_admits_when_it_could_not_read_the_time` check substring
containment in `embed.description` where a fields-based version would have
checked `embed.fields[n].value` — a different shape, not a lower bar, since
both still pin the three facts that matter (title text, a correctly-rendered
time, the duration) and the parse-failure fallback text. The genuinely new
`test_the_embed_shows_every_line_of_a_batch` is, if anything, stricter than
what a single-item fields test could have been: it pins an exact line count
(`len(embed.description.splitlines()) == 2`), which would fail on a stray
blank line or an accidental header — a category of bug a fields-based test
has no equivalent way to catch. **No assertion here reads as weakened.**

**General sweep for quietly-loosened assertions.** Read every test in both
files top to bottom (not just the ones the brief named). All of the
pre-existing single-intent tests in `test_confirmation.py`
(`test_the_bots_own_check_does_not_fire_its_own_confirmation` through
`test_a_fresh_check_still_books`) were adapted mechanically — the `pend()`
helper (`test_confirmation.py:99-100`) now wraps `INTENT` in a 1-tuple, but
every assertion downstream of it is untouched (`created[0][0] == "dentist"`,
`MESSAGE_ID not in handlers.PENDING`, etc.) — this is exactly the "shape
change forces a mechanical edit" case CLAUDE.md's own conventions would
expect, not a place where scrutiny was softened. Nothing in either file
asserts a weaker property than the finding it closes; several of the new
tests (the exact-line-count embed check, the `AssertionError`-not-`LLMError`
transport stub reused from the pre-existing spend tests) are stricter than
the median test already in the file. **Verdict: the test changes hold up.**
I would have signed off on this file as the test-writing subagent.

## Verdict on the four review questions

1. **Unnecessarily repeated blocks of code?** None. The fix adds one
   constant (`WRITE_NAMES`), one arithmetic line (`held =`), and generalizes
   existing functions to take a tuple instead of a single `Intent` — no
   parallel implementation of the confirmation flow was created anywhere.
2. **Helper-function use / modularity?** Yes. `_prompt` remains the single
   shared confirmation door for both callers; the write/proposal
   classification lives in exactly one file (`agent/tools.py`) and is
   consumed from exactly one other file (`router/llm.py`); `_check_schemas()`
   was correctly extended with two new assertions
   (`PROPOSAL_ONLY <= WRITE_NAMES <= set(TOOLS)`, and the reads-are-not-writes
   check in `__main__`) rather than left to drift silently.
3. **Does the code do what it promises?** Yes. H1's exact promise — a batch
   confirms together, a calendar write always confirms, a lone non-calendar
   write doesn't — is implemented as one decision and tested from both the
   router side and the handler side. The one promise that's now measurably
   not quite kept is the `messages` table's "idempotency and undo" claim for
   the batch case (FW-M1), which is a plan/code gap, not a broken feature.
4. **Does the project match the plan?** Yes for §4 (no move needed, see
   above); not yet for §6, where FW-M1 is a real, dated, and — per CLAUDE.md's
   own rule — overdue plan edit that should have shipped in this same fix
   wave.

## Phase-6 readiness

**Clear to start.** H1 is closed at the root, with a single decision point
and no disguised second path; items 2 and 3 are both confirmed fixed with
tests that would fail if either regressed; 465/465 tests pass. The one new
finding (FW-M1) is a MEDIUM documentation gap in a table Phase 6 (the daily
brief) never reads or writes — grepped `scheduler/` and confirmed it doesn't
exist yet, so there's nothing for Phase 6 to inherit here. The test-change
audit found no weakened assertions and no quietly-dropped case; the
write/test separation being off this round did not produce a worse outcome
than the normal process would have. Fix FW-M1's plan edit whenever `messages`
is next touched — ideally before Phase 9 (undo) starts, since that phase is
exactly where the gap stops being free.
