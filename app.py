"""FastAPI backend: JSON API over Tableau Cloud, plus the static frontend.

Run with:  uvicorn app:app --reload
"""

from __future__ import annotations

from pathlib import Path

import tableauserverclient as TSC
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import schedules
import tableau

load_dotenv()

app = FastAPI(title="Tableau Cloud Job Manager")

# Validated eagerly so a missing .env value fails at startup, not on first click.
_session = tableau.TableauSession()


class ScheduleInput(BaseModel):
    frequency: str
    start: str | None = None
    end: str | None = None
    hours: float | None = None
    minutes: float | None = None
    week_days: list[str] | None = None
    week_day: str | None = None
    month_day: str | None = None
    month_occurrence: str | None = None


class SubscriptionCreate(BaseModel):
    subject: str
    content_type: str  # "Workbook" or "View"
    content_id: str
    user_id: str
    schedule: ScheduleInput


class ExtractTaskCreate(BaseModel):
    refresh_type: str  # "FullRefresh" or "IncrementalExtract"
    target_type: str  # "workbook" or "datasource"
    target_id: str
    schedule: ScheduleInput


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except schedules.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TSC.ServerResponseError as exc:
        raise HTTPException(status_code=502, detail=f"Tableau Cloud error: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface as a clean API error either way
        raise HTTPException(status_code=502, detail=f"Tableau Cloud request failed: {exc}")


# -- Reads --------------------------------------------------------------


@app.get("/api/jobs")
def get_jobs():
    return _run(tableau.list_jobs, _session)


@app.get("/api/subscriptions")
def get_subscriptions():
    return _run(tableau.list_subscriptions, _session)


@app.get("/api/extract-tasks")
def get_extract_tasks():
    return _run(tableau.list_extract_tasks, _session)


@app.get("/api/workbooks")
def get_workbooks():
    return _run(tableau.list_workbooks, _session)


@app.get("/api/datasources")
def get_datasources():
    return _run(tableau.list_datasources, _session)


@app.get("/api/views")
def get_views():
    return _run(tableau.list_views, _session)


@app.get("/api/users")
def get_users():
    return _run(tableau.list_users, _session)


# -- Creates --------------------------------------------------------------


@app.post("/api/subscriptions")
def add_subscription(body: SubscriptionCreate):
    schedule_xml = _run(schedules.build_schedule_xml, body.schedule.model_dump(exclude_none=True))
    new_id = _run(
        tableau.create_subscription,
        _session,
        subject=body.subject,
        content_type=body.content_type,
        content_id=body.content_id,
        user_id=body.user_id,
        schedule_xml=schedule_xml,
    )
    return {"id": new_id}


@app.post("/api/extract-tasks")
def add_extract_task(body: ExtractTaskCreate):
    schedule_xml = _run(schedules.build_schedule_xml, body.schedule.model_dump(exclude_none=True))
    new_id = _run(
        tableau.create_extract_task,
        _session,
        refresh_type=body.refresh_type,
        target_type=body.target_type,
        target_id=body.target_id,
        schedule_xml=schedule_xml,
    )
    return {"id": new_id}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
