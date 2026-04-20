"""Browser (web-session) APIs for jobs and users.

The web UI exposes job listing endpoints (and related user listings) that are
not part of the OpenAPI surface. These endpoints require a web-session cookie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "JobInfo",
    "get_current_user",
    "get_train_job_workdir",
    "list_job_events",
    "list_job_instance_events",
    "list_job_users",
    "list_jobs",
]


@dataclass
class JobInfo:
    """Training job information."""

    job_id: str
    name: str
    status: str
    command: str
    created_at: str
    finished_at: Optional[str]
    created_by_name: str
    created_by_id: str
    project_id: str
    project_name: str
    compute_group_name: str
    gpu_type: str
    gpu_count: int
    instance_count: int
    priority: int
    workspace_id: str

    @classmethod
    def from_api_response(cls, data: dict) -> "JobInfo":
        framework_config = data.get("framework_config", [{}])[0]
        gpu_info = framework_config.get("instance_spec_price_info", {}).get("gpu_info", {})

        return cls(
            job_id=data.get("job_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            command=data.get("command", ""),
            created_at=data.get("created_at", ""),
            finished_at=data.get("finished_at"),
            created_by_name=data.get("created_by", {}).get("name", ""),
            created_by_id=data.get("created_by", {}).get("id", ""),
            project_id=data.get("project_id", ""),
            project_name=data.get("project_name", ""),
            compute_group_name=data.get("logic_compute_group_name", ""),
            gpu_type=gpu_info.get("gpu_type_display", ""),
            gpu_count=framework_config.get("gpu_count", 0),
            instance_count=framework_config.get("instance_count", 1),
            priority=data.get("priority", 0),
            workspace_id=data.get("workspace_id", ""),
        )


def list_jobs(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[JobInfo], int]:
    """List training jobs using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
    }

    if created_by:
        body["created_by"] = created_by
    if status:
        body["status"] = status

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/list"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    jobs_data = data.get("data", {}).get("jobs", [])
    total = data.get("data", {}).get("total", 0)

    jobs = [JobInfo.from_api_response(j) for j in jobs_data]
    return jobs, total


def get_current_user(session: Optional[WebSession] = None) -> dict:
    """Get current user details."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "GET",
        _browser_api_path("/user/detail"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        timeout=30,
    )
    return data.get("data", {})


def list_job_users(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List users who have created jobs."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/users"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body={"workspace_id": workspace_id},
        timeout=30,
    )
    return data.get("data", {}).get("items", [])


def get_train_job_workdir(
    *,
    project_id: str,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> str | None:
    """Fetch the training job workdir for a project within a workspace."""
    if session is None:
        session = get_web_session()

    project_id = str(project_id or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not project_id or not workspace_id:
        raise ValueError("project_id and workspace_id are required")

    body = {
        "project_id": project_id,
        "workspace_id": workspace_id,
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/workdir"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data")
    if isinstance(payload, str):
        value = payload.strip()
        return value or None

    return None


def list_job_events(
    job_id: str,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List job-level K8s events for a training job.

    Endpoint: ``POST /api/v1/train_job/job_event_list``. Returns controller-
    level events (e.g. ``SetPodTemplateSchedulerName``, ``Unschedulable``
    reported at the pytorchjob controller level). For per-pod events (e.g.
    ``FailedScheduling`` / ``Scheduled`` from the K8s scheduler on specific
    pods), use :func:`list_job_instance_events` instead.

    Best-effort: returns ``[]`` on any error.
    """
    try:
        if session is None:
            session = get_web_session()

        data = _request_json(
            session,
            "POST",
            _browser_api_path("/train_job/job_event_list"),
            referer=f"{_get_base_url()}/jobs/distributedTraining",
            body={"job_id": job_id},
            timeout=30,
        )

        if data.get("code") != 0:
            return []

        events = data.get("data", {}).get("events", [])
        if not isinstance(events, list):
            return []
        return events
    except Exception:
        return []


def list_job_instance_events(
    job_id: str,
    pod_names: list[str],
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List per-pod K8s events for a training job.

    Endpoint: ``POST /api/v1/train_job/events/list`` with
    ``filter.object_type="instance"`` and ``filter.object_ids=[<pod>, ...]``.
    Returns pod-level events (scheduler view — ``FailedScheduling`` /
    ``Scheduled`` / ``Pulling`` / ``Started``), richer than the job-level
    endpoint.

    `job_id` is only used for the Referer header; the filter keys off
    `pod_names` exclusively. Best-effort: returns ``[]`` on any error.
    """
    if not pod_names:
        return []
    try:
        if session is None:
            session = get_web_session()

        data = _request_json(
            session,
            "POST",
            _browser_api_path("/train_job/events/list"),
            referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
            body={
                "page_num": 1,
                "page_size": 200,
                "filter": {
                    "object_type": "instance",
                    "object_ids": list(pod_names),
                },
            },
            timeout=30,
        )

        if data.get("code") != 0:
            return []

        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return []
        for key in ("events", "items", "list"):
            events = payload.get(key)
            if isinstance(events, list):
                return events
        return []
    except Exception:
        return []
