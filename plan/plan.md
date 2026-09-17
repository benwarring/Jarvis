# Jarvis — Build Plan

An agentic secretary for one user. Discord is the interface; Google Calendar and
Notion are the systems of record; an LLM is the brain for anything the
deterministic layer can't parse.

Companion diagram: [`jarvis-creation-flow.drawio`](jarvis-creation-flow.drawio).
§2 is the prose form of that diagram — keep the two in sync.

---

## 1. Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best-documented path for discord.py + Google + Notion |
| Interface | Discord bot in a private guild | Channels give structure; a DM can't hold a `#daily-brief` |
| Hosting | Dev laptop for now | Everything below is written so moving to a VPS is a config change |
| Brain | Hybrid: deterministic fast-path, LLM fallback | Most daily traffic ("add milk") never touches the API |
| Model | `gpt-4.1-mini` via `OPENAI_MODEL` | §10 is resolved: `requirements.txt` pins `openai`, and §11 carries verified rates |
| Calendar auth | Service account + shared calendar | Zero token-refresh maintenance. Tradeoff in §7 |
| Notion | Two new databases, built from scratch | Schemas in §6 |
| Timezone | `America/New_York` | Store UTC internally, render local at the edges |

---

## 2. Creation flow

The org chart for the system: what gets built, and under what. This is the
structural view — §3 is the runtime view of how a single request moves.

```
Agent Manager
├── Tools
│   ├── Daily Jobs
│   │   └── Reminders
│   │       ├── Appointments
│   │       └── Tasks
│   └── Integrations
│       ├── Task Scheduler
│       ├── Google Calendar
│       ├── Notion
│       │   ├── Grocery List
│       │   └── To-Do List
│       └── Discord Bot
├── Testing
│   ├── Unit Testing
│   └── Functionality test
└── Reviewers
    ├── Security
    └── Standards
```

**Agent Manager** is the orchestration layer: it owns the tool registry, dispatches
an inbound intent to the right capability, and is the single place that knows what
Jarvis can do. Everything else hangs off it.

Two labels above are interpretations of the diagram, flagged in §14 as open
questions: the diagram's second nested `Tools` node is rendered here as
**Integrations** (a category of tools cannot usefully be named the same as its
parent), and the `Notion` node nested under `Notion` is rendered as **To-Do List**,
since its sibling is `Grocery List` and those are the two databases in §6.

### What this adds over the previous plan

| From the diagram | Status before |
|---|---|
| **Agent Manager** as an explicit layer | Implicit — the router did dispatch with no named owner |
| **Reminders** (appointments + tasks) | New. Distinct from the daily brief: nudges through the day, not one 07:00 post |
| **Task Scheduler** as a peer integration | Was buried as "APScheduler inside `scheduler/`" |
| **Testing** as a first-class branch | Was one line in the build phases |
| **Reviewers** (Security + Standards) | Existed as persona files, absent from the plan |

---

## 3. Runtime architecture

How one Discord message becomes an action. Complements §2 — same system, different axis.

```
                    Discord (private guild "Jarvis HQ")
                 #inbox      #daily-brief      #logs
                    |             ^              ^
                    v             |              |
           +----------------------------------------+
           |  bot/  - gateway client, allowlist,     |
           |          slash commands, message handler|
           +-------------------+--------------------+
                               v
           +----------------------------------------+
           |  agent/  - Agent Manager: tool registry,|
           |            intent dispatch              |
           +-------------------+--------------------+
                               v
           +----------------------------------------+
           |  router/  - Layer 0  slash commands     |
           |             Layer 1  regex fast-path    |
           |             Layer 2  LLM tool-use       |
           +-------------------+--------------------+
                               v   (a resolved Intent)
           +----------------------------------------+
           |  integrations/  gcal | notion | llm     |
           +-------------------+--------------------+
                               v
           +----------------------------------------+
           |  scheduler/  task scheduler -> daily    |
           |              jobs, reminders            |
           |  storage/    SQLite (state, idempotency)|
           +----------------------------------------+
```

### Directory layout

