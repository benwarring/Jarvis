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
| Model | `gpt-4.1-mini-2025-04-14` via `OPENAI_MODEL` | Matches the configured `.env`. See §10 — the cost section and `requirements.txt` still assume Anthropic and need reconciling |
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
│   │   ├── Weather update
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
| **Weather update** | Entirely new. Needs a provider and a credential — see §14 |
| **Reminders** (appointments + tasks) | New. Distinct from the daily brief: nudges through the day, not one 07:00 post |
| **Task Scheduler** as a peer integration | Was buried as "APScheduler inside `scheduler/`" |
| **Testing** as a first-class branch | Was one line in Phase 7 |
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
    gcal.py            list_events, create_event (find_free_slots lands in Phase 6,
                       when the daily brief is the first thing to consume it)
    notion.py          add_task, add_grocery, query_open_tasks, complete_task
    weather.py         current conditions + daily forecast
    llm.py             LLM client wrapper, tool schemas
  scheduler/
    jobs.py            registered cron jobs
    planner.py         daily brief construction
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
/weather [day]                        /brief
```

**Layer 1 — Deterministic fast-path.** Regex + keyword matching on plain messages.
This is the cost lever: it should catch the majority of daily traffic.

| Pattern | Intent |
|---|---|
| `add <x> to (the )?grocer...`, `buy <x>`, `we need <x>` | `grocery.add` |
| `remind me to <x>`, `todo: <x>`, `add <x> to my todo` | `task.add` |
| `what's on my plate`, `what do i have (today\|tomorrow)` | `agenda.read` |
| `done: <x>`, `finished <x>`, `got <x>` | `task.complete` / `grocery.check` |
| `weather`, `forecast`, `is it going to rain` | `weather.read` |
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
`check_off_grocery`, `get_weather`, `build_daily_schedule`.

**Write actions get a confirmation step.** Calendar writes and any multi-item batch
render as an embed with ✅/❌ reactions before executing. Reads execute immediately.

This guardrail matters *more* now that the configured model is a small one. A
cheaper model misparses more often, so the confirmation step is doing real work —
do not optimize it away to save a round trip.

---

## 5. Daily jobs

Three scheduled capabilities, all driven by the Task Scheduler (APScheduler,
in-process — no dependency on Windows Task Scheduler, and it survives the move to
a VPS unchanged).

### 5a. Daily brief

Runs at 07:00 local. Posts to `#daily-brief`. Deliberately **deterministic first,
LLM last** — gap-finding is arithmetic, not judgment:

1. Pull today's Google Calendar events (busy blocks).
2. Pull open Notion tasks: overdue, due today, or high-priority with no due date.
3. Pull today's weather (§5b) for the header line.
4. Compute free gaps between busy blocks inside waking hours (default 08:00–22:00),
   discarding gaps shorter than 20 minutes.
5. Greedily fit tasks into gaps by `(priority DESC, due_date ASC)`, using each
   task's `Estimate` (default 30 min when unset).
6. **One** LLM call turns that structure into readable prose and flags conflicts or
   an over-committed day. If the call fails, fall back to a plain template — the
   brief must never fail to post because of an API error.

The same code path serves `/brief` on demand.

### 5b. Weather update

Current conditions and the day's forecast, fetched once each morning and cached in
SQLite for the rest of the day so `/weather` and the brief share one fetch.

Rain or a temperature swing is the one weather fact that changes behavior, so the
brief surfaces those and stays quiet otherwise. **No LLM call** — this is a
formatted API response.

Provider is not yet chosen; see §14.

### 5c. Reminders

Distinct from the brief: the brief is one 07:00 summary, reminders are nudges
through the day. A poll every 15 minutes, entirely deterministic:

- **Appointments** — DM or `#inbox` ping at a configurable lead time (default 30
  minutes) before a calendar event starts.
- **Tasks** — a nudge for anything due today still marked `Not started` as its due
  time approaches, plus a single end-of-day sweep for what slipped.

Every fired reminder is recorded in SQLite so a restart cannot double-notify. No
LLM call — the trigger is a timestamp comparison and the message is a template.

---

## 6. Data model

### Notion — Tasks database

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
| `messages` | Discord message ID -> resolved intent -> resulting Notion/GCal ID. Powers undo and idempotency |
| `briefs` | One row per generated brief; prevents double-posting after a restart |
| `reminders_fired` | One row per delivered reminder; prevents double-notifying after a restart |
| `weather_cache` | One row per day; keeps the morning fetch serving `/weather` all day |
| `conversations` | Rolling short-term context for Layer 2 multi-turn |
| `cache` | Notion database schema cache, so properties aren't re-fetched every call |

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

**Weather — TBD**, pending the provider decision in §14.

**Everything is allowlisted to one Discord user ID.** Jarvis touches a real
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

## 10. Provider inconsistency — resolve before Phase 5

`.env` configures `OPENAI_API_KEY` and `OPENAI_MODEL`, with no Anthropic keys
present — so OpenAI is the configured provider, and the markdown (this plan,
`CLAUDE.md`, `AGENTS.md`) has been made consistent with it.

**One item is still outstanding: `requirements.txt` pins `anthropic` and no OpenAI
client.** That is a dependency change rather than a doc fix, so it is left for a
deliberate commit — swapping it silently would install one SDK and orphan another.

Nothing before Phase 5 touches the LLM, so this is not blocking — but it must be
settled before Layer 2 is built, not during. Whoever does it should also confirm
the §11 cost figures against current OpenAI pricing.

---

## 11. Cost model

Layers 0 and 1 are free. Only Layer 2 and the daily brief cost anything.
Reminders and the weather update are deterministic and cost nothing.

The lever that matters is fast-path hit rate, not model choice — every message
Layer 1 catches is a request that never happens. That holds regardless of provider.

**The dollar figures in this section still need re-baselining** against current
OpenAI pricing for the configured model; the previous numbers were Anthropic's and
have been removed rather than left to mislead. Two structural points survive the
provider change:

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
| **7** | Weather | Provider chosen, credential added, `/weather` works, brief header line lands |
| **8** | Reminders | Appointment lead-time pings and task nudges, idempotent across restart |
| **9** | Hardening | Retries with backoff, rate-limit handling, errors to `#logs`, undo via ❌, spend guard |
| **10** | Portability | Dockerfile + documented VPS deploy, so the laptop stops being load-bearing |

Unit tests (§8) are written alongside each phase, not batched at the end. The
functional suite lands with Phase 9.

Phase 2 is the one to resist rushing past. A bot that reliably adds groceries at
zero API cost is already worth having on your phone.

---

## 13. Explicit non-goals (for now)

- Multi-user support. One allowlisted user, assumed throughout.
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
- Weather provider: which API? This decides a `.env` key, a `SETUP.md` section, and
  whether the free tier covers a once-daily fetch.

**Carried over:**

- Waking-hours window for the scheduler — 08:00–22:00 is a placeholder.
- Should the brief include tomorrow's first commitment as a footer?
- Do groceries need a "shopping trip" concept, or is a flat checked/unchecked list enough?
- Appointment reminder lead time — 30 minutes is a placeholder, and it probably
  wants to vary by whether travel is involved.
