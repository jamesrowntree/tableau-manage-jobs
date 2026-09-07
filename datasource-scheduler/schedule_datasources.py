"""Standalone script to schedule multiple Tableau Cloud datasource extract refreshes at once.

Reads a list of datasources (each with its own schedule) from a CSV file and
creates one extract-refresh task per row on Tableau Cloud. It is fully
self-contained: it talks to the Tableau REST API directly with `httpx`. Credentials
come from the same `.env` the app uses; targets come from a separate CSV.

    python datasource-scheduler/schedule_datasources.py [--now] [path/to/list.csv]

Defaults to datasource-scheduler/datasources.csv when no path is given. See that file for the
column layout. Each datasource may be identified by name or by LUID.

Modes:
  - Default: create one recurring extract-refresh *schedule* per row, each using
    that row's own frequency. Re-running the same CSV creates additional tasks
    (it does not de-duplicate against existing ones).
  - --now: skip scheduling entirely and trigger an *immediate* one-off extract
    refresh for each row's datasource instead. The schedule columns are ignored
    in this mode, and the script prints that it is running refreshes now rather
    than creating schedules.

Notes:
  - Datasources only (no workbooks).
"""

from __future__ import annotations

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# High-level flow (see main() at the bottom for the orchestration)
#
#   1. Load credentials from .env               -> load_config()
#   2. Discover the live REST API version       -> discover_version()
#   3. Sign in with the PAT                      -> sign_in()
#   4. Fetch every datasource on the site once  -> list_datasources()
#   5. For each CSV row, in a fail-soft loop:
#        a. resolve the name/LUID to a real id   -> resolve()
#        b. turn the row's cells into a payload   -> row_to_payload()
#        c. build the inline <schedule> XML       -> build_schedule_xml()
#        d. POST the new extract-refresh task      -> create_extract_task()
#      (With --now, steps b-d are replaced by a single immediate-refresh call,
#       run_extract_now(), which runs the extract once instead of scheduling it.)
#   6. Print an "N done, M failed" summary.
#
# The file is organised in three sections below: (a) the schedule-XML builder,
# (b) the Tableau REST calls, and (c) the CSV row handling + main().
# ---------------------------------------------------------------------------

# --- Module constants ------------------------------------------------------
# Tableau REST responses use this default XML namespace on every element, so
# every ElementTree lookup below is written as "t:<tag>" against this map.
NS = {"t": "http://tableau.com/api"}
# Any recent version works to reach the unauthenticated serverinfo endpoint,
# which then tells us the real REST version to use for the rest of the run.
BOOTSTRAP_VERSION = "3.4"
# A Tableau LUID is a UUID. This pattern is how we tell "this CSV cell is
# already an id" apart from "this cell is a datasource name to look up".
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Default list file: datasources.csv sitting next to this script.
DEFAULT_CSV = Path(__file__).resolve().parent / "datasources.csv"
# The .env keys that must be present before we try to sign in.
REQUIRED_ENV_VARS = ("TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET", "TABLEAU_SERVER_URL")


