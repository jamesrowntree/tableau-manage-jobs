# Bulk datasource extract-refresh scheduler

A standalone command-line script that reads a list of Tableau Cloud data
sources from a CSV file and creates **one extract-refresh task per row**, each
with its **own schedule** taken from that row. It's the headless, batch
counterpart to the web app's *Add multiple tasks* tab — but every data source
can have a different cadence, and it runs entirely on its own.

> **Note:** This script is **self-contained**. It talks to the Tableau Cloud
> REST API directly with `httpx` and does **not** import the web app's modules,
> use `tableauserverclient`, or require the app to be running. It only shares
> the app's `.env` credentials file.

## What it does

- Reads a CSV list of data sources (`datasource-scheduler/datasources.csv` by default).
- Signs in to Tableau Cloud with your Personal Access Token.
- For each row, builds the inline Tableau Cloud schedule that row describes and
  creates a new extract-refresh task for that data source.
- **Fail-soft:** a bad row (unknown data source, invalid schedule) is reported
  and skipped; the rest still run. A final summary line reports how many
  succeeded and failed, and the process exits non-zero if anything failed.
- **Run now instead:** pass `--now` to skip scheduling and trigger an immediate
  one-off refresh for each datasource in the list (the schedule columns are
  ignored). The script clearly announces this mode before it starts.

Each data source can be identified **by name or by LUID**, and each row carries
its own frequency, so you can schedule (for example) one source hourly, another
daily at 6am, and a third on the first Monday of the month — all in one run.

## Why it's separate from the app

The web app creates tasks one at a time through a browser form and uses
`tableauserverclient` for its reads. This script is the *minimum viable code*
for the single job of bulk-scheduling data sources, pulled out into one file so
it can run anywhere with just `httpx` — no web server, no browser, no app
process. It reuses the app's `.env` so you don't configure credentials twice.

## How scheduling works

Tableau Cloud has **no shared, reusable schedules** (unlike Tableau Server):
every extract-refresh task carries its own private, inline schedule defined at
the moment the task is created. So this script builds one inline schedule per
CSV row and attaches it to that row's new task. Ten rows on the same "daily at
6am" cadence produce ten separate tasks, each with its own copy of that
schedule — exactly how Tableau Cloud itself models it.

## The list file (`datasources.csv`)

A CSV with a header row and one data source per line. Fill in only the columns
a given frequency needs; leave the rest blank. The `week_days` cell uses a
semicolon (`;`) to separate multiple days, since commas are reserved by CSV.

| Column | Meaning | Used by |
| --- | --- | --- |
| `datasource` | Data source **name** or **LUID** | all rows |
| `refresh_type` | `FullRefresh` (default) or `IncrementalExtract` | all rows |
| `frequency` | `Hourly`, `Daily`, `Weekly`, or `Monthly` | all rows |
| `start` | Start time, `HH:MM:SS`, on a 5-minute boundary | Hourly, Daily |
| `end` | End time, `HH:MM:SS` | Hourly; Daily when < 24h |
| `hours` | Interval in hours | Hourly (`1`); Daily (`2`/`4`/`6`/`8`/`12`/`24`) |
| `minutes` | `60` — alternative to `hours=1` | Hourly |
| `week_days` | Extra day filters, `;`-separated, e.g. `Monday;Wednesday` | Hourly, Daily (optional) |
| `week_day` | A single weekday | Weekly; Monthly occurrence pairing |
| `month_day` | `1`–`31` or `LastDay` | Monthly |
| `month_occurrence` | `First`/`Second`/`Third`/`Fourth`/`Last` (paired with `week_day`) | Monthly |

### Frequency rules

- **Hourly** — `hours=1` (or `minutes=60`); `start` and `end` are required.
  Optional `week_days` limit which days it runs.
- **Daily** — `hours` must be one of `2, 4, 6, 8, 12, 24`; `start` is required;
  `end` is required unless `hours=24`. Optional `week_days`.
- **Weekly** — exactly one `week_day`; no `start`/`end`.
- **Monthly** — either a `month_day` (`1`–`31` or `LastDay`), **or** a
  `month_occurrence` together with a `week_day` (e.g. `First` + `Monday`).
- Times must fall on 5-minute boundaries, and `end` − `start` must be a positive
  multiple of 60 minutes.

### Example

