"""Background batch refresh of persisted per-job detail (see jobstore.py).

Mirrors chains.py's split: `start_refresh()` registers the run and is called
from the request handler; `execute()` is a plain synchronous function the
caller schedules separately (e.g. via FastAPI's BackgroundTasks) so this
module doesn't need to know about FastAPI.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import tableauserverclient as TSC

import tableau
import jobstore

_UNSUPPORTED_JOB_TYPE_CODE = "400031"  # Tableau REST API doesn't expose detail for e.g. materialize_views

_lock = threading.Lock()
_MAX_SAMPLE_ERRORS = 20
_state = {
    "scope": None,
    "status": "idle",  # idle | running | done | error
    "total": 0,
    "done": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "sample_errors": [],  # up to _MAX_SAMPLE_ERRORS {job_id, error} from per-job failures
}


def start_refresh(scope: str) -> bool:
    """Registers a new refresh run if none is in progress. Returns False if a
    refresh is already running (the caller should treat that as a conflict)."""
    with _lock:
        if _state["status"] == "running":
            return False
        _state.update(
            scope=scope,
            status="running",
            total=0,
            done=0,
            failed=0,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            error=None,
            sample_errors=[],
        )
        return True


def get_status() -> dict:
    with _lock:
        return dict(_state)


def _unsupported_detail(job: dict) -> dict:
    """Stub detail for a job type the Tableau REST API refuses to look up by
    id (e.g. materialize_views). Persisting this - instead of leaving the job
    unknown - stops every future "unknown"-scope refresh from retrying a call
    that is guaranteed to fail again."""
    return {
        "type": job.get("type"),
        "progress": None,
        "finish_code": None,
        "notes": [f"Detail unavailable: Tableau's REST API does not support job type '{job.get('type')}'."],
        "mode": None,
        "datasource_id": None,
        "datasource_name": None,
        "workbook_id": None,
        "workbook_name": None,
        "target_name": None,
        "updated_at": "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "unsupported": True,
    }


def _record_error(job_id: str, exc: Exception) -> None:
    with _lock:
        _state["failed"] += 1
        if len(_state["sample_errors"]) < _MAX_SAMPLE_ERRORS:
            _state["sample_errors"].append({"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"})


def execute(session: tableau.TableauSession, scope: str) -> None:
    """Fetches detail for every job (scope="all") or only jobs with no
    persisted detail yet (scope="unknown"), saving to jobstore as it goes.
    Never raises - failures are recorded in the status instead."""
    try:
        jobs = tableau.list_jobs(session)
        if scope == "unknown":
            known = jobstore.known_ids()
            jobs = [j for j in jobs if j["id"] not in known]

        with _lock:
            _state["total"] = len(jobs)

        for i, job in enumerate(jobs, start=1):
            job_id = job["id"]
            try:
                detail = tableau.get_job_detail(session, job_id)
            except TSC.ServerResponseError as exc:
                if getattr(exc, "code", None) == _UNSUPPORTED_JOB_TYPE_CODE:
                    jobstore.upsert(job_id, _unsupported_detail(job), flush=False)
                    with _lock:
                        _state["done"] += 1
                else:
                    _record_error(job_id, exc)
            except Exception as exc:  # noqa: BLE001 - record and keep going
                _record_error(job_id, exc)
            else:
                jobstore.upsert(job_id, detail, flush=False)
                with _lock:
                    _state["done"] += 1
            if i % 25 == 0:
                jobstore.save()

        jobstore.save()
        with _lock:
            _state["status"] = "done"
            _state["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001 - never crash the background task
        jobstore.save()
        with _lock:
            _state["status"] = "error"
            _state["error"] = str(exc)
            _state["finished_at"] = datetime.now(timezone.utc).isoformat()
