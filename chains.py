"""Sequential "chain" runner for extract-refresh tasks.

Tableau Cloud has no native task dependencies (see the module docstrings in
tableau.py and schedules.py for the same conclusion re: schedules) - every
task's schedule is independent of every other task's. This module lets the
app run a user-defined ordered sequence of extract-refresh tasks, waiting for
each one to finish successfully before starting the next.

This only chains runs triggered here (via `start_chain`) - it does not
intercept a task's own Tableau-set schedule. For that, see the sibling
`tableau-webhooks` project (formerly tab2slack), which listens for Tableau's
*RefreshSucceeded webhook events and triggers the next task automatically
whenever the resource's own schedule completes on its own.

This module has no FastAPI dependency - `execute()` is a plain, synchronous
function. The caller (app.py) is responsible for scheduling it to run in the
background (e.g. via FastAPI's BackgroundTasks).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tableauserverclient.server.endpoint.exceptions import JobFailedException

import tableau


@dataclass
class ChainStep:
    task_id: str
    status: str = "pending"  # pending | running | success | failed
    job_id: str | None = None
    error: str | None = None


@dataclass
class ChainRun:
    id: str
    steps: list[ChainStep]
    status: str = "running"  # running | success | failed
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_runs: dict[str, ChainRun] = {}


def start_chain(task_ids: list[str]) -> ChainRun:
    """Create a new chain run in "running" state and register it.

    Does not execute anything - the caller schedules `execute()` separately
    (kept apart so this module doesn't need to know about FastAPI).
    """
    if not task_ids:
        raise ValueError("task_ids must not be empty")
    run = ChainRun(id=str(uuid.uuid4()), steps=[ChainStep(task_id=t) for t in task_ids])
    _runs[run.id] = run
    return run


def execute(session: tableau.TableauSession, run: ChainRun) -> None:
    """Run each step in order, waiting for completion before starting the
    next. Stops at the first failure (fail-closed) rather than continuing."""
    for step in run.steps:
        step.status = "running"
        try:
            job_id = tableau.run_extract_task_now(session, step.task_id)
            step.job_id = job_id
            session.call(lambda server: server.jobs.wait_for_job(job_id))
            step.status = "success"
        except JobFailedException as exc:
            # JobCancelledException is a subclass of JobFailedException, so
            # this also covers a cancelled run.
            step.status = "failed"
            step.error = str(exc)
            run.status = "failed"
            return
        except Exception as exc:  # noqa: BLE001 - record and stop; never crash the background task
            step.status = "failed"
            step.error = str(exc)
            run.status = "failed"
            return
    run.status = "success"


def get_run(run_id: str) -> ChainRun | None:
    return _runs.get(run_id)


def list_runs() -> list[ChainRun]:
    return sorted(_runs.values(), key=lambda r: r.created_at, reverse=True)


def run_to_dict(run: ChainRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "created_at": run.created_at,
        "steps": [
            {"task_id": s.task_id, "status": s.status, "job_id": s.job_id, "error": s.error}
            for s in run.steps
        ],
    }
