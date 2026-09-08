# Security Review — Phases 1 & 2

**Date:** 2026-09-07
**Scope:** `Jarvis/` (24 files) — Discord bot, Notion integration, fast-path router,
Agent Manager, SQLite storage. Phases 3+ (Calendar, LLM, weather, reminders,
scheduler) do not exist yet and are out of scope.
**Method:** full read of every file in `Jarvis/`, `plan/plan.md` §4/§7/§9, `CLAUDE.md`;
`python -m pytest tests/ -q` (read-only, no edits made anywhere).

---

## Findings (most severe first)

### MEDIUM — `Config` dataclass has no redaction; a future log/debug call prints both tokens in cleartext
**File:** `Jarvis/config.py:12-26`

`Config` is `@dataclass(frozen=True)` with the default `repr`. That default `repr`
prints every field verbatim, including `discord_bot_token` and `notion_token`. No
call site in the current code logs the `Config` object, so nothing leaks *today* —
but the landmine is live: `log.info("cfg=%s", get_config())`, an unhandled
exception whose traceback dumps local variables (e.g. a debugger, or a future
`log.exception` inside a function that has `config` in scope), or a `print(config)`
during future debugging will emit both tokens straight into console/`#logs`.
CLAUDE.md is explicit that a credential must never "reach a log line ... or an
error trace" — this dataclass makes that one line-of-code away everywhere `Config`
is imported (11+ modules).

**Fix:** `@dataclass(frozen=True, repr=False)` plus a hand-written `__repr__` that
redacts (`Config(discord_owner_user_id=..., notion_token='***', ...)`), or at minimum
redact the two token fields. Cheap, and removes the landmine permanently rather than
relying on every future caller remembering not to log the object.

### LOW — No `allowed_mentions` guard; reflected user text can trigger @everyone/@here/user pings
**Files:** `Jarvis/bot/handlers.py:31,54` (`interaction.followup.send`, `message.reply`),
`Jarvis/agent/tools.py:25,30` (`_pick`'s f-string messages), `Jarvis/bot/formatting.py`
(task/grocery names rendered verbatim in list output)

Task/grocery item text comes from the owner's own message or slash-command argument
and is echoed back into Discord messages and lists with no `discord.AllowedMentions`
override anywhere in the codebase (confirmed — no `allowed_mentions`/`AllowedMentions`
hits in `Jarvis/`). `/todo add text:"@everyone standup"` → Jarvis's own reply
`"Added task: @everyone standup."` will actually ping the whole guild when sent,
and the same text pings again every time `/todo list` echoes it back. Given the
single-owner threat model this is self-inflicted rather than exploitable by a third
party, but it's still an unauthorized-broadcast side effect the plan's "guild of
one" framing doesn't actually prevent (nothing stops a second guild member from
being added, and a self-inflicted `@everyone` in a shared guild pings everyone in
it). Cheap, worth closing now rather than after Phase 5 hands the LLM the same
reply path.

**Fix:** default `allowed_mentions=discord.AllowedMentions.none()` on every outbound
send (`message.reply`, `interaction.followup.send`), or set it once on the
`commands.Bot(...)` constructor in `bot/client.py` so it applies everywhere without
touching each call site.

### LOW — `on_message`'s `parse()` / `dispatch()` calls sit outside any try/except in the handler itself
**File:** `Jarvis/bot/handlers.py:40-47`

Every other fallible step in this file (the reactions/reply at 48-56, `record_message`
at 58-63) is explicitly wrapped in try/except, matching CLAUDE.md's "every external
call is fallible" rule. The `parse(message.content, ...)` call (line 40) and
`result = await _run(intent)` (line 47) are not. In practice this is low-risk today:
`dispatch()` (`Jarvis/agent/manager.py:19-26`) already catches `Exception` broadly
and never raises, and `parse()` is pure regex with no I/O, so nothing currently
throws from either call. The bot process would also not go down even if one did —
discord.py's own event dispatch wraps `on_message` and logs via `on_error` rather
than propagating — but that's the library's safety net, not this codebase's, and an
exception taking that path skips this project's own logger/format and won't reach
`#logs` once Phase 9 wires that up. Inconsistent with the file's own established
pattern.

