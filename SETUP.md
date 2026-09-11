# Setup — Phase 0

Everything here is account/credential work outside the repo. Nothing in `Jarvis/`
runs until `.env` is fully populated. Work top to bottom; each section ends with
the `.env` keys it produces.

---

## 1. Discord

1. **Create the app** — <https://discord.com/developers/applications> -> *New Application*, name it Jarvis.
2. **Bot tab** -> *Reset Token* -> copy it. This is `DISCORD_BOT_TOKEN`. It is shown once.
3. **Privileged Gateway Intents** (same tab) — enable **MESSAGE CONTENT INTENT**.
   Without it `message.content` arrives empty and the whole natural-language layer
   silently does nothing. Also enable **SERVER MEMBERS INTENT**.
4. **Invite the bot** — *OAuth2 -> URL Generator*:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: Send Messages, Embed Links, Read Message History, Add Reactions, Use Slash Commands

   Open the generated URL and add it to a server.
5. **Create the server** — a private Discord server, "Jarvis HQ", with channels:
   - `#inbox` — where you talk to Jarvis
   - `#daily-brief` — where the 07:00 schedule lands
   - `#groceries` — bare lines here are treated as grocery items
   - `#logs` — errors and audit trail
6. **Get the IDs** — enable *Settings -> Advanced -> Developer Mode*, then right-click
   each to *Copy ID*: the server, each channel, and **your own user account**.

   If you use Discord from more than one account, copy the ID of each. The first
   goes in `DISCORD_OWNER_USER_ID1`, the second in `DISCORD_OWNER_USER_ID2`. Only
   the first is required — a single-account setup needs no placeholder for the
   second, and a typo in it fails at startup rather than silently locking that
   account out.

Produces: `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_OWNER_USER_ID1`,
`DISCORD_OWNER_USER_ID2` (optional),
`DISCORD_INBOX_CHANNEL_ID`, `DISCORD_BRIEF_CHANNEL_ID`,
`DISCORD_GROCERY_CHANNEL_ID`, `DISCORD_LOG_CHANNEL_ID`

> Register slash commands **guild-scoped**, not globally — guild commands appear
> instantly, global commands take up to an hour to propagate. That's why
> `DISCORD_GUILD_ID` is required rather than optional.

---

## 2. Google Calendar

Using a service account, so there is no OAuth refresh token to maintain.

1. **Create a project** — <https://console.cloud.google.com> -> new project, "jarvis".
2. **Enable the API** — *APIs & Services -> Library* -> "Google Calendar API" -> *Enable*.
3. **Create the service account** — *IAM & Admin -> Service Accounts -> Create*.
   Name it `jarvis-calendar`. No roles needed — its access comes from the calendar
   share in step 5, not from IAM.
4. **Create a key** — open the service account -> *Keys -> Add Key -> Create new key
   -> JSON*. It downloads once. Save it to `secrets/gcal-service-account.json` in
   this repo (already gitignored).
5. **Share your calendar with it** — copy the service account's email
   (`jarvis-calendar@<project>.iam.gserviceaccount.com`). In Google Calendar:
   *Settings -> your calendar -> Share with specific people -> Add people* -> paste
   the address -> permission **"Make changes to events"**.

   **This step is the actual grant.** Skipping it produces a working credential
   that can see nothing.
6. **Find your calendar ID** — same settings page, *Integrate calendar -> Calendar ID*.
   For your primary calendar it's your Gmail address.

Produces: `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_CALENDAR_ID`

---

## 3. Notion

### 3a. Create the integration

1. <https://www.notion.so/my-integrations> -> *New integration*, internal, name it Jarvis.
2. Capabilities: Read content, Update content, Insert content.
3. Copy the **Internal Integration Secret** (starts `ntn_`).

### 3b. Create the Tasks database

New page -> `/database - full page` -> name it **Tasks**. Add these properties
(`Name` already exists as the title):

| Property | Type | Options |
|---|---|---|
| `Status` | Status | `Not started`, `In progress`, `Done` |
| `Due` | Date | |
| `Priority` | Select | `High`, `Medium`, `Low` |
| `Estimate` | Number | Minutes |
| `Project` | Select | (add as you go) |
| `Notes` | Text | |
| `Source` | Select | `discord`, `manual` |

### 3c. Create the Groceries database

New page -> `/database - full page` -> **Groceries**. Rename the title property to
`Item`, then add:

| Property | Type | Options |
|---|---|---|
| `Qty` | Text | |
| `Category` | Select | `Produce`, `Dairy`, `Meat`, `Pantry`, `Frozen`, `Household`, `Other` |
| `Got it` | Checkbox | |
| `Added` | Created time | |

### 3d. Connect the integration to each database

On **each** database page: `•••` (top right) -> *Connections* -> *Connect to* -> Jarvis.

**Do this for both databases individually.** Connecting the parent page does not
cascade. A database the integration isn't connected to returns 404, not 403 —
which reads like a wrong ID and sends you debugging the wrong thing.

### 3e. Get the database IDs

Open each database as a full page and read the URL:

```
https://www.notion.so/<workspace>/<32-char-database-id>?v=<view-id>
                                  ^^^^^^^^^^^^^^^^^^^^ this part
```

Produces: `NOTION_TOKEN`, `NOTION_TASKS_DB_ID`, `NOTION_GROCERIES_DB_ID`

---

## 4. OpenAI

1. <https://platform.openai.com> -> *API keys* -> create a key.
2. Set a monthly spend limit in *Billing -> Limits* while you're there. Belt and
   braces with the in-app spend guard.

Produces: `OPENAI_API_KEY`, `OPENAI_MODEL`

> `requirements.txt` still pins the `anthropic` client and no OpenAI client. See
> `plan/plan.md` §10 — that has to be reconciled before Phase 5.

---

## 5. Weather — deferred to Phase 7

No provider chosen yet, so there is nothing to set up here. Whoever picks one
(`plan/plan.md` §14) should add the section: signup, the key name, and whether the
free tier covers one forecast fetch per day.

---

## 6. Local environment

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

`.env` already exists and is populated — it is gitignored and never committed.
There is no longer a `.env.example` in the repo; if you set this up on a second
machine, copy `.env` across by hand rather than through git.

---

## Verification checklist

Work through these before starting Phase 1 — each one fails in a confusing way later.

- [ ] Bot appears **online** in Jarvis HQ
- [ ] MESSAGE CONTENT INTENT is toggled on in the Developer Portal
- [ ] All five Discord IDs copied (guild, your user, three channels)
- [ ] The service account email appears under your calendar's "Share with specific people"
- [ ] `secrets/gcal-service-account.json` exists and is **not** tracked by git (`git status` shows nothing)
- [ ] Both Notion databases show Jarvis under `•••` -> Connections
- [ ] Both Notion database IDs are 32 characters, no dashes
- [ ] `.env` has no placeholder values left
- [ ] `.env` and `secrets/` do not appear in `git status`