```
Jarvis/
  main.py              entrypoint: boots Discord client + scheduler
  config.py            loads .env, validates required keys, fails loud at startup
  agent/
    manager.py         Agent Manager: tool registry, intent dispatch
    tools.py           tool definitions shared by all three router layers
  bot/
    client.py          discord.py client, intents, ready hook
    commands/          slash command definitions
    handlers.py        on_message -> router
    formatting.py      the ✅/❌ confirmation embed. List rendering is NOT here —
                       it lives as private helpers in agent/tools.py, so the
                       capability layer never imports the presentation layer
  router/
    intents.py         the Intent schema every layer produces
    fastpath.py        Layer 1 regex/keyword matching
    llm.py             Layer 2 LLM tool-use loop
  integrations/
    gcal.py            list_events, create_event, free_slots. free_slots takes events
                       rather than fetching them, so the brief reads the day once and
                       the agenda and the gaps cannot disagree
    notion.py          add_task, add_grocery, query_open_tasks, complete_task
    llm.py             LLM client wrapper + cost accounting. Tool *schemas* live in
                       agent/tools.py beside the implementations they describe, so
                       the two cannot drift apart
  scheduler/
    jobs.py            APScheduler; the 07:00 brief, and the atomic day-claim that
                       stops a restart posting a second one
    planner.py         daily brief construction: build_plan and render_plain are
                       deterministic, render adds the one LLM call and falls back to
                       render_plain on any failure
    reminders.py       appointment + task nudges
  storage/
    db.py              SQLite connection, migrations
    models.py          row helpers
  utils/
    dates.py           natural-language date parsing, tz conversion
    logging.py         structured logging -> console + #logs
tests/
  unit/                pure-function tests, no network
  functional/          end-to-end against sandbox accounts
docs/
```

The two reviewer personas in `.claude/personas/` audit against this file, so keep
it current. If the code diverges from this layout, update the plan or the code —
not neither.

---

## 4. Request lifecycle — the hybrid router

Every inbound Discord message walks three layers and stops at the first hit. The
Agent Manager owns the dispatch; the layers differ only in how the intent is
*derived*, never in what executes it.

**Layer 0 — Slash commands.** Zero cost, zero ambiguity, and the escape hatch for
when natural language misfires.

```
/todo add <text> [due] [priority]     /todo list     /todo done <query>
/grocery add <item> [qty]             /grocery list  /grocery got <query>
/event <title> <when> [duration]      /agenda [day]
/brief
```

**Layer 1 — Deterministic fast-path.** Regex + keyword matching on plain messages.
This is the cost lever: it should catch the majority of daily traffic.

| Pattern | Intent |
|---|---|
| `add <x> to (the )?grocer...`, `buy <x>`, `we need <x>` | `grocery.add` |
| `remind me to <x>`, `todo: <x>`, `add <x> to my todo` | `task.add` |
| `what's on my plate`, `what do i have (today\|tomorrow)` | `agenda.read` |
| `done: <x>`, `finished <x>`, `got <x>` | `task.complete` / `grocery.check` |
| Bare list lines posted in `#groceries` | `grocery.add` |

`done` and `got` take a **substring of the item**, not a list index. An index looks
tidier but shifts under you the moment anything else is checked off between `/list`
and `/done`; a substring that refuses to guess when it matches two things is safer
on a list you actually act on.

Fast-path parses are **confirmed by reaction, not by prose** — Jarvis reacts ✅ to
your message rather than replying. Cheap, quiet, and reversible (❌ to undo).

The exception is anything that reads or resolves: list, check-off and complete
intents react ✅ *and* reply with one line. An emoji cannot carry a list, and a
check-off that silently matched nothing — or matched the wrong item — is exactly
the failure you need to see. Adds stay reaction-only.

**Layer 2 — LLM tool-use loop.** Anything Layer 1 misses. The tools are the same
functions Layers 0 and 1 call, registered once in `agent/tools.py`, so there is
exactly one implementation of "create an event."

Tools exposed: `create_calendar_event`, `list_calendar_events`, `add_task`,
`list_tasks`, `complete_task`, `add_grocery_item`, `list_groceries`,
`check_off_grocery`, `build_daily_schedule`.

**Write actions get a confirmation step.** Calendar writes and any multi-item batch
render as an embed with ✅/❌ reactions before executing. Reads execute immediately.

This guardrail matters *more* now that the configured model is a small one. A
cheaper model misparses more often, so the confirmation step is doing real work —
do not optimize it away to save a round trip.

---

