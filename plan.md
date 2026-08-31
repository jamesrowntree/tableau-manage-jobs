# Tableau Cloud Job Manager — a small local web app

## Context

You want to **read the tasks in your Tableau Cloud job queue and add new ones** — without
clicking through the Tableau Cloud web UI each time. The project directory is empty, so we
build from scratch.

The end product is a **small local web app**: a FastAPI backend (Python) that holds your
credentials and talks to Tableau Cloud via the official `tableauserverclient` (TSC) library,
and a single-page frontend (plain HTML + Alpine.js + Pico.css, no build step) that talks only
to your backend.

**Why a backend is required:** the browser cannot call the Tableau Cloud REST API directly
(no CORS for cross-origin browser calls), and a Personal Access Token must never live in
front-end JavaScript. The backend holds the PAT, signs in, and exposes a small JSON API to the
page. Because the browser only talks to same-origin FastAPI, CORS is a non-issue.

**Decisions already made with you:**
- Cover **multiple task types**: **subscriptions** and **extract refreshes** in v1 (flow runs
  left as an easy extension).
- **Read scope = both**: live background **jobs** (the actual queue) *and* the **scheduled
  tasks** that feed it.
- **Scope = read + add** first, structured so run-now / delete slot in later.
- **Frontend = HTML + Alpine.js + Pico.css** — reactive enough for the dynamic schedule
  builder, zero build step, sharp default styling.
- Runs **locally**, single user, PAT in a gitignored `.env`.

## Architecture

```
Browser (index.html + Alpine.js + Pico.css)
   │  GET  /api/jobs, /api/subscriptions, /api/extract-tasks
   │  GET  /api/workbooks, /api/datasources, /api/views, /api/users   (form pickers)
   │  POST /api/subscriptions, /api/extract-tasks                     (add)
   ▼
FastAPI backend  ──uses──▶ tableauserverclient ──▶ Tableau Cloud REST API
   • PAT + site info from .env; signs in, caches the session, re-signs-in on 401
   • serves the static page AND the JSON endpoints (same origin)
```

## Prerequisites (Tableau side — needed to run/test, not to write code)

- A **Personal Access Token**: Tableau Cloud → account menu → **My Account Settings** →
  **Personal Access Tokens** → create one; copy the **token name** and **secret** (secret shown
  once).
- Your **pod / server URL**, e.g. `https://10ax.online.tableau.com` (the host in your browser
  when signed into Tableau Cloud).
- Your **site content URL** — the `…/site/<contentUrl>/…` segment from the address bar (this is
  the value TSC calls `site_id`, distinct from the site display name).
- The PAT owner needs a **Creator or Explorer** role (or admin) to create subscriptions/tasks.

## Project layout

```
add-tasks-to-schedule/
  app.py                 FastAPI app: JSON routes + serves static/
  tableau.py             TSC session manager + list/create helpers (+ REST fallback)
  schedules.py           Build/validate the Cloud schedule model from form input
  requirements.txt       fastapi, uvicorn[standard], tableauserverclient, python-dotenv
  .env.example           documents required vars (committed)
  .env                   real secrets (gitignored)
  .gitignore
  static/
    index.html           single page: Jobs / Scheduled tasks / Add task
    app.js               Alpine component (fetch + reactive form) — or inline in index.html
  scripts/
    smoke.py             CLI sanity check: sign in + list jobs (validate creds before the UI)
  README.md              setup + run instructions
```

## Backend (`app.py` + `tableau.py`)

**Config & auth (`tableau.py`)** — grounded in TSC docs:
```python
auth = TSC.PersonalAccessTokenAuth(TOKEN_NAME, TOKEN_SECRET, site_id=SITE_CONTENT_URL)
server = TSC.Server(SERVER_URL, use_server_version=True)
server.auth.sign_in(auth)
```
- Load the four vars from `.env` via `python-dotenv`.
- Provide a small `TableauSession` helper: signs in lazily, **caches** the signed-in `server`,
  and **re-signs-in on a 401**. (A PAT holds one active session at a time; for a single-user
  local tool a cached session with relogin is simplest and avoids token-stomping.)
- Expose it to routes as a FastAPI dependency.

**JSON endpoints:**

| Method | Path | TSC / REST used | Purpose |
|---|---|---|---|
| GET | `/api/jobs` | `server.jobs.get()` (+ `TSC.Pager`, optional status filter) | live queue |
| GET | `/api/subscriptions` | `server.subscriptions.get()` | scheduled subs |
| GET | `/api/extract-tasks` | `server.tasks.get()` (extract refreshes) | scheduled refreshes |
| GET | `/api/workbooks` / `/datasources` / `/views` / `/users` | `server.workbooks` / `datasources` / `views` / `users` | populate Add-form pickers (pick by name, send LUID) |
| POST | `/api/subscriptions` | create (see risk note) | add subscription |
| POST | `/api/extract-tasks` | create (see risk note) | add extract refresh |

