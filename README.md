# Jarvis

An agentic secretary curated to my needs.

Talk to it on Discord. It manages a Google Calendar, keeps a Notion to-do list and
grocery list current, and posts a rough schedule for the day each morning.

## Status

Phase 0 — infrastructure and planning. No application code yet.

## Docs

- **[plan/plan.md](plan/plan.md)** — architecture, data model, cost model, build phases
- **[SETUP.md](SETUP.md)** — accounts, credentials, and the Phase 0 checklist
- **[AGENTS.md](AGENTS.md)** — conventions for anyone (or anything) writing code here

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then work through [SETUP.md](SETUP.md) to fill in `.env`.