## 5. Daily jobs

Two scheduled capabilities, all driven by the Task Scheduler (APScheduler,
in-process — no dependency on Windows Task Scheduler, and it survives the move to
a VPS unchanged).

### 5a. Daily brief

Runs at 07:00 local. Posts to `#daily-brief`. Deliberately **deterministic first,
LLM last** — gap-finding is arithmetic, not judgment:

1. Pull today's Google Calendar events (busy blocks).
2. Pull open Notion tasks: overdue, due today, or high-priority with no due date.
3. Compute free gaps between busy blocks inside waking hours (default 08:00–22:00),
   discarding gaps shorter than 20 minutes.
4. Greedily fit tasks into gaps by `(priority DESC, due_date ASC)`, using each
   task's `Estimate` (default 30 min when unset).
5. **One** LLM call turns that structure into readable prose and flags conflicts or
   an over-committed day. If the call fails, fall back to a plain template — the
   brief must never fail to post because of an API error.

The same code path serves `/brief` on demand.

### 5b. Reminders

Distinct from the brief: the brief is one 07:00 summary, reminders are nudges
through the day. A poll every `REMINDER_POLL_MINUTES` (default 15), entirely
deterministic:

- **Appointments** — an `#inbox` ping at `REMINDER_LEAD_MINUTES` (default 30) before a
  calendar event starts. Only for an event that is *still ahead of you*: one already
  under way, or already over, is never pinged, so a late poll or a restart mid-meeting
  stays quiet. An all-day event has no start to be early for and never pings.
- **Tasks** — a nudge for anything due today still marked `Not started` as its due
  time approaches, plus a single end-of-day sweep for what slipped. The sweep says
  nothing at all when nothing is outstanding.

Every reminder is *claimed* in SQLite before it is sent, so two overlapping polls or a
restart cannot double-notify. The claim is deliberately never released: unlike the
brief, which releases its day and retries because losing it is that phase's whole
failure mode, a reminder is one of many and pinned to a moment — a duplicate ping is
worse than a missed one, so a failed send is logged and dropped.

No LLM call anywhere in this path — the trigger is a timestamp comparison and the
message is a template. It runs every quarter hour forever, which makes it the easiest
place in the project to acquire a recurring bill by accident.

---

## 6. Data model

### Notion — Tasks database

Property names are declared once as constants at the top of `integrations/notion.py`
rather than inline at each call site. The live database is the authority, not this
table: rename a property in Notion and exactly one line changes in the code.

Two API facts that cost an afternoon, recorded so they do not cost another:

- **Addressing is by *data source* ID, not database ID.** Notion's current API puts one
  or more data sources under a database, and reads and writes address the data source.
  The ID in a Notion URL is not it, and the failure says "is a page, not a database".
  `SETUP.md` §3e has the query that lists the real IDs.
- **`databases.query` no longer exists**; it is `data_sources.query`, and a page is
  created with `parent={"data_source_id": ...}`. Unit tests stub at the
  `integrations/` boundary, so they cannot catch this class of breakage — only the §8
  functional tier can.

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | The task |
| `Status` | Status | `Not started` / `In progress` / `Done` |
| `Due` | Date | Optional; date-only or with time |
| `Priority` | Select | `High` / `Medium` / `Low` |
| `Estimate` | Number | Minutes. Drives the scheduler's gap-fitting |
| `Project` | Select | Free-form bucket. Set by hand in Notion — Jarvis neither reads nor writes it yet |
| `Notes` | Rich text | |
| `Source` | Select | `discord` / `manual` — lets you audit what Jarvis created |

### Notion — Groceries database

| Property | Type | Notes |
|---|---|---|
| `Item` | Title | |
| `Qty` | Rich text | Free-form ("2 lbs", "a bunch") — resist making this a Number |
| `Category` | Select | `Produce` / `Dairy` / `Meat` / `Pantry` / `Frozen` / `Household` / `Other` |
| `Got it` | Checkbox | |
| `Added` | Created time | Automatic |

Category is auto-assigned by the fast-path from a static keyword map
(`milk` -> Dairy), falling back to `Other`. No API call needed for the common case.

### SQLite (local, `jarvis.db`)