def _esc(value: str) -> str:
    """Escape a string for safe use inside an XML attribute.

    Every value we interpolate into a `<... attr="...">` body (PAT name/secret,
    site contentUrl, target id) goes through this so a stray & or " can't break
    the request or inject markup.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Section (a): inline <schedule> XML builder (ported from the app's schedules.py)
#
# Tableau Cloud has no server-wide schedules; the frequency is defined inline on
# the create request. Valid shapes per frequency, per the REST API reference:
#   Hourly  - hours="1" or minutes="60"; optional weekDay intervals; start/end.
#   Daily   - hours in {2,4,6,8,12,24}; optional weekDay intervals; end unless 24.
#   Weekly  - exactly one weekDay; no start/end.
#   Monthly - monthDay (1-31 or "LastDay"), or occurrence + weekDay.
# start/end are "HH:MM:SS" on 5-minute boundaries; end-start a multiple of 60m.
# ---------------------------------------------------------------------------

# Allowed values, used to reject bad input early with a clear message.
VALID_FREQUENCIES = {"Hourly", "Daily", "Weekly", "Monthly"}
VALID_DAILY_HOURS = {2, 4, 6, 8, 12, 24}
VALID_WEEKDAYS = {
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
}
VALID_MONTH_OCCURRENCES = {"First", "Second", "Third", "Fourth", "Last"}


class ScheduleError(ValueError):
    """Raised when a row describes a schedule Tableau Cloud would reject.

    Subclasses ValueError so the per-row loop in main() catches schedule
    problems and datasource-resolution problems with one `except ValueError`.
    """


def _parse_time(value: str, field: str) -> datetime:
    """Parse an "HH:MM:SS" string, raising ScheduleError with the field name."""
    try:
        return datetime.strptime(value, "%H:%M:%S")
    except (TypeError, ValueError):
        raise ScheduleError(f'{field} must be "HH:MM:SS", got {value!r}')


def _check_five_minute_boundary(t: datetime, field: str) -> None:
    """Tableau only allows schedule times that land on a 5-minute mark."""
    if t.minute % 5 != 0 or t.second != 0:
        raise ScheduleError(f"{field} must be on a 5-minute boundary")


def build_schedule_xml(payload: dict) -> str:
    """Validate `payload` and return the `<schedule>...</schedule>` XML string.

    The output has two variable parts that each branch below fills in:
      - `attrs`         : the start/end attributes on <frequencyDetails> (only
                          some frequencies carry these).
      - `intervals_xml` : one or more <interval .../> elements.
    """
    frequency = payload.get("frequency")
    if frequency not in VALID_FREQUENCIES:
        raise ScheduleError(f"frequency must be one of {sorted(VALID_FREQUENCIES)}")

    intervals_xml = ""
    attrs = ""

    if frequency == "Hourly":
        # Hourly always needs a start and end window plus an hours=1/minutes=60
        # interval; extra weekDays are optional and handled by _hourly_intervals.
        start, end = _require_start_end(payload, require_end=True)
        attrs = f'start="{start}" end="{end}"'
        intervals_xml = _hourly_intervals(payload)
    elif frequency == "Daily":
        hours = payload.get("hours")
        if hours not in VALID_DAILY_HOURS:
            raise ScheduleError(f"Daily hours must be one of {sorted(VALID_DAILY_HOURS)}")
        # A 24h daily schedule runs once and needs no end; anything shorter does.
        if hours == 24:
            start, _ = _require_start_end(payload, require_end=False)
            attrs = f'start="{start}"'
        else:
            start, end = _require_start_end(payload, require_end=True)
            attrs = f'start="{start}" end="{end}"'
        intervals_xml = f'<interval hours="{hours}"/>' + _weekday_intervals(payload.get("week_days"))
    elif frequency == "Weekly":
        # Weekly is the simplest: exactly one weekday, no time window.
        week_day = payload.get("week_day")
        if week_day not in VALID_WEEKDAYS:
            raise ScheduleError(f"Weekly week_day must be one of {sorted(VALID_WEEKDAYS)}")
        intervals_xml = f'<interval weekDay="{week_day}"/>'
    elif frequency == "Monthly":
        # Monthly has two mutually exclusive shapes; _monthly_interval picks one.
        intervals_xml = _monthly_interval(payload)

    # Wrap the interval(s) in <frequencyDetails>, including the start/end attrs
    # only when this frequency produced any (Weekly/Monthly produce none).
    frequency_details = (
        f"<frequencyDetails {attrs}><intervals>{intervals_xml}</intervals></frequencyDetails>"
        if attrs
        else f"<frequencyDetails><intervals>{intervals_xml}</intervals></frequencyDetails>"
    )
    return f'<schedule frequency="{frequency}">{frequency_details}</schedule>'


def _require_start_end(payload: dict, *, require_end: bool) -> tuple[str, str | None]:
    """Validate the start (and optionally end) time for time-windowed frequencies.

    Returns the raw (start, end) strings so the caller can drop them straight
    into the XML. `end` is None when `require_end` is False (e.g. Daily/24h).
    """
    start = payload.get("start")
    end = payload.get("end")
    if not start:
        raise ScheduleError("start is required for this frequency")
    start_t = _parse_time(start, "start")
    _check_five_minute_boundary(start_t, "start")

    if require_end:
        if not end:
            raise ScheduleError("end is required for this frequency")
        end_t = _parse_time(end, "end")
        _check_five_minute_boundary(end_t, "end")
        # Tableau requires the window to be a whole number of hours, end > start.
        diff_minutes = (end_t - start_t).total_seconds() / 60
        if diff_minutes <= 0 or diff_minutes % 60 != 0:
            raise ScheduleError("end minus start must be a positive multiple of 60 minutes")
    return start, end


def _hourly_intervals(payload: dict) -> str:
    """Build the interval element(s) for an Hourly schedule.

    Hourly is expressed as either hours="1" or minutes="60" (both mean "every
    hour"), optionally followed by weekDay intervals limiting which days it runs.
    """
    hours = payload.get("hours")
    minutes = payload.get("minutes")
    if hours == 1:
        base = '<interval hours="1"/>'
    elif minutes == 60:
        base = '<interval minutes="60"/>'
    else:
        raise ScheduleError("Hourly requires hours=1 or minutes=60")
    return base + _weekday_intervals(payload.get("week_days"))


def _weekday_intervals(week_days: list[str] | None) -> str:
    """Turn an optional list of weekday names into <interval weekDay="..."/> XML."""
    if not week_days:
        return ""
    for day in week_days:
        if day not in VALID_WEEKDAYS:
            raise ScheduleError(f"Invalid weekDay: {day!r}")
    return "".join(f'<interval weekDay="{d}"/>' for d in week_days)


def _monthly_interval(payload: dict) -> str:
    """Build the single <interval/> for a Monthly schedule.

    Two accepted shapes, checked in order:
      - a fixed day of month: monthDay="15" or monthDay="LastDay"; or
      - an occurrence pairing: monthDay="Third" + weekDay="Thursday".
    """
    month_day = payload.get("month_day")
    occurrence = payload.get("month_occurrence")
    week_day = payload.get("week_day")

    # Shape 1: a specific calendar day (1-31) or the special "LastDay".
    if month_day:
        if month_day != "LastDay":
            try:
                day_num = int(month_day)
            except ValueError:
                raise ScheduleError('month_day must be 1-31 or "LastDay"')
            if not (1 <= day_num <= 31):
                raise ScheduleError('month_day must be 1-31 or "LastDay"')
        return f'<interval monthDay="{month_day}"/>'

    # Shape 2: "the <occurrence> <weekday> of the month", e.g. First Monday.
    if occurrence and week_day:
        if occurrence not in VALID_MONTH_OCCURRENCES:
            raise ScheduleError(f"month_occurrence must be one of {sorted(VALID_MONTH_OCCURRENCES)}")
        if week_day not in VALID_WEEKDAYS:
            raise ScheduleError(f"week_day must be one of {sorted(VALID_WEEKDAYS)}")
        return f'<interval monthDay="{occurrence}" weekDay="{week_day}"/>'

    raise ScheduleError(
        "Monthly requires either month_day, or month_occurrence together with week_day"
    )


# ---------------------------------------------------------------------------
# Section (b): Tableau REST calls (direct httpx, no tableauserverclient)
#
# Each call sends/receives Tableau's XML format. Authenticated calls carry the
# X-Tableau-Auth token from sign_in(); responses are parsed with ElementTree
# using the NS namespace map defined at the top.
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Read connection settings from the environment (populated from .env).

    Fails fast with an actionable message if any required variable is missing,
    rather than surfacing a confusing auth error later.
    """
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in your values."
        )
    return {
        "pat_name": os.environ["TABLEAU_PAT_NAME"],
        "pat_secret": os.environ["TABLEAU_PAT_SECRET"],
        # Trailing slash removed so we can safely build "{server_url}/api/..." URLs.
        "server_url": os.environ["TABLEAU_SERVER_URL"].rstrip("/"),
        # Empty string means the Default site.
        "site_content_url": os.environ.get("TABLEAU_SITE_CONTENT_URL", ""),
    }


