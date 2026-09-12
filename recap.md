# Recap — for the next session

Written 2026-09-11. Read this, then `plan/plan.md`. This file is the session handoff;
`plan/plan.md` is still the specification and the thing the reviewer personas audit
against.

---

## How this build is being run

The user asked for a specific orchestration, and it has worked well enough to keep:

> tools → tests → review → review → repeat. Before repeating, read the reviewer logs
> and brief the tool agents from them.

Concretely, per cycle:

1. **I act as Agent Manager.** I own the interface contract and the spec. I do not write
   feature code; I write the contract, make the rulings, and update `plan/plan.md`.
2. **Two Opus tool subagents in parallel** — split "foundation" (config, utils, storage,
   router, integrations) vs "surface" (agent manager, bot, commands, entrypoint). They
   code against a contract file in the scratchpad so they can run concurrently without
   colliding on interfaces. Define the contract FIRST; this is the whole reason the
   parallelism works.
3. **One Opus test subagent** — owns `tests/` exclusively. Tool agents are explicitly
   forbidden from editing tests, which is what stops "fix the test until it passes".
4. **Two Sonnet reviewer subagents** — the personas in `.claude/personas/`, run in
   parallel, **report-only** (they may write only their own log). They do not edit code.
5. **I read both logs**, make rulings, and brief a fix wave.

Contracts live in the session scratchpad: `contract.md` (Phases 1–2),
`contract-phase34.md`, `contract-phase5.md`. Those are gone when the session resets —
rewrite one per cycle. They do not need to be preserved; `plan/plan.md` plus the review
logs carry the decisions forward.

**The reviewers earn their keep.** They have caught things no test did, and the two
personas catch *different* things — run both. Real examples: a DST off-by-one in relative
date math, items being written to Notion lowercased, `external_id` silently NULL,
`agent/` importing `bot/` (layering inversion), a pending confirmation re-parsing
"tomorrow" against the click moment and booking the wrong day, and indirect prompt
injection through Notion item titles replayed into a tool-calling round.

Three times a review finding was resolved by **changing the plan rather than the code** —
that is a legitimate outcome and the log records the reasoning.

---

## State

Branch `feat/phases-0-4`, pushed to `origin` through `614a0ba`.

```
614a0ba  Allow one owner to hold two Discord accounts
4a7580a  Track the plan and the review logs
2aaa9f2  Build Phases 1-4: bot skeleton, Notion, fast-path, and calendar
b86b856  Add Phase 0 scaffolding and align docs with creation flow
```

**Phases 0–5 of `plan/plan.md` §12 are built. 458 tests passing, no network in any test.**

| Phase | What works |
|---|---|
| 0 | Credentials — see the gap below |
| 1 | `config.py` validates everything at startup, console logging, Discord client, owner allowlist, `/ping` |
| 2 | Agent Manager dispatch, Notion tasks + groceries, Layer 1 regex fast-path |
| 3 | Calendar read — `/agenda`, agenda fast-path patterns |
| 4 | Calendar write — `/event` behind a ✅/❌ confirmation |
| 5 | Layer 2 LLM fallback — OpenAI tool-use loop, spend guard |

Three modules carry embedded `__main__` self-checks that run with no config at all:
`python -m Jarvis.utils.dates`, `python -m Jarvis.router.fastpath`,
`python -m Jarvis.integrations.gcal`, plus `Jarvis.agent.tools` and
`Jarvis.storage.models`. Cheap smoke test.

---

## Phase 5 review fixes — DONE (was in flight, now landed)

The fix agent hit the monthly spend limit mid-run, but had already applied the code
changes; they were verified by hand afterwards, the tests were brought back to green,
and each fix was **mutation-checked** — break it, watch a named test fail, restore.
Suite: **465 passing**.

What the three fixes were, and the test that pins each:

| Fix | Pinned by |
|---|---|
| Round 2 gets no tools | `test_round_two_is_handed_no_tools_at_all` |
| Batches confirm as a unit | `test_a_batch_of_plain_writes_is_held_even_with_no_calendar_write` |
| Spend write fails closed | `test_a_failed_spend_write_shuts_layer_2_until_restart` |

Two of those three had **no coverage at all** when the code landed — the mutation check
is what found that, not the test run. Worth repeating the technique on the next
security-shaped fix.

Still to do here: **run both reviewer personas over these fixes.** The loop is
tools → tests → review → review, and the review half has not happened for this wave.

The fixes, for context:

1. **[MEDIUM, security] Drop `tools=` on round 2** of the tool loop. Round 1's tool
   output is replayed to the model, and it can contain a Notion task/grocery title —
   text anyone with Notion access can write. With schemas still attached, a crafted
   title could drive an immediately-executing write (`grocery.add`, `task.complete`)
   with no ✅. Round 2 only ever needed to produce prose.
2. **[HIGH, standards] Multi-item batches must be confirmed.** `CLAUDE.md` and plan §4
   both require ✅ for "calendar writes **and multi-item batches**", but only
   `calendar.create` was gated. One Layer 2 reply with three `add_task` calls executed
   all three. The fix generalises `PENDING` from a single `Intent` to a **tuple** of
   Intents (single case becomes a 1-tuple) so there is one code path, not two. Two or
   more writes in a reply → execute none, confirm the batch. A single write stays
   immediate, matching the fast-path.
3. **[LOW, security] Spend guard fails closed on write too.** Reads already treated an
   unreadable counter as over-budget; a write failure left the counter stale and let
   spending continue past the cap. Asymmetry favoured spending.