| Table | Purpose |
|---|---|
| `messages` | Discord message ID -> resolved intent -> resulting Notion/GCal ID. Powers idempotency. **Not yet sufficient for undo:** the Discord message id is the primary key, so a confirmed multi-write batch (Layer 2 can propose one) overwrites its own row and only the last write survives. Phase 8 needs a row per write before ❌-undo can be honest |
| `briefs` | One row per generated brief; prevents double-posting after a restart |
| `reminders_fired` | One row per reminder *claimed for sending*, not per delivered one — a failed send leaves the row, by design (§5b) |
| `conversations` | Rolling short-term context for Layer 2 multi-turn. **Not built** — Layer 2 is single-turn, and a rolling context multiplies both tokens and prompt-injection surface for no demonstrated need |
| `llm_spend` | One row per local day: tokens and USD. Backs the §11 spend guard |
| `cache` | Notion database schema cache, so properties aren't re-fetched every call |

**One connection, shared across threads — a known hazard, to be fixed in Phase 8.**
`db.connect()` is a single `@lru_cache`d connection with `check_same_thread=False`.
Concurrent *statements* are safe (SQLite is built serialized here, and every claim is a
single atomic `INSERT ... ON CONFLICT`), and that is pinned by a test: eight threads
released on a barrier produce exactly one winner.

The transaction is the soft spot. `with conn:` commits or rolls back the **whole shared
connection**, so a thread that has already been told `rowcount == 1` can lose its row to
a *different* thread's write failing inside its own `with conn:`. The consequence is a
reminder delivered twice, or a spend row lost — which under-counts against
`LLM_DAILY_SPEND_LIMIT_USD` and so weakens the cost guard rather than strengthening it.

It has been reproduced deliberately, but it needs a concurrently *failing* write, and
every writer here is upsert-shaped (`OR REPLACE` / `ON CONFLICT DO NOTHING` /
`DO UPDATE`) and cannot raise `IntegrityError` on its own — so today it takes something
exogenous: a disk error, a lock timeout, an interrupt mid-shutdown. It predates Phase 7;
Phase 7 is what turned concurrent writers from theoretical into routine, by running the
poll in a worker thread alongside the brief job and the Discord handlers.

Fix in Phase 8: a connection per thread (`threading.local`), or one lock around writes.

---

## 7. Auth strategy

**Google Calendar — service account, calendar shared to it.** Create a service
account, take its `...iam.gserviceaccount.com` address, and share your personal
calendar with it granting *Make changes to events*. The app authenticates with a
JSON key file and never touches a refresh token.

The tradeoff, stated plainly: a service account cannot reliably send invitation
emails to guests, and events it creates show it as the organizer. For solo
scheduling — the entire scope here — neither matters. If you later want Jarvis to
invite other people, switch to the OAuth desktop flow; if you do, set the OAuth
consent screen to **In production**, not Testing, or refresh tokens silently expire
every 7 days.

**Notion — internal integration token.** Each database must be explicitly connected
to the integration via its `•••` -> Connections menu. Sharing the parent page is not
enough, and this is the single most common reason a Notion call returns 404.

**Discord — bot token + MESSAGE CONTENT INTENT.** The intent is privileged but
self-serve for bots in under 100 servers. Without it, `message.content` arrives
empty and Layer 1 never fires.

**Everything is allowlisted to one person's Discord user IDs.** `DISCORD_OWNER_USER_ID1`
is required and `DISCORD_OWNER_USER_ID2` is optional, so one human running two accounts
gets both without the system becoming multi-user — the allowlist is a set, and every
command path tests membership through the single `is_owner` in `bot/client.py`. A
confirmation is still answered by the account that requested it, so a `/event` started
on one account cannot be confirmed from the other. Jarvis touches a real
calendar; a bot that takes commands from anyone in the guild is a bad idea even in
a guild of one.

---

## 8. Testing

Two tiers, mirroring the diagram. `tests/` mirrors the `Jarvis/` package layout.

**Unit testing** — pure functions, no network, run on every change. The highest-value
targets are the pieces where a bug is silent rather than loud:

- Fast-path regexes: every pattern in §4, plus the near-misses that must *not* match.
- Gap-finding arithmetic: overlapping events, all-day events, an empty day, a
  fully-booked day, a gap exactly at the minimum threshold.