def discover_version(server_url: str) -> str:
    """Ask the (unauthenticated) serverinfo endpoint for the live REST version.

    Tableau Cloud is continuously updated, so instead of hardcoding a version we
    hit serverinfo with a safe baseline and use whatever version it reports for
    every subsequent call.
    """
    resp = httpx.get(
        f"{server_url}/api/{BOOTSTRAP_VERSION}/serverinfo",
        headers={"Accept": "application/xml"},
        timeout=30,
    )
    resp.raise_for_status()
    node = ET.fromstring(resp.content).find(".//t:restApiVersion", NS)
    if node is None or not node.text:
        raise RuntimeError("Could not read restApiVersion from serverinfo response")
    return node.text.strip()


def sign_in(server_url: str, version: str, cfg: dict) -> tuple[str, str]:
    """Sign in with the PAT. Returns (auth_token, site_id).

    The token goes into the X-Tableau-Auth header on every later call, and the
    site_id scopes the datasource/task URLs to the right Tableau Cloud site.
    """
    # Build the signin request body; every interpolated value is XML-escaped.
    body = (
        "<tsRequest>"
        f'<credentials personalAccessTokenName="{_esc(cfg["pat_name"])}" '
        f'personalAccessTokenSecret="{_esc(cfg["pat_secret"])}">'
        f'<site contentUrl="{_esc(cfg["site_content_url"])}"/>'
        "</credentials>"
        "</tsRequest>"
    )
    resp = httpx.post(
        f"{server_url}/api/{version}/auth/signin",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Accept": "application/xml"},
        timeout=30,
    )
    resp.raise_for_status()
    # Pull the token (an attribute on <credentials>) and the site id (on <site>).
    creds = ET.fromstring(resp.content).find("t:credentials", NS)
    if creds is None:
        raise RuntimeError("Sign-in response missing <credentials>")
    site = creds.find("t:site", NS)
    return creds.get("token", ""), (site.get("id", "") if site is not None else "")


