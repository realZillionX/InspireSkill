"""Browser (web-session) APIs for jobs and users.

The web UI exposes job endpoints through the browser session; these helpers
require a web-session cookie.
"""

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
    "JobInfo",
    "create_training_job",
    "delete_job",
    "get_current_user",
    "get_job_detail_v2",
    "list_job_instances",
    "list_job_events",
    "list_job_instance_events",
    "list_train_job_logs",
    "list_jobs",
    "stop_training_job",
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
    cpu_count: int = 0
    memory_gib: int = 0
    shm_gib: Optional[int] = None

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
            cpu_count=framework_config.get("cpu", 0),
            memory_gib=framework_config.get("mem_gi", 0),
            shm_gib=framework_config.get("shm_gi"),
            instance_count=framework_config.get("instance_count", 1),
            priority=data.get("priority", 0),
            workspace_id=data.get("workspace_id", ""),
        )


def create_training_job(
    *,
    payload: dict[str, Any],
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Create a distributed-training job via the current Web UI v2 Action API."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=CreateJobConsole",
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=payload,
        timeout=60,
    )
    return _v2_result(data)


def stop_training_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Stop a distributed-training job via the current Web UI v2 Action API."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=StopJob",
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body={"job_id": job_id},
        timeout=30,
    )
    return _v2_result(data)


def get_job_detail_v2(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch a distributed-training job via the current Web UI v2 Action API."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=GetJob",
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
        body={"job_id": job_id},
        timeout=30,
    )
    return _v2_result(data)


def list_jobs(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[JobInfo], int]:
    """List training jobs using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")
    if created_by is None:
        current_user = get_current_user(session=session)
        created_by = str(current_user.get("id") or current_user.get("user_id") or "").strip()
        if not created_by:
            raise ValueError("Current user could not be resolved for job listing.")

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
        "created_by": created_by,
    }

    if status:
        body["status"] = status
    if keyword:
        body["keyword"] = keyword

    data = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=ListJobs",
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )

    payload = _v2_result(data)
    jobs_data = payload.get("jobs", [])
    total = payload.get("total", 0)

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


def list_job_instances(
    job_id: str,
    *,
    limit: int = 500,
    page_num: int = 1,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
    """Fetch pod-level instances for a distributed-training job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Job selection is required.")
    if limit < 1:
        raise ValueError("limit must be positive")
    if page_num < 1:
        raise ValueError("page_num must be positive")

    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        "/api/v2/train?Action=ListJobInstances",
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
        body={"job_id": job_id, "page_num": page_num, "page_size": limit},
        timeout=30,
    )

    payload = _v2_result(data)
    if not isinstance(payload, dict):
        return [], 0
    items = payload.get("items") or []
    total = payload.get("total") or len(items)
    return (items if isinstance(items, list) else []), int(total)


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
    *,
    page_size: int = 200,
    max_pages: int | None = None,
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
    clean_pods = list(
        dict.fromkeys(
            str(name or "").strip()
            for name in pod_names
            if str(name or "").strip()
        )
    )
    if not clean_pods:
        return []
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    try:
        if session is None:
            session = get_web_session()

        all_events: list[dict] = []
        for offset in range(0, len(clean_pods), 200):
            pod_chunk = clean_pods[offset : offset + 200]
            page_num = 1
            chunk_events: list[dict] = []
            while max_pages is None or page_num <= max_pages:
                data = _request_json(
                    session,
                    "POST",
                    _browser_api_path("/train_job/events/list"),
                    referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{job_id}",
                    body={
                        "page_num": page_num,
                        "page_size": page_size,
                        "filter": {
                            "object_type": "instance",
                            "object_ids": pod_chunk,
                        },
                    },
                    timeout=30,
                )
                if data.get("code") != 0:
                    return []

                payload = data.get("data") if isinstance(data, dict) else None
                if not isinstance(payload, dict):
                    return []
                page_events: list[dict] = []
                for key in ("events", "items", "list"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        page_events = value
                        break
                chunk_events.extend(page_events)

                raw_total = payload.get("total")
                total: int | None
                try:
                    total = int(raw_total) if raw_total is not None else None
                except (TypeError, ValueError):
                    total = None
                if (
                    not page_events
                    or len(page_events) < page_size
                    or (total is not None and len(chunk_events) >= total)
                ):
                    break
                page_num += 1
            all_events.extend(chunk_events)
        return all_events
    except Exception:
        return []


def list_train_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    job_id: str | None = None,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
    """Fetch aggregated train-job logs from the web UI API.

    Endpoint: ``POST /api/v1/logs/train``. The web backend validates
    ``start_timestamp_ms`` and ``end_timestamp_ms`` as string fields, even
    though their values are epoch milliseconds.
    """
    if session is None:
        session = get_web_session()

    clean_pods = [str(name or "").strip() for name in pod_names if str(name or "").strip()]
    body: dict[str, Any] = {
        "page_size": page_size,
        "filter": {
            "podNames": clean_pods,
            "start_timestamp_ms": str(start_timestamp_ms),
            "end_timestamp_ms": str(end_timestamp_ms),
        },
    }
    referer_job_id = str(job_id or "").strip()
    referer = (
        f"{_get_base_url()}/jobs/distributedTrainingDetail/{referer_job_id}"
        if referer_job_id
        else f"{_get_base_url()}/jobs/distributedTraining"
    )

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/logs/train"),
        referer=referer,
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return [], 0
    logs = payload.get("logs") or []
    total = payload.get("total") or len(logs)
    return (logs if isinstance(logs, list) else []), int(total)


def delete_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Permanently delete a training job entry from the platform.

    Endpoint: ``POST /api/v1/train_job/delete`` with body ``{"job_id": <id>}``. The
    exact body key is inferred from the parallel ``/train_job/*`` endpoints
    (``detail``/``workdir`` both use ``job_id``); confirm via
    ``inspire --debug`` on a rejected call. Destructive: the job entry
    disappears from the UI and cannot be recovered — if it is still
    running, ``stop`` first.
    """
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {"job_id": job_id}

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/delete"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}