- Timezone conversion across a DST boundary — the bug that surfaces twice a year.
- Grocery category keyword mapping.
- Reminder trigger logic: fires once, and only once, across a simulated restart.

**Functionality test** — end-to-end against real APIs using **separate sandbox
accounts**: a test Discord guild, a throwaway Google calendar, duplicate Notion
databases. Run before a release, not on every commit.

Never point functional tests at the live calendar or the real to-do list. The
sandbox credentials belong in `.env.test`, gitignored alongside `.env`.

External APIs are stubbed at the `integrations/` boundary for unit tests, which is
the reason that boundary exists.

---

## 9. Review gates

Two personas in `.claude/personas/`, writing to `.claude/logs/`:

| Persona | Reviews for |
|---|---|
| **Security** (`security-reviewer.md`) | Credential handling, the owner allowlist on every command path, injection surface in anything reaching an API, secrets never reaching a log or a Discord message |
| **Standards** (`standards-reviewer.md`) | Repeated code, modularity, helper-function use, and conformance to this plan |

Both audit against this document, which is why §1 and §3 have to stay accurate —
a stale plan turns the standards review into noise.

Run both at the end of every build phase in §12, not just at the end of the project.

---

## 10. Provider inconsistency — RESOLVED

`.env` configures `OPENAI_API_KEY` and `OPENAI_MODEL` with no Anthropic keys present,
so OpenAI is the provider. The markdown was made consistent with that first, and
`requirements.txt` now pins `openai` instead of `anthropic` — settled deliberately at
the start of Phase 5, as this section required, rather than drifting into it.

Historical note, kept because it explains the delay: the swap was held back through
Phases 1-4 because it is a dependency change rather than a doc fix, and doing it
silently would have installed one SDK and orphaned another.

The §11 figures were re-baselined against OpenAI's published pricing at the same time.

---

## 11. Cost model

Layers 0 and 1 are free. Only Layer 2 and the daily brief cost anything.
Reminders are deterministic and cost nothing.

The lever that matters is fast-path hit rate, not model choice — every message
Layer 1 catches is a request that never happens. That holds regardless of provider.

Rates for the configured model, `gpt-4.1-mini`, verified against OpenAI's pricing
docs on 2026-09-11: **$0.40 per 1M input tokens, $1.60 per 1M output tokens** (standard
tier). The same table in `integrations/llm.py` is hand-maintained and carries the source
URL and that date — it is the one number in the codebase that goes stale silently, so
re-check it when the spend guard starts behaving oddly in either direction.

An unpriced model falls back to the most expensive known rate. That direction is
deliberate: over-charging trips the guard early and visibly, while under-charging
disables it quietly.

Two structural points, independent of provider:

- **Skip prompt caching at first.** Caching pays off across requests that share a
  large prefix inside a short window. A personal bot's traffic is sporadic single
  messages, so a cache would mostly be written and never read. Revisit only if you
  find yourself in genuine back-and-forth bursts.
- **Keep the model ID in `.env`, never in code**, so changing model or provider
  stays a config change.

Keep the daily spend guard: a counter in SQLite that disables Layer 2 past
`LLM_DAILY_SPEND_LIMIT_USD`, falling back to "I didn't catch that — try a slash
command."

---

## 12. Build phases

Each phase ends with something usable, so the project is never a half-built lump.
Each phase also ends with a §9 review pass.

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | Accounts + credentials | `.env` fully populated. See `SETUP.md` |
| **1** | Skeleton | `config.py`, logging, Discord bot connects, allowlist works, `/ping` responds |
| **2** | Agent Manager + Notion + fast-path | Tool registry dispatches; `/todo add`, `/grocery add`, and the Layer 1 regexes work. **No LLM yet** — this phase delivers most of the daily value |
| **3** | Calendar read | `/agenda` renders today's events |
| **4** | Calendar write | `/event` creates events, behind the ✅ confirmation |
| **5** | LLM fallback | Layer 2 tool-use loop. Resolve §10 first |
| **6** | Daily brief | Task Scheduler posts to `#daily-brief` at 07:00; `/brief` on demand |
| **7** | Reminders | Appointment lead-time pings and task nudges, idempotent across restart |
| **8** | Hardening | Retries with backoff, rate-limit handling, errors to `#logs`, undo via ❌ (needs the `messages` rework in §6 first — one row per write, not per Discord message), the shared-connection fix in §6, and the §8 functional suite. The spend guard already shipped in Phase 5 |
| **9** | Portability | Dockerfile + documented VPS deploy, so the laptop stops being load-bearing |
| **10** | Control hub (GUI) | A single screen for Jarvis: today's schedule, open tasks, the grocery list, recent activity, and what Layer 2 has cost this month. See §15 |

