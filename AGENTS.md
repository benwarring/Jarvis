# Jarvis — Agent Instructions

An AI secretary for a single user. Discord in, Google Calendar and Notion out,
an LLM as the fallback parser.

**Read `plan/plan.md` before making structural changes.** It is the specification the
reviewer personas in `.claude/personas/` audit against. If a change makes the code
diverge from the plan, update `plan/plan.md` in the same commit.

## Project layout

All application code lives under `Jarvis/`. See `plan/plan.md` §3 for the module map and
what belongs in each package. Tests mirror that structure under `tests/`.

## Conventions

- **Python 3.11+.** Type hints on anything crossing a module boundary.
- **One implementation per capability.** Slash commands, the regex fast-path, and
  the LLM tool loop all call the same functions in `integrations/`. If you find
  yourself writing a second "create an event," stop.
- **Config comes from `Jarvis/config.py`**, which reads `.env` once and validates at
  startup. Never call `os.environ` from feature code, and never hard-code an ID,
  channel name, model string, or timezone.
- **Store UTC, render local.** Timezone conversion happens in `utils/dates.py` at
  the edges, nowhere else.
- **Every external call is fallible.** Google, Notion, Discord, and the LLM provider all
  get try/except with a logged failure and a user-visible message. A failed API
  call must never take the bot process down.
- **The daily brief must always post.** If the LLM call fails, fall back to the
  plain template.

## Cost discipline

Layer 2 (the LLM) is the only thing that costs money. Before adding an LLM call, ask
whether the regex fast-path or a deterministic computation can answer it. The
gap-finding in the daily brief is arithmetic and stays arithmetic.

The model ID is `OPENAI_MODEL` in `.env` — never a literal in code.

## Security

- `.env` and `secrets/` are gitignored. Never read a credential into a log line, a
  Discord message, an error trace, or a test fixture.
- Every command path checks the caller against the owner allowlist (`DISCORD_OWNER_USER_ID1`,
  plus the optional `DISCORD_OWNER_USER_ID2`) before doing
  anything. This is not optional — Jarvis writes to a real calendar.
- Calendar writes and multi-item batches require ✅ confirmation before executing.
