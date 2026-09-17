# Recap — for the next session

Written 2026-09-11, updated 2026-09-17. Read this, then `plan/plan.md`. This file is the session handoff;
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

Branch `feat/phases-0-4`, pushed to `origin` through `65555d1`.

```
65555d1  Build Phase 6: the daily brief
895f703  Migrate Notion to the data sources API
728731b  Record that messages/ cannot power undo yet
123228d  Build Phase 5: the Layer 2 LLM fallback
```

**Phases 0–6 of `plan/plan.md` §12 are built. 555 tests passing, no network in any test.**

**Phases 1–5 are verified against live services**, not just unit tests: the bot connects,
`/ping` answers, a bare line in `#groceries` reaches Notion categorised, and a
natural-language calendar request goes fast-path miss → Layer 2 → confirmation embed →
real calendar write. Phase 6's brief is the one part still verified by tests only — it
has never actually fired at 07:00.

| Phase | What works |
|---|---|
| 0 | Credentials — see the gap below |
| 1 | `config.py` validates everything at startup, console logging, Discord client, owner allowlist, `/ping` |
| 2 | Agent Manager dispatch, Notion tasks + groceries, Layer 1 regex fast-path |
| 3 | Calendar read — `/agenda`, agenda fast-path patterns |
| 4 | Calendar write — `/event` behind a ✅/❌ confirmation |
| 5 | Layer 2 LLM fallback — OpenAI tool-use loop, spend guard |
| 6 | Daily brief — APScheduler at 07:00, `/brief` on demand, atomic day-claim |

Three modules carry embedded `__main__` self-checks that run with no config at all:
`python -m Jarvis.utils.dates`, `python -m Jarvis.router.fastpath`,
`python -m Jarvis.integrations.gcal`, plus `Jarvis.agent.tools` and
`Jarvis.storage.models`. Cheap smoke test.

---

## Technique worth repeating: mutation-check the guard

Green tests do not mean a guard is load-bearing. Twice now a security-shaped fix landed
with **no coverage at all** and the suite stayed green — found by breaking the guard on
purpose and seeing what failed, not by reading the test count.

The routine: break the guard in the file, run the suite, confirm a *named* test fails,
restore (verify byte-identical). It has been run on the round-2 tool drop, the batch
confirmation, the spend-write latch, the atomic brief claim, the render fallback, and
the brief-claim release. Two of those had no test until the mutation exposed it.

Both reviewer personas now get asked to spot-check that claim rather than take it.

## Live-service notes

`.env` is complete and all three integrations read cleanly. Setup pain worth not
repeating, all of it now written into `SETUP.md`:

- Notion addresses **data sources**, not databases. The ID in a Notion URL is the wrong
  one and fails with "is a page, not a database". §3e has the query that lists the real
  IDs; the UI does not show them anywhere.
- `databases.query` no longer exists — it is `data_sources.query`, and pages are created
  with `parent={"data_source_id": ...}`. **The 465-test suite passed before and after
  that breakage**, because it stubs at the `integrations/` boundary. Green does not mean
  the integrations work; only the §8 functional tier can say that, and it is Phase 8.
- `GOOGLE_CALENDAR_ID` is the calendar ID (an email for a primary calendar), NOT the
  iCal private URL — that URL is itself a credential.
- Both privileged Discord intents must be on in the Developer Portal, or the gateway
  refuses the connection outright.

To run it: `python -m Jarvis.main`. It is not running as a service — when the session
ends, so does the bot.

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

Phases 7–10 of plan §12, in order:

| Phase | Deliverable |
|---|---|
| **7** | Reminders — appointment lead-time pings and task nudges, idempotent across restart via `reminders_fired`. |
| **8** | Hardening — retries with backoff, rate limits, errors to `#logs`, undo via ❌, and the functional test suite. |
| **9** | Portability — Dockerfile + VPS deploy, so the laptop stops being load-bearing. |
| **10** | Control hub (GUI) — a dashboard for state at a glance. Native vs local web app is still open and depends on Phase 9. See plan §15. |

**Weather was dropped, not deferred** — it was never built, and it is now an explicit
non-goal in plan §13. The phases after it were renumbered, so anything remembering
"Phase 9 = hardening" is one out.

Also open, from plan §14: the appointment lead time (30 min, placeholder), whether the
brief should footer tomorrow's first commitment, and three cosmetic ambiguities in
`plan/jarvis-creation-flow.drawio` — which still shows a **Weather update** node that no
longer matches §2.

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