def list_datasources(server_url: str, version: str, site_id: str, token: str) -> list[dict]:
    """All datasources on the site, following pagination. Each: {id, name}.

    Fetched once up front so name->LUID resolution for every CSV row is a cheap
    in-memory lookup rather than a REST call per row.
    """
    headers = {"X-Tableau-Auth": token, "Accept": "application/xml"}
    out: list[dict] = []
    page = 1
    while True:
        url = (
            f"{server_url}/api/{version}/sites/{site_id}/datasources"
            f"?pageSize=1000&pageNumber={page}"
        )
        resp = httpx.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        # Collect this page's datasources.
        for ds in root.findall(".//t:datasources/t:datasource", NS):
            out.append({"id": ds.get("id"), "name": ds.get("name")})
        # Stop once we've retrieved every item the <pagination> block reports.
        pagination = root.find("t:pagination", NS)
        if pagination is None:
            break
        total = int(pagination.get("totalAvailable", "0"))
        page_size = int(pagination.get("pageSize", "1000"))
        if page * page_size >= total:
            break
        page += 1
    return out


def create_extract_task(
    server_url: str,
    version: str,
    site_id: str,
    token: str,
    *,
    refresh_type: str,
    target_id: str,
    schedule_xml: str,
) -> str:
    """Create one datasource extract-refresh task. Returns the new task id.

    `schedule_xml` is the already-built <schedule> element from
    build_schedule_xml(); this just wraps it with the target datasource and POSTs.
    """
    body = (
        "<tsRequest>"
        f'<extractRefresh type="{_esc(refresh_type)}">'
        f'<datasource id="{_esc(target_id)}"/>'
        "</extractRefresh>"
        f"{schedule_xml}"
        "</tsRequest>"
    )
    resp = httpx.post(
        f"{server_url}/api/{version}/sites/{site_id}/tasks/extractRefreshes",
        content=body.encode("utf-8"),
        headers={
            "X-Tableau-Auth": token,
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        },
        timeout=30,
    )
    resp.raise_for_status()
    # The new task's id comes back as an attribute on <extractRefresh>.
    node = ET.fromstring(resp.content).find(".//t:extractRefresh", NS)
    return node.get("id", "") if node is not None else ""


