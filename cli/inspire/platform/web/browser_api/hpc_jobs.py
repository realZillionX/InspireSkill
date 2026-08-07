"""Browser (web-session) APIs for HPC jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import WebSession, get_web_session

__all__ = [
    "HPCJobInfo",
    "create_hpc_job",
    "delete_hpc_job",
    "get_hpc_job_detail",
    "list_hpc_job_instances",
    "list_hpc_jobs",
    "list_hpc_job_events",
    "list_hpc_job_logs",
    "stop_hpc_job",
]


@dataclass
class HPCJobInfo:
    """HPC job information."""

    job_id: str
    name: str
    status: str
    entrypoint: str
    created_at: str
    finished_at: Optional[str]
    created_by_name: str
    created_by_id: str
    project_id: str
    project_name: str
    compute_group_name: str
    workspace_id: str

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "HPCJobInfo":
        created_by = data.get("created_by", {}) if isinstance(data.get("created_by"), dict) else {}
        return cls(
            job_id=data.get("job_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            entrypoint=data.get("entrypoint", data.get("command", "")),
            created_at=data.get("created_at", ""),
            finished_at=data.get("finished_at"),
            created_by_name=created_by.get("name", ""),
            created_by_id=created_by.get("id", ""),
            project_id=data.get("project_id", ""),
            project_name=data.get("project_name", ""),
            compute_group_name=data.get("logic_compute_group_name", ""),
            workspace_id=data.get("workspace_id", ""),
        )


def create_hpc_job(
    *,
    payload: dict[str, Any],
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Create an HPC job via the current Web UI v2 Action API."""
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=CreateJobConsole",
        referer=f"{_get_base_url()}/jobs/hpc",
        body=payload,
        timeout=60,
    )
    try:
        return _v2_result(data)
    except ValueError as exc:
        message = str(exc).lower()
        if "unknown field" not in message or "task" not in message:
            raise
        retry_payload = dict(payload)
        retry_payload.pop("task_priority", None)
        data = _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=CreateJobConsole",
            referer=f"{_get_base_url()}/jobs/hpc",
            body=retry_payload,
            timeout=60,
        )
        return _v2_result(data)


def get_hpc_job_detail(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch an HPC job via the current Web UI v2 Action API."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=GetJob",
        referer=f"{_get_base_url()}/jobs/hpcDetail/{job_id}",
        body={"job_id": job_id},
        timeout=30,
    )
    return _v2_result(data)


def stop_hpc_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Stop an HPC job via the current Web UI v2 Action API."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        "/api/v2/hpc?Action=StopJob",
        referer=f"{_get_base_url()}/jobs/hpc",
        body={"job_id": job_id},
        timeout=30,
    )
    return _v2_result(data)


def list_hpc_jobs(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[HPCJobInfo], int]:
    """List HPC jobs using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")
    if created_by is None:
        data = _request_json(
            session,
            "GET",
            _browser_api_path("/user/detail"),
            referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
            timeout=30,
        )
        user_payload = data.get("data")
        current_user: dict[str, Any] = user_payload if isinstance(user_payload, dict) else {}
        created_by = str(current_user.get("id") or current_user.get("user_id") or "").strip()
        if not created_by:
            raise ValueError("Current user could not be resolved for HPC listing.")

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
        "created_by": created_by,
    }
    if status:
        body["status"] = status

    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=ListJobs",
            referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
            body=body,
            timeout=30,
        )
    )
    jobs_data = payload.get("jobs")
    if not isinstance(jobs_data, list):
        jobs_data = payload.get("items")
    if not isinstance(jobs_data, list):
        jobs_data = []

    total = payload.get("total")
    if not isinstance(total, int):
        total = len(jobs_data)

    jobs = [HPCJobInfo.from_api_response(item) for item in jobs_data if isinstance(item, dict)]
    return jobs, total


def list_hpc_job_events(
    job_id: str,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List platform events for an HPC job.

    Action: ``ListJobEvents``. This wrapper fetches job-level events. Use
    :func:`list_hpc_job_instances` for the component inventory shown on the
    job detail page.

    Returns ``[]`` on any error. The platform garbage-collects events for
    completed jobs, so a not-found answer is a normal steady state after event
    retention expires.
    """
    try:
        if session is None:
            session = get_web_session()

        payload = _v2_result(
            _request_json(
                session,
                "POST",
                "/api/v2/hpc?Action=ListJobEvents",
                referer=f"{_get_base_url()}/jobs/hpcDetail/{job_id}",
                body={
                    "pageNum": -1,
                    "pageSize": 200,
                    "filter": {"object_ids": [job_id], "object_type": "HPC_JOB"},
                    "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
                },
                timeout=30,
            )
        )
        for key in ("events", "items", "list"):
            events = payload.get(key)
            if isinstance(events, list):
                return events
        return []
    except Exception:
        return []


def list_hpc_job_instances(
    job_id: str,
    *,
    limit: int = 500,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List pod/component instances for an HPC job.

    Endpoint: ``POST /api/v1/hpc_jobs/instances/list`` with body
    ``{jobId, page_num, page_size}``.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if limit < 1:
        raise ValueError("limit must be positive")

    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=ListJobInstances",
            referer=f"{_get_base_url()}/jobs/hpcDetail/{job_id}",
            body={"jobId": job_id, "page_num": 1, "page_size": limit},
            timeout=30,
        )
    )
    items = payload.get("items")
    if not isinstance(items, list):
        items = payload.get("list")
    if not isinstance(items, list):
        items = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except ValueError:
        total = len(items)
    return [item for item in items if isinstance(item, dict)], total


def list_hpc_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    job_id: str | None = None,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch aggregated HPC logs via ``POST /api/v1/logs/hpc``.

    The platform rejects explicit sorter fields on this endpoint, including
    ``@timestamp``. Send no sorter and sort client-side if needed.
    """
    if session is None:
        session = get_web_session()
    detail = f"/jobs/hpcDetail/{job_id}" if job_id else "/jobs/highPerformanceComputing"
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=GetJobLog",
            referer=f"{_get_base_url()}{detail}",
            body={
                "page_size": page_size,
                "filter": {
                    "podNames": pod_names,
                    "start_timestamp_ms": str(start_timestamp_ms),
                    "end_timestamp_ms": str(end_timestamp_ms),
                },
            },
            timeout=30,
        )
    )
    logs = payload.get("logs")
    if not isinstance(logs, list):
        logs = payload.get("items")
    if not isinstance(logs, list):
        logs = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(logs)
    except ValueError:
        total = len(logs)
    return [item for item in logs if isinstance(item, dict)], total


def delete_hpc_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Permanently delete an HPC job entry from the platform.

    Action: ``DeleteJob``. v1 needed the REST-style
    ``DELETE /api/v1/hpc_jobs/{id}`` because ``POST /hpc_jobs/delete`` was a
    404; v2 has a first-class Action, so the special case is gone.

    Destructive: the entry disappears from the UI — if the job is still
    running, ``stop`` it first. An id that does not resolve comes back as
    ``ResourceNotFound``.
    """
    if session is None:
        session = get_web_session()

    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=DeleteJob",
            referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
            body={"job_id": job_id},
            timeout=30,
        )
    )