```csv
datasource,refresh_type,frequency,start,end,hours,minutes,week_days,week_day,month_day,month_occurrence
Sales Extract,FullRefresh,Daily,06:00:00,,24,,,,,
Marketing Data,IncrementalExtract,Daily,06:00:00,22:00:00,4,,,,,
a1b2c3d4-e5f6-7890-abcd-ef1234567890,FullRefresh,Weekly,,,,,,Monday,,
Ops Metrics,FullRefresh,Hourly,06:00:00,18:00:00,1,,Monday;Wednesday;Friday,,,
Finance Cube,FullRefresh,Monthly,,,,,,,1,
Inventory,FullRefresh,Monthly,,,,,,Monday,,First
```

### Identifying data sources

- If a `datasource` value looks like a LUID (a UUID), it's used directly and
  verified against the site.
- Otherwise it's treated as a **name** and looked up. If **no** data source has
  that name, or if **more than one** does, that row fails with a clear message
  telling you to use the LUID instead — nothing is scheduled by guess.

## Prerequisites

- **Python 3.10+**
- A **Tableau Cloud Personal Access Token (PAT)** whose owner has the
  **Creator** or **Explorer** site role (or Site Administrator) — the role
  needed to create extract-refresh tasks.
- The same connection details the web app uses (pod URL and site content URL).

Only two third-party packages are needed — `httpx` and `python-dotenv` — both
already listed in `requirements.txt`.

## Setup

If you've already set up the web app, you're done — the script reuses the same
`.env`. Otherwise:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and fill in your PAT name/secret, pod URL, and site content URL
```

`.env` is listed in `.gitignore` and must never be committed.

## Usage

```bash
source .venv/bin/activate                                   # if not already active
python datasource-scheduler/schedule_datasources.py         # SCHEDULE: uses datasource-scheduler/datasources.csv
python datasource-scheduler/schedule_datasources.py my.csv  # SCHEDULE: a custom list file
python datasource-scheduler/schedule_datasources.py --now   # RUN NOW: refresh each datasource immediately (no schedule)
```

(You can also skip activation and call `.venv/bin/python datasource-scheduler/schedule_datasources.py`.)

### Run now instead of scheduling

Pass `--now` to skip scheduling and instead trigger an **immediate one-off
extract refresh** for each datasource in the list — like clicking *Refresh now*
in Tableau. In this mode the schedule columns (`frequency`, `start`, …) are
**ignored**; each row only needs its `datasource`. The script prints a clear
`RUN-NOW mode` banner so you always know which action it's taking, and reports
the queued job id per row:

```
RUN-NOW mode: triggering an IMMEDIATE one-off extract refresh for each datasource.
No schedules are created — the schedule columns in the CSV are ignored.

  OK   line 2: Sales Extract -> refresh started now (job 7f1e...c2)

Done: 1 refresh(es) started, 0 failed.
```

**Tip:** start with a one-row CSV to confirm sign-in and see the returned task
id before scheduling the whole batch.

Example output:

```
Signing in to https://10ax.online.tableau.com (site: acme) ...
Signed in OK (REST 3.24). 128 datasource(s) on site.

  OK   line 2: Sales Extract [Daily] -> task 7f1e...c2
  FAIL line 3: Marketing Data -> end minus start must be a positive multiple of 60 minutes
  OK   line 4: a1b2c3d4-... [Weekly] -> task 90ab...11

Done: 2 created, 1 failed.
```

## Notes and limitations

- **Creates only.** It does not de-duplicate against existing tasks, so
  re-running the same CSV creates additional tasks.
- **Two modes.** By default it creates a recurring schedule per row. Pass
  `--now` to instead run a one-off refresh immediately (no schedule created).
- **Data sources only** — no workbooks.
- **Shares the app's PAT.** A Personal Access Token is single-use-at-a-time, so
  running this script while the web app has an active session will invalidate
  that session (it re-signs-in on its next call, and vice-versa). Run the script
  when the app isn't mid-operation.

## Files

```
datasource-scheduler/
├── schedule_datasources.py    This script (self-contained, httpx + stdlib)
├── datasources.csv            The data source list it reads (edit this)
├── README.md                  This document
└── README.html                This document (HTML version)
```

## Built with

- [httpx](https://www.python-httpx.org/) — direct Tableau Cloud REST calls
- [python-dotenv](https://pypi.org/project/python-dotenv/) — loads `.env`
- Python standard library (`csv`, `xml.etree.ElementTree`, …) — everything else