def run_extract_now(
    server_url: str,
    version: str,
    site_id: str,
    token: str,
    *,
    target_id: str,
) -> str:
    """Trigger an immediate one-off extract refresh for one datasource.

    Returns the queued background job's id. This uses Tableau Cloud's "Update
    Data Source Now" endpoint, which starts the refresh straight away and creates
    NO recurring schedule — the opposite of create_extract_task(). An empty
    <tsRequest/> body runs the datasource's standard refresh, so the CSV's
    schedule columns (and refresh_type) don't apply here.
    """
    resp = httpx.post(
        f"{server_url}/api/{version}/sites/{site_id}/datasources/{target_id}/refresh",
        content=b"<tsRequest></tsRequest>",
        headers={
            "X-Tableau-Auth": token,
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        },
        timeout=30,
    )
    resp.raise_for_status()
    # The queued refresh's id comes back as an attribute on <job>.
    node = ET.fromstring(resp.content).find(".//t:job", NS)
    return node.get("id", "") if node is not None else ""


# ---------------------------------------------------------------------------
# Section (c): CSV row handling + orchestration
# ---------------------------------------------------------------------------


def resolve(identifier: str, by_id: dict, by_name: dict) -> tuple[str, str]:
    """Resolve a name-or-LUID to (luid, display_name). Raises ValueError on miss.

    `by_id` maps luid -> name; `by_name` maps name -> [luids] (a list, so we can
    detect two datasources sharing a name). A cell that looks like a UUID is
    treated as a LUID; anything else is treated as a name to look up.
    """
    if UUID_RE.match(identifier):
        # Looks like a LUID: accept it only if it actually exists on the site.
        if identifier in by_id:
            return identifier, by_id[identifier]
        raise ValueError(f"LUID not found on site: {identifier}")
    # Otherwise treat it as a name. Refuse to guess when it's missing/ambiguous.
    matches = by_name.get(identifier, [])
    if not matches:
        raise ValueError(f"datasource name not found: {identifier!r}")
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous name {identifier!r} ({len(matches)} datasources share it) — use the LUID"
        )
    return matches[0], identifier


def row_to_payload(row: dict) -> dict:
    """Turn a CSV row into a build_schedule_xml payload (omitting blank cells).

    Blank cells are dropped so each frequency sees only the fields it needs.
    `hours`/`minutes` are cast to int (build_schedule_xml compares them
    numerically) and `week_days` is split on ';' into a list.
    """
    payload: dict = {}
    # Straight string fields: copy through only when non-empty.
    for key in ("frequency", "start", "end", "week_day", "month_day", "month_occurrence"):
        val = (row.get(key) or "").strip()
        if val:
            payload[key] = val
    # Numeric fields: cast to int; a bad value fails this row (caught in main).
    for key in ("hours", "minutes"):
        val = (row.get(key) or "").strip()
        if val:
            try:
                payload[key] = int(val)
            except ValueError:
                raise ValueError(f"{key} must be an integer, got {val!r}")
    # Multi-value field: "Monday;Wednesday" -> ["Monday", "Wednesday"].
    week_days = (row.get("week_days") or "").strip()
    if week_days:
        payload["week_days"] = [d.strip() for d in week_days.split(";") if d.strip()]
    return payload


def _parse_args(argv: list[str]) -> tuple[bool, Path]:
    """Parse the CLI args into (run_now, csv_path).

    Accepts an optional `--now` (alias `--run-now`) flag and an optional CSV path,
    in any order. `-h`/`--help` prints the module docstring and exits. Anything
    else beginning with '-' is rejected so typos don't get silently ignored.
    """
    run_now = False
    positional: list[str] = []
    for arg in argv:
        if arg in ("--now", "--run-now"):
            run_now = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        elif arg.startswith("-"):
            raise SystemExit(f"Unknown option: {arg} (use --now to run refreshes immediately)")
        else:
            positional.append(arg)
    if len(positional) > 1:
        raise SystemExit("Expected at most one CSV path argument")
    csv_path = Path(positional[0]) if positional else DEFAULT_CSV
    return run_now, csv_path


