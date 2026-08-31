"""Tableau Cloud session handling plus list/create helpers.

Reads (jobs, subscriptions, extract-refresh tasks, workbooks, datasources,
views, users) go through `tableauserverclient` (TSC), which is well-supported
for these calls.

Creates (subscriptions, extract-refresh tasks) and run-now go through direct
REST calls instead of TSC's create() methods. Tableau Cloud creates these with
an *inline* <schedule> element (no server-wide schedule to reference), and
that inline-schedule payload shape is Cloud-only (API >= 3.20) and not
uniformly covered across tableauserverclient versions. Building the documented
XML body directly, reusing the already-signed-in TSC session's auth token and
site, is guaranteed to match the Tableau Cloud REST API reference exactly.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import httpx
import tableauserverclient as TSC

REQUIRED_ENV_VARS = (
    "TABLEAU_PAT_NAME",
    "TABLEAU_PAT_SECRET",
    "TABLEAU_SERVER_URL",
)


@dataclass(frozen=True)
class TableauConfig:
    pat_name: str
    pat_secret: str
    server_url: str
    site_content_url: str

    @classmethod
    def from_env(cls) -> "TableauConfig":
        missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill in your values."
            )
        return cls(
            pat_name=os.environ["TABLEAU_PAT_NAME"],
            pat_secret=os.environ["TABLEAU_PAT_SECRET"],
            server_url=os.environ["TABLEAU_SERVER_URL"].rstrip("/"),
            site_content_url=os.environ.get("TABLEAU_SITE_CONTENT_URL", ""),
        )


class TableauSession:
    """Lazily signs in to Tableau Cloud and caches the session.

    A Personal Access Token is single-use-at-a-time: signing in elsewhere
    invalidates this session. If a call fails with a 401, we re-sign-in once
    and retry, which is enough for a single-user local tool.
    """

    def __init__(self, config: TableauConfig | None = None):
        self.config = config or TableauConfig.from_env()
        self._server: TSC.Server | None = None
        self._lock = threading.Lock()

    def _sign_in(self) -> TSC.Server:
        auth = TSC.PersonalAccessTokenAuth(
            self.config.pat_name,
            self.config.pat_secret,
            site_id=self.config.site_content_url,
        )
        server = TSC.Server(self.config.server_url, use_server_version=True)
        server.auth.sign_in(auth)
        return server

    @property
    def server(self) -> TSC.Server:
        with self._lock:
            if self._server is None:
                self._server = self._sign_in()
            return self._server

    def call(self, fn):
        """Run `fn(server)`, re-signing in once if the session expired."""
        try:
            return fn(self.server)
        except TSC.ServerResponseError as exc:
            if getattr(exc, "code", None) == "401002":
                with self._lock:
                    self._server = self._sign_in()
                return fn(self._server)
            raise

    # -- REST fallback (creates + run-now) ---------------------------------

    def rest_post_xml(self, path: str, body_xml: str) -> httpx.Response:
        """POST a raw <tsRequest> XML body to a site-scoped REST endpoint.

        `path` is relative to the site, e.g. "subscriptions" or
        "tasks/extractRefreshes/<task-id>/runNow".
        """

        def _do(server: TSC.Server) -> httpx.Response:
            url = (
                f"{self.config.server_url}/api/{server.version}/sites/"
                f"{server.site_id}/{path}"
            )
            headers = {
                "X-Tableau-Auth": server.auth_token,
                "Content-Type": "application/xml",
                "Accept": "application/xml",
            }
            resp = httpx.post(url, content=body_xml.encode("utf-8"), headers=headers, timeout=30)
            if resp.status_code == 401:
                raise TSC.ServerResponseError("401002", "Session expired", "")
            resp.raise_for_status()
            return resp

        return self.call(_do)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _get(obj, attr, default=None):
    return getattr(obj, attr, default)


def job_to_dict(job) -> dict:
    """`server.jobs.get()` (via TSC.Pager) yields `BackgroundJobItem`, which has
    a different attribute set than the `JobItem` returned by `get_by_id`."""
    return {
        "id": job.id,
        "type": _get(job, "type"),
        "status": _get(job, "status"),
        "title": _get(job, "title"),
        "subtitle": _get(job, "subtitle"),
        "priority": _get(job, "priority"),
        "created_at": str(_get(job, "created_at", "") or ""),
        "started_at": str(_get(job, "started_at", "") or ""),
        "ended_at": str(_get(job, "ended_at", "") or ""),
    }


def subscription_to_dict(sub: TSC.SubscriptionItem) -> dict:
    target = _get(sub, "target")
    # On Cloud, subscriptions carry an inline schedule (a list of ScheduleItem,
    # since there's no shared schedule_id to reference) instead of schedule_id.
    schedule_list = _get(sub, "schedule") or []
    interval = _get(schedule_list[0], "interval_item") if schedule_list else None
    return {
        "id": sub.id,
        "subject": _get(sub, "subject"),
        "user_id": _get(sub, "user_id"),
        "target_type": _get(target, "type") if target else None,
        "target_id": _get(target, "id") if target else None,
        "suspended": _get(sub, "suspended"),
        "schedule_frequency": str(interval) if interval else None,
    }


def task_to_dict(task: TSC.TaskItem) -> dict:
    target = _get(task, "target")
    schedule_item = _get(task, "schedule_item")
    interval = _get(schedule_item, "interval_item") if schedule_item else None
    return {
        "id": task.id,
        "task_type": _get(task, "task_type"),
        "priority": _get(task, "priority"),
        "target_type": _get(target, "type") if target else None,
        "target_id": _get(target, "id") if target else None,
        "consecutive_failed_count": _get(task, "consecutive_failed_count"),
        "last_run_at": str(_get(task, "last_run_at", "") or ""),
        "schedule_frequency": str(interval) if interval else None,
    }


def list_jobs(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [job_to_dict(j) for j in TSC.Pager(server.jobs)]

    return session.call(_do)


def list_subscriptions(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [subscription_to_dict(s) for s in TSC.Pager(server.subscriptions)]

    return session.call(_do)


def list_extract_tasks(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [task_to_dict(t) for t in TSC.Pager(server.tasks)]

    return session.call(_do)


def list_workbooks(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [{"id": w.id, "name": w.name} for w in TSC.Pager(server.workbooks)]

    return session.call(_do)


def list_datasources(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [{"id": d.id, "name": d.name} for d in TSC.Pager(server.datasources)]

    return session.call(_do)


def list_views(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [{"id": v.id, "name": v.name} for v in TSC.Pager(server.views)]

    return session.call(_do)


def list_users(session: TableauSession) -> list[dict]:
    def _do(server: TSC.Server):
        return [{"id": u.id, "name": u.name} for u in TSC.Pager(server.users)]

    return session.call(_do)


# ---------------------------------------------------------------------------
# Create helpers (REST, see module docstring)
# ---------------------------------------------------------------------------


def create_subscription(
    session: TableauSession,
    *,
    subject: str,
    content_type: str,
    content_id: str,
    user_id: str,
    schedule_xml: str,
) -> str:
    """Create a Tableau Cloud subscription. Returns the new subscription id."""
    body = (
        "<tsRequest>"
        f'<subscription subject="{_esc(subject)}">'
        f'<content id="{_esc(content_id)}" type="{_esc(content_type)}"/>'
        f'<user id="{_esc(user_id)}"/>'
        "</subscription>"
        f"{schedule_xml}"
        "</tsRequest>"
    )
    resp = session.rest_post_xml("subscriptions", body)
    return _extract_id(resp.text, "subscription")


def create_extract_task(
    session: TableauSession,
    *,
    refresh_type: str,
    target_type: str,
    target_id: str,
    schedule_xml: str,
) -> str:
    """Create a Tableau Cloud extract refresh task. Returns the new task id.

    `refresh_type` is "FullRefresh" or "IncrementalExtract".
    `target_type` is "workbook" or "datasource".
    """
    body = (
        "<tsRequest>"
        f'<extractRefresh type="{_esc(refresh_type)}">'
        f'<{target_type} id="{_esc(target_id)}"/>'
        "</extractRefresh>"
        f"{schedule_xml}"
        "</tsRequest>"
    )
    resp = session.rest_post_xml("tasks/extractRefreshes", body)
    return _extract_id(resp.text, "extractRefresh")


def run_extract_task_now(session: TableauSession, task_id: str) -> str:
    """Trigger an existing extract refresh task immediately. Returns the job id."""
    resp = session.rest_post_xml(f"tasks/extractRefreshes/{task_id}/runNow", "<tsRequest/>")
    return _extract_id(resp.text, "job")


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _extract_id(xml_text: str, tag: str) -> str:
    import re

    match = re.search(rf'<{tag}\b[^>]*\bid="([^"]+)"', xml_text)
    return match.group(1) if match else ""