**Fix:** wrap lines 40-47 in the same try/except style used two lines later, for
consistency and so a future regressed regex reports through this project's logger
rather than discord.py's default handler.

---

## Clean categories

- **Owner allowlist:** every path traced and gated. Slash commands go through
  `bot.tree.interaction_check = _owner_only` (`bot/client.py:56`), a tree-level
  global check that applies to `/ping` and both command groups regardless of
  registration order. The message handler checks `is_owner` before touching the
  router (`bot/handlers.py:37`). No reaction handlers, no components/modals, and no
  second dispatch entry point exist yet to leave ungated. Clean.
- **SQL injection:** `storage/models.py` uses parameterised `?` placeholders
  throughout (`INSERT OR REPLACE ... VALUES (?, ?, ?, ?, ?)`); no string-built SQL
  anywhere in `storage/`. Clean.
- **Notion "injection":** all writes build structured property dicts
  (`{"text": {"content": name}}`); nothing concatenates user text into a query or
  formula. Clean.
- **No `eval`/`exec`/`subprocess`/`os.system`/`pickle`** anywhere in `Jarvis/`
  (grepped). Clean.
- **Credential leakage in logs/errors (current call sites):** `config.py`'s
  `SystemExit` names only key names, never values (`config.py:63-66`). `notion.py`'s
  `_call` logs `type(exc).__name__` only, never the exception body that could echo a
  token (`notion.py:88-97`). No test fixture contains a real-looking secret —
  `tests/conftest.py`'s `FAKE_ENV` is clearly fake and labelled as such. The one
  landmine is the `Config` repr above, not an active leak.