def main() -> None:
    """Wire the pieces together: sign in, then either schedule or run each row."""
    # 1. Parse the CLI: optional --now flag plus an optional CSV path.
    run_now, csv_path = _parse_args(sys.argv[1:])

    # 2. Load .env from the repo root (works regardless of the current dir) and
    #    read/validate the connection settings.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    cfg = load_config()

    # 3. Make sure the list file exists before we bother signing in.
    if not csv_path.exists():
        raise SystemExit(f"Datasource list file not found: {csv_path}")

    # 4. Discover the REST version and sign in.
    site_label = cfg["site_content_url"] or "<default>"
    print(f"Signing in to {cfg['server_url']} (site: {site_label}) ...")
    version = discover_version(cfg["server_url"])
    token, site_id = sign_in(cfg["server_url"], version, cfg)

    # 5. Fetch all datasources once and index them for fast row resolution.
    datasources = list_datasources(cfg["server_url"], version, site_id, token)
    by_id = {d["id"]: d["name"] for d in datasources}
    by_name: dict[str, list[str]] = {}
    for d in datasources:
        by_name.setdefault(d["name"], []).append(d["id"])
    print(f"Signed in OK (REST {version}). {len(datasources)} datasource(s) on site.")

    # 5b. State plainly which of the two things is about to happen, so a --now run
    #     can never be mistaken for scheduling (or vice versa).
    if run_now:
        print(
            "RUN-NOW mode: triggering an IMMEDIATE one-off extract refresh for each "
            "datasource.\nNo schedules are created — the schedule columns in the CSV "
            "are ignored.\n"
        )
    else:
        print("SCHEDULE mode: creating one recurring extract-refresh task per row.\n")

    # 6. Process each row independently (fail-soft): a bad row is reported and
    #    skipped so one mistake never sinks the rest of the batch. Line numbers
    #    start at 2 because line 1 is the CSV header.
    done = failed = 0
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            identifier = (row.get("datasource") or "").strip()
            if not identifier:
                continue  # blank line
            refresh_type = (row.get("refresh_type") or "").strip() or "FullRefresh"
            try:
                # Every row first resolves its name/LUID to a real datasource id.
                luid, name = resolve(identifier, by_id, by_name)
                if run_now:
                    # Run the extract once, right now — no schedule is created.
                    job_id = run_extract_now(
                        cfg["server_url"], version, site_id, token, target_id=luid
                    )
                    done += 1
                    print(f"  OK   line {lineno}: {name} -> refresh started now (job {job_id})")
                else:
                    # Build this row's inline schedule and create the recurring task.
                    payload = row_to_payload(row)
                    schedule_xml = build_schedule_xml(payload)
                    task_id = create_extract_task(
                        cfg["server_url"],
                        version,
                        site_id,
                        token,
                        refresh_type=refresh_type,
                        target_id=luid,
                        schedule_xml=schedule_xml,
                    )
                    done += 1
                    print(f"  OK   line {lineno}: {name} [{payload.get('frequency', '?')}] -> task {task_id}")
            except ValueError as exc:
                # Resolution or schedule-validation problem (ScheduleError is a
                # ValueError too) — the row's own fault, report and continue.
                failed += 1
                print(f"  FAIL line {lineno}: {identifier} -> {exc}")
            except httpx.HTTPStatusError as exc:
                # Tableau rejected the call; surface its response body.
                failed += 1
                detail = (exc.response.text or "").strip().replace("\n", " ")[:300]
                print(f"  FAIL line {lineno}: {identifier} -> HTTP {exc.response.status_code}: {detail}")
            except httpx.HTTPError as exc:
                # Transport-level problem (timeout, connection error, ...).
                failed += 1
                print(f"  FAIL line {lineno}: {identifier} -> {exc}")

    # 7. Summary line; non-zero exit code if anything failed (handy for CI/cron).
    action = "refresh(es) started" if run_now else "task(s) created"
    print(f"\nDone: {done} {action}, {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