If it landed, `tests/unit/test_confirmation.py` almost certainly needs updating for the
new `PENDING` shape — that is the **test agent's** job, not a tool agent's. A failing
confirmation *guard* test means a guard broke; fix the code, not the test.

Then: commit, and run both reviewers once more over the fix.

---

## The one thing only the user can do

**`DISCORD_GUILD_ID` is missing from `.env`.** Every other key is present. Until it is
added, `get_config()` correctly refuses to boot and names it.

**The bot has never actually been run.** Phases 1–5 are verified by unit tests only.
Once the guild id is in, the first real milestone is Phase 1's "done when": the bot
connects and `/ping` answers. Expect to find integration problems that no stub caught.

---

## Traps, learned the hard way

- **Never read `.env` or `secrets/`.** The user has live Notion, Google and OpenAI
  credentials there. To inspect it, list key *names* only:
  `grep -oE "^[A-Za-z_][A-Za-z0-9_]*=" .env | tr -d '='`.
- **`tests/conftest.py` neuters `dotenv.load_dotenv` at the source** before the first
  Jarvis import, and forces `llm._client` to raise. This is not paranoia: the suite
  *was* loading the user's real `.env` — including a live OpenAI key — into the test
  process, and passing only by import-order accident. Two regression tests guard it.
  If you see `SystemExit: Configuration error` in tests, add the key to `FAKE_ENV`;
  do not weaken the stubbing.
- **Bash heredocs fail in this environment** (`unexpected EOF while looking for
  matching`). Use the Write tool for any multi-line content, including commit messages
  (`git commit -F <file>`).
- **`plan/` and `.claude/logs/` are tracked now.** They were gitignored; `plan/` was
  sitting in the *secrets* block, and a bare `logs/` rule was swallowing the review
  logs. Fixed in `4a7580a`.
- **The pricing table in `integrations/llm.py` is hand-maintained** — `gpt-4.1-mini` at
  $0.40/$1.60 per 1M tokens, verified 2026-09-11, source URL in the comment. It is the
  one number in the codebase that goes stale silently. An unpriced model falls back to
  the most *expensive* known rate on purpose: over-charging trips the guard visibly,
  under-charging disables it quietly.
- **`requirements.txt` took four phases to settle.** It pinned `anthropic` while `.env`
  configured OpenAI. Now `openai`. Plan §10 is marked RESOLVED.

---

## Deliberate deferrals — do NOT "fix" these

Each was argued and ruled on; re-litigating them wastes a cycle.

- **`utils/logging.py` reads `LOG_LEVEL` via `os.getenv`**, the only such read outside
  `config.py`. Both reviewers ruled it correct: `get_logger()` runs before
  `get_config()`, so routing it through `Config` would force eager validation during
  test collection.
- **`llm.py` uses an inline try/except** rather than a `_call` wrapper like
  `notion.py`/`gcal.py`. There is exactly one call site; a wrapper would be an
  abstraction with one caller.
- **Check-then-record TOCTOU in the spend guard.** One user messaging sequentially
  overshoots by a fraction of a cent.
- **`conversations` table / multi-turn Layer 2 context.** Listed in plan §6, marked not
  built. It multiplies tokens *and* prompt-injection surface.
- **A confirmation is answered by the account that requested it.** With two owner
  accounts, `/event` on one cannot be confirmed from the other. The security review
  ruled this buys little access control but does stop a misfire being approved from the
  wrong device. The user has not asked to relax it.
- **`todo.py`/`grocery.py`/`agenda.py`/`event.py` look near-identical.** Ruled genuine
  parallel structure twice; merging them makes the code worse.
- **The Notion `Project` property** is hand-set in Notion, not written by Jarvis.
- **`find_free_slots`** is Phase 6's, when the brief is the first thing to consume it.

---

## What is left

Phases 6–10 of plan §12, in order:

| Phase | Deliverable |
|---|---|
| **6** | Daily brief — APScheduler posts to `#daily-brief` at 07:00, `/brief` on demand. Gap-finding is **arithmetic**, exactly one LLM call for prose, plain-template fallback if it fails. This is where `find_free_slots` lands. |
| **7** | Weather — **provider not chosen yet** (plan §14). Decides a `.env` key and a `SETUP.md` section. |
| **8** | Reminders — appointment lead-time pings and task nudges, idempotent across restart via `reminders_fired`. |
| **9** | Hardening — retries with backoff, rate limits, errors to `#logs`, undo via ❌, and the functional test suite. |
| **10** | Portability — Dockerfile + VPS deploy, so the laptop stops being load-bearing. |

Phase 6 was cleared to start by both reviewers regardless of the in-flight fix, since the
brief touches neither the tool loop nor the confirmation flow.

Also open, from plan §14: the waking-hours window (08:00–22:00 is a placeholder), the
appointment lead time (30 min, placeholder), whether the brief should footer tomorrow's
first commitment, and three cosmetic ambiguities in
`plan/jarvis-creation-flow.drawio`.

---

## Standing constraints

From `CLAUDE.md`, and they are not negotiable:

- Every command path checks the owner allowlist before doing anything. That includes any
  new path — the reaction handler needed it, and so will the next one.
- Calendar writes and multi-item batches require ✅ before executing.
- One implementation per capability. Slash commands, the fast-path and the LLM loop all
  route through `agent/manager.dispatch` into `agent/tools.TOOLS`. A second "create an
  event" is a bug.
- Store UTC, render local; conversion only in `utils/dates.py`.
- No credential in a log line, a Discord message, an error trace, or a test fixture.
- The model id comes from `OPENAI_MODEL`, never a literal.
- If code diverges from `plan/plan.md`, update the plan in the same commit.