- **Runaway/looping API calls:** `on_message` filters `message.author.bot` first,
  so the bot's own reactions/replies can never re-enter the router — no echo loop.
  No retry/backoff exists yet (correctly — that's Phase 9), so nothing can retry
  unboundedly. Notion reads pass a single `page_size` (25/50) with no pagination
  loop. `dispatch()` and `notion._call` never re-enter each other. Clean for the
  phases that exist.

## Already-known issues — security dimension assessed as requested

- **`utils/dates.py` `parse_when` "in N hours" off by an hour across DST.**
  Reproduced (`test_in_n_hours_is_n_real_hours_across_a_dst_boundary` fails both
  parametrizations). No security dimension — it only affects the owner's own
  due-date/reminder math, no cross-user exposure, no auth implication.
- **`router/fastpath.py` "got a minute?" matches as a grocery/task check-off.**
  Reproduced (`test_a_question_is_never_a_check_off` fails both cases). No security
  dimension — the path is already owner-gated before it reaches this code, so the
  worst case is the owner's own list getting a spurious `_pick` lookup against their
  own data (and `_pick` degrades safely to "Nothing on the list matches" if nothing
  fits). Correctness bug, not an access-control or leakage bug.
- **`TOOLS` return only `str`, so `messages.external_id` is always `NULL`.**
  No security dimension *today* — nothing reads `external_id` yet. Flagging forward:
  when Phase 9's undo is built, it must not compensate for the missing id by
  fuzzy-matching "the most recent thing that looks right" against Notion (by name or
  recency) — that would let an undo silently act on the wrong page if two items
  share a name. The fix belongs in Phase 9 (thread the real page id back through
  `TOOLS`), not now, but the review gate should catch an undo implementation that
  tries to route around the missing id instead.
- **`utils/logging.py` calls `os.getenv("LOG_LEVEL")` directly.** No security
  dimension on its own — `LOG_LEVEL` isn't sensitive, and the file-level comment
  correctly explains why (logging must work before config validation runs). The
  only forward risk is precedent: it's the one place in the codebase that reads
  `os.environ` outside `config.py`'s validated path, so if a future edit reuses this
  same shortcut for something sensitive, it would skip `config.py`'s validation and
  centralized handling. Worth keeping the comment that flags it as the deliberate
  exception, not worth fixing now.

---

## Verdict

**Can someone else access my personal information?** No ungated path found. Every
way a Discord interaction reaches Notion (slash command, plain message) is checked
against `DISCORD_OWNER_USER_ID` before any business logic runs, and no credential
currently reaches a log line, Discord message, or exception string at any live call
site. The one open risk is the `Config` repr landmine (MEDIUM, above) — not an
active leak, but one careless future `log.info(config)` away from becoming one.

**Can anyone else make calls to Jarvis?** No. The global `interaction_check` covers
every slash command and `is_owner` covers the message handler; there is no second
entry point (no reaction/component handlers exist yet) that bypasses either. Non-owner
slash-command attempts are rejected and logged by user ID only.

---

## Review Pass 2

**Date:** 2026-09-07
**Scope:** same as Pass 1 (`Jarvis/`, now 23 files — `bot/formatting.py` was deleted).
Verifying the fix wave applied after Pass 1, plus the new `dispatch` return-tuple
contract. Read-only; no files edited under `Jarvis/` or `tests/`.

### Pass 1 findings — verification

1. **MEDIUM — Config repr — FIXED.** `Jarvis/config.py:13` is now
   `@dataclass(frozen=True, repr=False)` with a hand-written `__repr__`
   (`config.py:28-35`) that redacts `discord_bot_token` and `notion_token` as
   `'***'` while echoing every other field via `dataclasses.fields(self)`. Grepped
   the whole tree for `asdict`, `astuple`, `dataclasses.fields`, `.__dict__` and
   `vars(...)` against `Config`/`get_config()` — the only hit is the `__repr__`
   implementation itself; nothing in the codebase calls `dataclasses.asdict()` or
   `astuple()` on a `Config`, which are the two APIs that would still read the raw
   field values regardless of `__repr__` (they bypass `repr` by design). That's a
   theoretical residual, not a live gap — no such call site exists, and adding
   field-level redaction metadata against a hypothetical caller would be
   over-engineering for a landmine that isn't live. Formatting a single field
   directly (`f"{cfg.notion_token}"`) still prints the real value, but that's
   deliberate call-site intent, not the accidental-whole-object-dump this finding
   was about. Closed.

2. **LOW — AllowedMentions — FIXED.** `Jarvis/bot/client.py:54-59` sets
   `allowed_mentions=discord.AllowedMentions.none()` once on the `commands.Bot(...)`
   constructor. discord.py stores that as the connection-level default and applies
   it to every send path that doesn't pass its own `allowed_mentions` kwarg —
   `Message.reply`/`.send`, `InteractionResponse.send_message`, and
   `Webhook`/interaction `followup.send`. Grepped every outbound call
   (`bot/handlers.py:32` `interaction.followup.send`, `bot/handlers.py:59`
   `message.reply`, `bot/client.py:25,35,67` the three `response.send_message`/
   `followup.send` call sites) — none pass a competing `allowed_mentions` argument,
   so none override the constructor default. Closed.

3. **LOW — on_message try/except — FIXED.** `Jarvis/bot/handlers.py:41-51` now
   wraps `parse()` and `result, external_id = await _run(intent)` in
   `try/except Exception`, matching the style of the reaction/reply block right
   below it (53-61) and the `record_message` block after that (63-67). Consistent
   three-stage try/except now covers the whole handler. Closed.

### New data flow — `dispatch` → `(message, external_id)` → `record_message`

Traced end to end: `agent/tools.py`'s page-creating tools (`grocery_add`,
`task_add`) return `(line, page["id"])`; the read/check-off tools still return a
bare `str`. `agent/manager.dispatch` (`manager.py:31`) normalises both shapes to
`(message, external_id)` and never raises. `bot/handlers.py:32` (slash path)
unpacks `message, _ = await _run(intent)` — the id is explicitly discarded before
anything reaches Discord. `bot/handlers.py:48,59` (plain-message path) unpacks
`result, external_id`; only `result` (the human-readable line) is passed to
`message.reply()`. `external_id` only ever flows into
`storage/models.record_message` (`handlers.py:65`), which binds it as a plain
`?` parameter into a parameterised `INSERT OR REPLACE` (`storage/models.py:19-22`)
— no string interpolation, no f-string building SQL. The Notion page id is not a
secret (it's an opaque UUID scoped to the owner's own workspace, not a
credential), it never reaches a log line or a Discord message body, and there's
no reflection path (nothing echoes `messages.external_id` back out to Discord
anywhere in the current tool set). No injection vector, no leak. Clean.

