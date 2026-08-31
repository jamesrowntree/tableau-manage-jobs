# Tableau Cloud Job Manager

A small local web app for reading the live job queue and scheduled tasks on a
**Tableau Cloud** site, and creating new subscriptions and extract-refresh
tasks — without clicking through the Tableau Cloud UI.

> **Note:** This is an independent tool built against the public Tableau
> REST API. It is not an official Tableau or Salesforce product.

## What it does

- **Jobs** — lists live background jobs (running, pending, succeeded, failed)
  and auto-refreshes every few seconds.
- **Scheduled tasks** — lists existing subscriptions and extract-refresh
  tasks, with their target and schedule.
- **Add task** — creates a new subscription or extract-refresh task, with a
  form that builds the correct Tableau Cloud schedule (hourly / daily /
  weekly / monthly) for you.

## Architecture

```
Browser (index.html + Alpine.js + Pico.css)
   │  GET  /api/jobs, /api/subscriptions, /api/extract-tasks
   │  GET  /api/workbooks, /api/datasources, /api/views, /api/users
   │  POST /api/subscriptions, /api/extract-tasks
   ▼
FastAPI backend  ──uses──▶  tableauserverclient  ──▶  Tableau Cloud REST API
```

The backend holds your Tableau credentials and talks to Tableau Cloud on
your behalf. The browser never talks to Tableau directly and never sees
your credentials — it only talks to this local backend.

Reads (jobs, subscriptions, extract-refresh tasks, workbooks, datasources,
views, users) go through the official `tableauserverclient` library. Creates
(new subscriptions and extract-refresh tasks) build the exact XML body
documented by the [Tableau REST API reference](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref.htm)
for Tableau Cloud's inline schedules, since Cloud has no server-wide
schedules to reference (unlike Tableau Server).

## Prerequisites

- **Python 3.10+**
- A **Tableau Cloud Personal Access Token (PAT)**:
  Tableau Cloud → account menu (top right) → **My Account Settings** →
  **Personal Access Tokens** → create one. Copy the **token name** and
  **secret** — the secret is shown once.
- Your **pod URL** — the host you see in the browser when signed into
  Tableau Cloud, e.g. `https://10ax.online.tableau.com`.
- Your **site content URL** — the segment after `/site/` in the address bar
  when viewing your site (not the display name shown in the UI).
- The PAT owner needs the **Creator** or **Explorer** site role (or Site
  Administrator) to create subscriptions and extract-refresh tasks.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and fill in your PAT name/secret, pod URL, and site content URL
```

`.env` is listed in `.gitignore` and must never be committed.

## Verify your credentials

Before starting the web app, confirm your `.env` values are correct:

```bash
python scripts/smoke.py
```

This signs in and prints the current jobs on your site. If it fails, check
your PAT, pod URL, and site content URL (this is the most common mistake —
it must be the content URL, not the site's display name).

## Run

```bash
uvicorn app:app --reload
```

Open **http://localhost:8000**.

## Using the app

- **Jobs** tab — the live queue. Refreshes automatically every 7 seconds, or
  click *Refresh now*.
- **Scheduled tasks** tab — existing subscriptions and extract-refresh tasks
  and their frequency.
- **Add task** tab:
  1. Choose **Subscription** or **Extract refresh**.
  2. Pick the target by name (workbook, view, or data source) and, for
     subscriptions, the user to send to.
  3. Build the schedule: pick a frequency and the form shows exactly the
     fields Tableau Cloud needs for that frequency (e.g. Weekly needs a
     single day; Monthly needs either a day-of-month or an occurrence like
     "third Thursday").
  4. Click **Create task**. On success it appears in **Scheduled tasks** —
     and in Tableau Cloud itself.

## Project structure

```
tableau-manage-jobs/
├── app.py               FastAPI app: JSON routes + serves the frontend
├── tableau.py            Tableau Cloud session, list/create helpers
├── schedules.py          Builds and validates the Tableau schedule XML
├── requirements.txt
├── .env.example          Documents the required environment variables
├── static/
│   └── index.html         The whole frontend (Alpine.js + Pico.css via CDN)
├── scripts/
│   └── smoke.py           Sign-in sanity check
├── README.md
└── README.html
```

## Security notes

- Your PAT lives only in `.env` on your machine and is used only by the
  backend process — it is never sent to or visible in the browser.
- This app has **no login of its own** and is meant to run on
  `localhost` for a single user. Do not deploy it to a shared or public
  server without adding authentication in front of it.
- Never commit `.env`. `.env.example` documents the variable names with
  no real values, and is safe to commit.

## Known limitations

- Covers subscriptions and extract-refresh tasks. Flow-run tasks are not
  yet included.
- No run-now, edit, or delete actions from the UI yet (the read/create paths
  are built so these are straightforward to add later).
- Single-user, local use only — there is no multi-user auth model.

## Built with

- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/)
- [tableauserverclient](https://tableau.github.io/server-client-python/) (the official Tableau REST API client for Python)
- [Alpine.js](https://alpinejs.dev/) and [Pico.css](https://picocss.com/) (via CDN, no build step)