Unit tests (§8) are written alongside each phase, not batched at the end. The
functional suite lands with Phase 8.

Phase 2 is the one to resist rushing past. A bot that reliably adds groceries at
zero API cost is already worth having on your phone.

---

## 13. Explicit non-goals (for now)

- Weather. Dropped, not deferred — it was never built, and the brief is more
  useful for being one thing done well than two things half-wired.
- Multi-user support. One allowlisted *person* throughout — who may hold more than one
  Discord account (§7). There is still no per-user data, no separate calendars, and no
  notion of "whose" a task is.
- Email. Deliberately out of scope — it multiplies the auth and privacy surface.
- Rescheduling or moving existing calendar events (read + create only through Phase 6).
- Recurring event creation.
- Voice.

---

## 14. Open questions

**From the creation-flow diagram:**

- The diagram nests a `Tools` node inside `Tools`. Read here as **Integrations** —
  confirm, or rename the node in the `.drawio`.
- The diagram nests a `Notion` node inside `Notion`, sibling to `Grocery List`.
  Read here as **To-Do List** — confirm, or rename the node.
- `Reminders -> Appointments` is drawn twice (two identical edges). Cosmetic; worth
  deleting one so the diagram doesn't imply two distinct paths.

**Carried over:**

- Waking-hours window for the scheduler — 08:00–22:00 is a placeholder.
- Should the brief include tomorrow's first commitment as a footer?
- Do groceries need a "shopping trip" concept, or is a flat checked/unchecked list enough?
- Appointment reminder lead time — 30 minutes is a placeholder, and it probably
  wants to vary by whether travel is involved.
- **Phase 10's hub: native desktop or local web app?** It interacts with Phase 9 —
  a native app assumes Jarvis runs on the same machine, so moving to a VPS would then
  need an API built for it. Decide once Phase 9 is real, not before. See §15.

---

## 15. Control hub (Phase 10)

A GUI that acts as the central place to see and steer Jarvis, rather than a second
way to type at it. Discord stays the conversational interface — this is the dashboard
Discord is bad at: state at a glance instead of a scrollback.

### What earns a place on it

- **Today** — the calendar, open tasks, and the computed schedule side by side. The
  same structure §5a's brief builds, shown rather than narrated.
- **Lists** — tasks and groceries, editable. Checking something off here does exactly
  what ✅-ing it in Discord does.
- **Activity** — what Jarvis did and when, from the `messages` table. This is the
  first thing that makes the local SQLite worth having beyond idempotency.
- **Cost** — Layer 2 spend today and this month from `llm_spend`, against the
  `LLM_DAILY_SPEND_LIMIT_USD` ceiling. A number that currently exists but is invisible.
- **Health** — is the bot connected, did the 07:00 brief post, is each integration
  reachable. Today the honest answer to "is it working" is "read the console".

### Constraints it inherits

- **No second implementation.** The hub calls the same `agent/tools.TOOLS` entries
  through `agent/manager.dispatch`. The rule that has held for slash commands, the
  fast-path and the LLM holds here — a hub that writes its own "create an event" is
  the bug §4 keeps warning about.
- **Writes still confirm.** Calendar writes and multi-item batches need the same
  confirmation the ✅ flow gives them. A button press is a confirmation; a button that
  silently books is not.
- **The allowlist still applies.** Whatever the transport, the hub is a command path,
  and every command path checks the caller. A local-only bind is not an access control.

### The decision that shapes it

**Native desktop or local web app**, and it interacts with Phase 9. A native app
talks to a bot on *this* laptop fine, but the moment Jarvis moves to a VPS it needs an
API to talk to — so choosing native quietly adds a server phase later. A local web app
(FastAPI serving a small page, or the same served from the VPS behind auth) costs a
little more now and nothing later.

Deferred deliberately until Phase 9 is real, because where Jarvis runs decides what
the hub can be. Flagged in §14.

---