### Re-verified since-pass-1 edits (not separate findings, checked per the brief)

- `router/fastpath.py`: `_arg()` (line 56-58) now slices the **original** `raw`
  string by the match's span (`raw[m.start(group):m.end(group)]`), matched
  against the lowercased copy — confirmed casing survives in item/task names
  without changing which text matched. The `"?" not in raw` guard (line 99) now
  sits above both the got/done branches, so `"got a minute?"` and `"done
  already?"` both fall through instead of being treated as check-offs. No
  security dimension (owner-gated before this code runs either way), matches the
  self-check block at the bottom of the file.
- `utils/dates.py`: hour/minute deltas now convert through UTC before adding
  (`parse_when`, line 115-116) so a DST boundary doesn't skip/repeat an hour;
  day/week deltas stay wall-clock. Bare ISO dates default to `_DEFAULT_TIME`
  (09:00, line 31, 98-99). Correctness-only, no security dimension.
- `integrations/notion.py`: `_set_property` (line 189-190) is a private helper
  now shared by `complete_task` and `check_off_grocery`; both still route through
  `_call`, so the token-safe error handling (type-name-only logging, generic
  `NotionError` message) still applies uniformly. No new call path bypasses
  `_call`.
- `bot/formatting.py` deletion confirmed — file no longer exists. Its renderers
  (`_format_tasks`, `_format_groceries`) now live as private (underscore-prefixed)
  helpers in `agent/tools.py:31-56`, not exported, not registered in `TOOLS`.
  Same truncation-at-`MAX_LEN` behaviour preserved.
- `DATABASE_PATH` fallback removal confirmed — grepped the whole tree;
  `config.py:90` reads only `DB_PATH`, no other module reads either name via
  `os.getenv`/`os.environ` except `utils/logging.py`'s documented `LOG_LEVEL`
  exception (pre-existing, already assessed in Pass 1).

### New findings

None. No new attack surface was introduced by this fix wave.

### Verdict (Review Pass 2)

**Can someone else access my personal information?** No. All three Pass 1
findings closed as designed; the Config redaction closes the only landmine that
existed. The new `external_id` data flow was traced end to end and introduces no
leak — the Notion page id never reaches a Discord message, only a parameterised
local DB write.