Return trimmed JSON (id, name/subject, type, status, target, schedule summary) — the page
never sees raw XML. Later extensions: `POST /api/extract-tasks/{id}/run` (run-now) and
`DELETE` routes.

## Tableau Cloud API specifics (grounded from the current REST docs)

These are Cloud-specific and differ from Tableau Server (Server references a pre-existing
`schedule-id`; **Cloud defines the schedule inline** in the create request).

**Subscriptions** — `GET/POST /api/<ver>/sites/<site-luid>/subscriptions`. Required to create:
`subject`, `content` (`id` + `type`=`Workbook`|`View`), `user` `id`, and an inline `<schedule>`.

**Extract refresh tasks** — list `GET .../tasks/extractRefreshes`; **create** (Cloud only,
API ≥ 3.20 / June 2023) `POST .../tasks/extractRefreshes` with `extractRefresh type`
(`FullRefresh`|`IncrementalExtract`) + a `workbook id` **or** `datasource id`, plus an inline
`<schedule>`. Run-now: `POST .../tasks/extractRefreshes/<task-id>/runNow` (empty body →
returns a `job`).

**The inline schedule model** (drives the Add-form's dynamic fields):
- `frequency`: `Hourly` | `Daily` | `Weekly` | `Monthly`
- `frequencyDetails start`/`end` (`HH:MM:SS`); `end` required for Hourly and sub-24h Daily.
  Times in **5-minute increments**; start↔end difference in **60-minute increments**.
- `intervals` vary by frequency:
  - **Hourly** → `hours="1"` or `minutes="60"` (+ optional `weekDay`s)
  - **Daily** → one `hours` ∈ {2,4,6,8,12,24} (+ optional `weekDay`s)
  - **Weekly** → a single `weekDay`
  - **Monthly** → `monthDay` (1–31 or `LastDay`), or occurrence (e.g. `Third`) + `weekDay`

`schedules.py` builds and **validates** this from form input (enforce the {2,4,6,8,12,24},
5-min, and 60-min-diff rules before calling Tableau).

## Frontend (`static/index.html` + Alpine.js + Pico.css)

- Load **Pico.css** and **Alpine.js** from CDN (`<script>` tags) — no build, no npm.
- One page, three sections:
  1. **Jobs** — table of live background jobs; filter by status; **poll `/api/jobs` every
     ~5–10s** for a live view (Alpine interval).
  2. **Scheduled tasks** — subscriptions + extract-refresh tables with a schedule summary.
  3. **Add task** — task-type selector (Subscription | Extract refresh). A **reactive schedule
     builder** uses Alpine `x-show`/`x-if` bound to the frequency dropdown to show exactly the
     right interval fields (this dynamic form is the reason we chose Alpine over plain JS).
     Content/user chosen from **name dropdowns** (populated by the picker endpoints; LUIDs sent
     under the hood). On submit → `fetch` POST → on success refresh the relevant table and show
     a confirmation; on error show the message.

## Implementation risk / decision to confirm during build

TSC's built-in support for **creating** Cloud subscriptions / extract-refresh tasks with an
*inline* schedule varies by TSC version (reads are fully supported). Plan:
1. Pin a current `tableauserverclient` and try its native create (`server.subscriptions.create`,
   `server.tasks.create`).
2. If the installed version doesn't cover Cloud inline-schedule creation, fall back to a thin
   REST call that **reuses TSC's already-signed-in session** (`server.auth_token`,
   `server.baseurl`) to `POST` the exact XML bodies documented above. This keeps one auth path
   and guarantees the create works regardless of TSC coverage.

## Verification (end-to-end)

1. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
2. Copy `.env.example` → `.env`; fill in PAT name/secret, pod URL, site content URL.
3. **Validate creds first:** `python scripts/smoke.py` → should sign in and print current jobs.
   (Catches bad PAT / wrong site content URL before touching the UI.)
4. `uvicorn app:app --reload` → open `http://localhost:8000`.
5. **Read:** confirm the Jobs table populates and auto-refreshes; subscriptions and
   extract-refresh tasks list correctly.
6. **Add:** create a **Daily** extract refresh on a known workbook/datasource → confirm it
   appears in the app's list *and* in Tableau Cloud (the content's **Extract Refreshes** tab).
   Create a subscription to a view for your user → confirm likewise.
7. (Optional) trigger **run-now** on a task and watch a new job appear in the Jobs table.

## Out of scope for v1 (easy to add later)

- Flow-run tasks (`server.flow_runs`), run-now UI, edit/delete of tasks, multi-user auth,
  deployment beyond localhost.
