"""FastAPI backend: JSON API over Tableau Cloud, plus the static frontend.

Run with:  uvicorn app:app --reload
"""

from __future__ import annotations

from pathlib import Path

import tableauserverclient as TSC
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import chains
import jobdetail
import jobstore
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


class ChainCreate(BaseModel):
    task_ids: list[str]


class DetailRefreshCreate(BaseModel):
    scope: str  # "all" or "unknown"


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
    jobs = _run(tableau.list_jobs, _session)
    for job in jobs:
        job["detail"] = jobstore.get(job["id"])
    return jobs


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


# -- Deletes / actions on existing tasks & jobs ------------------------------


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _run(tableau.cancel_job, _session, job_id)
    return {"cancelled": job_id}


@app.post("/api/extract-tasks/{task_id}/run")
def run_extract_task(task_id: str):
    job_id = _run(tableau.run_extract_task_now, _session, task_id)
    return {"job_id": job_id}


@app.delete("/api/extract-tasks/{task_id}")
def delete_extract_task(task_id: str):
    _run(tableau.delete_extract_task, _session, task_id)
    return {"deleted": task_id}


@app.delete("/api/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str):
    _run(tableau.delete_subscription, _session, subscription_id)
    return {"deleted": subscription_id}


# -- Chains -----------------------------------------------------------------


@app.post("/api/chains")
def add_chain(body: ChainCreate, background_tasks: BackgroundTasks):
    try:
        run = chains.start_chain(body.task_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    background_tasks.add_task(chains.execute, _session, run)
    return {"id": run.id}


@app.get("/api/chains/{run_id}")
def get_chain(run_id: str):
    run = chains.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No chain run with that id")
    return chains.run_to_dict(run)


@app.get("/api/chains")
def get_chains():
    return [chains.run_to_dict(r) for r in chains.list_runs()]


# -- Job detail admin --------------------------------------------------------


@app.post("/api/jobs/detail/refresh")
def refresh_job_detail(body: DetailRefreshCreate, background_tasks: BackgroundTasks):
    if body.scope not in ("all", "unknown"):
        raise HTTPException(status_code=400, detail="scope must be 'all' or 'unknown'")
    if not jobdetail.start_refresh(body.scope):
        raise HTTPException(status_code=409, detail="A job-detail refresh is already running")
    background_tasks.add_task(jobdetail.execute, _session, body.scope)
    return jobdetail.get_status()


@app.get("/api/jobs/detail/status")
def get_job_detail_status():
    return jobdetail.get_status()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