**Can anyone else make calls to Jarvis?** No. Re-traced fresh per the brief:
`bot/client.py:62` still installs `_owner_only` as the global
`bot.tree.interaction_check` (covers `/ping`, `/todo`, `/grocery` regardless of
registration order — `todo.setup`/`grocery.setup` at lines 69-70 just call
`bot.tree.add_command`, they don't touch the check). `bot/handlers.py:38` still
gates `on_message` with `is_owner(message.author.id)` before `parse()` ever
runs. No new entry point (no reaction/component/modal handler) was added in this
wave. Allowlist holds end to end.

---

## Cycle 2 Review — Phases 3 & 4

**Date:** 2026-09-07
**Scope:** New/changed only, per the cycle brief — `integrations/gcal.py` (new),
`bot/handlers.py` (`PENDING`, `confirm`, `on_raw_reaction_add`), `bot/formatting.py`
(new), `bot/commands/agenda.py` + `event.py` (new), `bot/client.py`, `agent/tools.py`,
`agent/manager.py`, `config.py`, `router/fastpath.py`, `router/intents.py`,
`utils/dates.py`. Cross-checked against `integrations/notion.py`'s `_call` (the
pattern `gcal.py` is required to mirror) and against Pass 1/2 findings above for
regressions. Method: full read of every listed file, hand-traced the reaction
dispatch path statement-by-statement for the atomicity/self-trigger/double-fire
questions below (not taken on the implementer's comments), ran the three pure
self-checks (`fastpath.py`, `utils/dates.py`, `integrations/gcal.py` `__main__`
blocks — none touch the network or require credentials) to confirm they still pass.
Did not open `.env` or `secrets/`. No files edited under `Jarvis/` or `tests/`;
the full suite was not re-run (optional per brief, and out of scope for a
report-only pass).

### Findings

**LOW — non-owner reaction on ANY message logs a warning, not just on a live confirmation.**
`Jarvis/bot/handlers.py:73-75` (`on_raw_reaction_add`). Order today: bot-reaction
filter → `is_owner` (logs + returns if false) → `PENDING.get`. Because the
owner check runs before the pending lookup, any guild member reacting with any
emoji to any message anywhere the bot can see — not just a confirmation embed —
produces `log.warning("Ignored confirmation reaction from non-owner %s", ...)`.
Harmless today (console-only, single-tenant guild), but the message name
("confirmation reaction") is actively misleading for 99% of what will trigger it,
and once `#logs` is wired up in Phase 9 this becomes a free, trivial way for any
guild member to write into the owner's log channel by reacting to unrelated
messages. **Fix:** reorder to check `PENDING.get(payload.message_id)` first and
return early if `None`; only call `is_owner` (and log) once the reaction is known
to be on a tracked confirmation. Three-line reorder, no behavior change for the
real confirmation path.

**LOW — `PENDING` entries have no TTL and a failed `add_reaction` can orphan one.**
`Jarvis/bot/handlers.py:24,42-56`. Two related gaps: (1) `PENDING[message.id]` is
set before either `add_reaction` call; if `add_reaction(CROSS)` raises after
`add_reaction(CHECK)` already succeeded (rate limit, permission change, network
blip), the `except discord.DiscordException` at the bottom of `confirm()` logs
and swallows it, but the `PENDING` entry is never removed — the message now has
a real ✅ on it with no matching cleanup path if the owner never clicks it.
(2) `PENDING` has no expiry check anywhere, so a confirmation the owner ignores
(or forgets) stays live indefinitely; if they later react ✅ on that old message
out of habit, `calendar_create` re-resolves `when` (e.g. `"tomorrow"`) against
*that moment's* clock, silently booking an event on a date the owner no longer
means. Not exploitable by a non-owner (the owner-id and requester-id checks
still hold either way), so this is a correctness/staleness issue with a security-
adjacent edge (a real calendar write firing at an unintended time from a
long-stale prompt), not an access-control bypass. **Fix:** wrap the two
`add_reaction` calls so a failure pops the just-registered `PENDING` entry
before re-raising/logging; separately, store a timestamp alongside `(intent,
requester_id)` and reject the reaction (with a "this confirmation expired, run
`/event` again" reply) past a short window (a few minutes is plenty for a human
to click ✅). No SQLite table needed — this is an in-memory timestamp check, not
persistence, so it doesn't conflict with the contract's "no DB table for this."

### Ruling on the two test-agent observations

1. **Check order (`is_owner` before `PENDING`) — confirmed, real, worth fixing now.**
   Matches the LOW finding above. Not blocking for this cycle (console-only today,
   trivial fix, no live exploit path since `#logs` doesn't exist yet), but cheap
   enough that it should go in before Phase 9 rather than be revisited then.
2. **Orphaned `PENDING` entry on a failed `add_reaction`, no expiry — confirmed, real,
   worth fixing now.** Matches the second LOW finding above. Also not blocking: it
   requires either a Discord API failure at exactly the wrong moment or the owner
   manually re-triggering a long-dead prompt, and in both cases the owner-only /
   requester-only gates still hold — the risk is a mistimed write, not an
   unauthorized one. Recommend closing both in the same small patch rather than
   carrying them into Phase 5+.

### What was independently verified (not taken on trust)

- **Self-trigger loop:** Jarvis's own ✅/❌ additions in `confirm()` do fire
  `on_raw_reaction_add` (Discord dispatches `MESSAGE_REACTION_ADD` for the bot's
  own reactions too), but `payload.member.bot` is `True` for the bot's own guild
  member object and the `if payload.member is None or payload.member.bot: return`
  guard at `handlers.py:70` discards it before anything else runs. The second,
  independent `is_owner(payload.user_id)` check would also reject the bot's own
  snowflake even if the first guard were ever removed. Traced, not assumed — this
  is the exact loop the persona exists to catch, and it's closed twice over.
- **Non-owner dispatch:** blocked at `is_owner(payload.user_id)` (`handlers.py:73`)
  before the `PENDING` dict is even consulted. A non-owner reacting to a real
  confirmation embed cannot reach the dispatch branch.
- **Owner-on-someone-else's-confirmation:** structurally impossible today — `/event`
  itself is gated by the global `_owner_only` tree check (`bot/client.py:62`), so
  only the owner can ever create a `PENDING` entry, meaning `requester_id` is
  always the owner. The `payload.user_id != requester_id` check (`handlers.py:81`)
  is still correctly present as defense-in-depth against a future multi-user change,
  not doing nothing.
- **Double-reaction / mid-dispatch race:** traced statement-by-statement. Between
  `pending = PENDING.get(...)` and `del PENDING[payload.message_id]`
  (`handlers.py:77-89`) there is no `await`, so the whole read-check-delete
  sequence is atomic with respect to the event loop — asyncio can only switch
  tasks at an `await` point, and the first one is `await _run(intent)`, which sits
  *after* the `del`. A second, concurrently-scheduled `on_raw_reaction_add` task
  (e.g. a rapid double-react) will find `PENDING.get(...)` already `None` and
  return early. No double-create possible, including the "arrives mid-dispatch"
  case the brief called out specifically.
- **Restart / stale entry:** `PENDING` is a plain in-process dict (`handlers.py:24`),
  correctly `# ponytail:`-commented as an accepted ceiling. A reaction on a
  pre-restart confirmation message finds nothing in the fresh dict and is silently
  ignored (`handlers.py:78-79`) — cannot fire a stale intent across a restart. The
  narrower staleness gap that *can* still fire (same-process, long-forgotten
  entry) is the second finding above, not this one.
- **Google credential handling, mirrored against `notion.py`:** `gcal._call`
  (`gcal.py:53-63`) wraps `_events()` construction *and* the API call in the same
  try block, logs `type(exc).__name__` only (never `str(exc)`), and raises
  `CalendarError(...) from None` with a hardcoded, non-parameterized user-safe
  string — line-for-line the same shape as `notion._call`. Confirmed no call site
  anywhere in the reviewed files uses `log.exception`, `str(exc)`, or `repr(exc)`
  on a `CalendarError`/`NotionError` (`agent/manager.py:29-31` uses `log.error`
  with `%s` against the exception, whose `__str__` is the safe hardcoded message,
  not the underlying cause — `from None` plus never printing the chained
  `__context__` means even a `FileNotFoundError` or JSON-parse error naming a path
  or file fragment never surfaces). The key file's path is a validated config
  value (not secret, per the contract) and is never itself logged or sent to
  Discord; its contents are never touched outside `google-auth`'s own loader.
  `config.py`'s redacting `__repr__` was re-verified unchanged and correctly still
  excludes only the two token fields — a file path and a calendar id are
  intentionally left visible, matching the contract. Clean, no regression from
  the pattern that passed Pass 1/2.
- **Runaway/looping calls:** `_events()`/credentials are built once behind
  `@lru_cache(maxsize=1)`, not per call. `list_events` issues one bounded
  (`maxResults=50`) call with no pagination loop. No retry/backoff exists anywhere
  in `gcal.py` (correct — Phase 9 territory). The reaction path cannot re-enter
  itself: `channel.send(result)` after a dispatch posts plain text with no
  reactions attached, so it cannot spawn another confirmation cycle. `dispatch()`
  and `gcal._call` never call back into each other or into `on_raw_reaction_add`.
  Clean.
- **Fast-path cannot emit `calendar.create`:** grepped `router/fastpath.py` in
  full — the only calendar-shaped branch is `_AGENDA` → `Intent("calendar.agenda",
  ...)`; there is no code path in the file that can produce the string
  `"calendar.create"`, and the file's own self-check (`fastpath.py:176-181`)
  asserts this against several booking-shaped phrasings. Independently confirmed
  at the dispatch layer too: `calendar.create` can only reach `TOOLS[...]` via
  `on_raw_reaction_add`'s `_run(intent)` call after a ✅ — `respond()` (used by
  `/agenda`, `/todo`, `/grocery`) is never called with `calendar.create`, and
  `/event` (`bot/commands/event.py:24`) deliberately calls `confirm()`, not
  `respond()`. One dispatch path for that intent name, and it's the confirmed one.
- **Prior-pass regressions:** none. `bot/client.py:62` still installs the global
  `_owner_only` tree check; `agenda.setup`/`event.setup` just call
  `bot.tree.add_command`, same as `todo`/`grocery`. `AllowedMentions.none()`
  (`bot/client.py:58`) is set once on the `commands.Bot` constructor and covers
  the two new send sites this phase adds (`handlers.py:37` follow-up,
  `handlers.py:49` confirmation embed send, `handlers.py:105` post-dispatch
  `channel.send`) — none of the three pass a competing `allowed_mentions` kwarg.
  `on_message`'s three-stage try/except from Pass 2 is intact and now also covers
  the new `external_id` unpack. The `Config` redaction still excludes only the
  two token fields with the two new Google fields correctly left visible.

### Verdict

**Can someone else access my personal information?** No. No credential, key-file
content, or raw Google response reaches a log line, exception string, or Discord
message anywhere in the reviewed code. Matches `notion.py`'s pattern exactly.

**Can anyone else make calls to Jarvis?** No new gap. The reaction handler is a
second front door and was audited as such: self-triggering is closed twice over,
non-owner dispatch is blocked before the pending lookup, cross-user confirmation
hijacking is structurally impossible (only the owner can ever create a pending
entry), and double-dispatch is prevented by an atomic (no-`await`) check-and-delete.
The two LOW findings above are real but narrow — one is a log-noise/forward-looking
issue, the other is a staleness edge that can misfire a write's *timing*, not its
*authorization*. Neither lets a non-owner trigger, see, or redirect a calendar
write.

**Is the confirmation flow safe to point at a real calendar?** Yes. The
create-on-✅ path is single-sourced (fast-path cannot emit it, no slash command
dispatches it directly, only `on_raw_reaction_add` post-confirmation reaches it),
the self-trigger loop is closed, and the double-create race is structurally
impossible given the atomic dict operation. Recommend fixing the two LOW findings
before Phase 9 (both are small, and the second one touches write *timing*, so it's
worth closing before this flow sees real traffic) — but neither blocks shipping
Phase 3/4 as reviewed.
