"""Browser API client for Ray (弹性计算) jobs.

The web UI exposes Ray-cluster job management under ``/api/v1/ray_job/*`` for
users running hybrid CPU-decode / GPU-inference streaming pipelines (what the
UI labels "弹性计算"). Because this endpoint family is web-session only —
there is no OpenAPI equivalent — we hit it the same way the SPA does, with
stored Playwright cookies and a matching ``Referer``.

Create payload shape was reverse-engineered from the SPA's own submit handler
(``/assets/constant.BP_zw-df.js``). Wire surprises worth remembering:
``head_node`` (singular, not ``head``); ``mirror_id`` = internal ``image_id``
(not the Docker URL — resolve via ``image/list`` first); worker side is
``worker_groups[]`` with ``group_name`` / ``min_replicas`` / ``max_replicas``
/ ``quota_id``; command is ``entrypoint`` (form renames it from ``command``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "RayJobInfo",
    "create_ray_job",
    "delete_ray_job",
    "get_ray_job_detail",
    "list_ray_job_events",
    "list_ray_job_instances",
    "list_ray_job_scaling_histories",
    "list_ray_job_users",
    "list_ray_jobs",
    "stop_ray_job",
]


_RAY_JOB_REFERER_PATH = "/jobs/ray"


def _ray_referer() -> str:
    return f"{_get_base_url()}{_RAY_JOB_REFERER_PATH}"


@dataclass
class RayJobInfo:
    """Summary view of a Ray job returned by ``ray_job/list``.

    Field names intentionally mirror the wire format so future additions
    (e.g. elastic scaling metrics) can be surfaced without renames. Fields
    that the API doesn't reliably populate are optional.
    """

    ray_job_id: str
    name: str
    status: str
    workspace_id: str
    project_id: str
    project_name: str
    created_at: str
    finished_at: Optional[str]
    created_by_id: str
    created_by_name: str
    priority: Optional[int]
    raw: dict

    @classmethod
    def from_api_response(cls, data: dict) -> "RayJobInfo":
        created_by = data.get("created_by") or {}
        return cls(
            ray_job_id=str(data.get("ray_job_id") or data.get("id") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            project_id=str(data.get("project_id") or ""),
            project_name=str(data.get("project_name") or ""),
            created_at=str(data.get("created_at") or ""),
            finished_at=data.get("finished_at") or None,
            created_by_id=str(created_by.get("id") or ""),
            created_by_name=str(created_by.get("name") or ""),
            priority=_int_or_none(data.get("priority")),
            raw=data,
        )


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _assert_ok(data: dict, *, context: str) -> dict:
    code = data.get("code")
    if code != 0:
        raise ValueError(
            f"Ray Job {context} failed: code={code} message={data.get('message')}"
        )
    return data


def list_ray_jobs(
    workspace_id: Optional[str] = None,
    *,
    user_ids: Optional[list[str]] = None,
    page_num: int = 1,
    page_size: int = 20,
    session: Optional[WebSession] = None,
) -> tuple[list[RayJobInfo], int]:
    """List Ray (弹性计算) jobs in a workspace.

    ``user_ids`` filters to a specific user or set of users; pass ``None``
    to see every caller's jobs (mirrors the "所有人" tab in the web UI).
    Returns ``(jobs, total)`` where ``total`` is the server-reported match
    count, useful for paging.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
    }
    if user_ids:
        body["filter_by"] = {"user_id": list(user_ids)}

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/list"),
            referer=_ray_referer(),
            body=body,
            timeout=30,
        ),
        context="list",
    )

    payload = data.get("data") or {}
    items = payload.get("items") or payload.get("list") or []
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return [RayJobInfo.from_api_response(item) for item in items], total


def list_ray_job_users(
    workspace_id: Optional[str] = None,
    *,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List users who have created Ray jobs in this workspace.

    Surfaces the dropdown the web UI uses to filter jobs by owner; handy
    for CLI users who want to inspect a teammate's jobs.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/users"),
            referer=_ray_referer(),
            body={"workspace_id": workspace_id},
            timeout=15,
        ),
        context="users",
    )
    payload = data.get("data") or {}
    return payload.get("items") or payload.get("list") or []


def get_ray_job_detail(
    ray_job_id: str,
    *,
    session: Optional[WebSession] = None,
) -> dict:
    """Fetch full details for a single Ray job.

    The web UI consumes this response to render the detail panel (head/worker
    specs, elastic instance ranges, runtime status). We return the raw
    ``data`` payload so callers can pick out whichever nested field they
    need without this wrapper having to keep up with schema churn.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/detail"),
            referer=_ray_referer(),
            body={"ray_job_id": ray_job_id},
            timeout=30,
        ),
        context="detail",
    )
    return data.get("data") or {}


def stop_ray_job(
    ray_job_id: str,
    *,
    session: Optional[WebSession] = None,
) -> None:
    """Stop a running Ray job (does not remove the record)."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/stop"),
            referer=_ray_referer(),
            body={"ray_job_id": ray_job_id},
            timeout=30,
        ),
        context="stop",
    )


def delete_ray_job(
    ray_job_id: str,
    *,
    session: Optional[WebSession] = None,
) -> None:
    """Permanently delete a Ray job record.

    Analogous to ``inspire job delete``: caller should ``stop`` first if
    the job is still running so the scheduler releases reserved capacity
    cleanly.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/delete"),
            referer=_ray_referer(),
            body={"ray_job_id": ray_job_id},
            timeout=30,
        ),
        context="delete",
    )


def create_ray_job(
    body: dict[str, Any],
    *,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Submit a new Ray (弹性计算) job.

    ``body`` is posted verbatim to ``/api/v1/ray_job/create``. Callers are
    expected to assemble the structure the SPA submits — a flat copy of
    the wire contract:

    .. code-block:: json

        {
          "name": "...",
          "description": "...",
          "workspace_id": "ws-...",
          "project_id": "project-...",
          "task_priority": 4,
          "entrypoint": "<driver command>",
          "head_node": {
            "mirror_id": "<image_id>",
            "image_type": "SOURCE_PUBLIC|SOURCE_PRIVATE|SOURCE_OFFICIAL",
            "logic_compute_group_id": "lcg-...",
            "quota_id": "<quota_id>",
            "shm_gi": 64
          },
          "worker_groups": [
            {
              "group_name": "decode",
              "mirror_id": "<image_id>",
              "image_type": "SOURCE_PUBLIC",
              "logic_compute_group_id": "lcg-...",
              "min_replicas": 1,
              "max_replicas": 4,
              "quota_id": "<quota_id>",
              "shm_gi": 32
            }
          ]
        }

    Returns the ``data`` sub-object from the response, which typically
    contains ``ray_job_id`` (plus the platform's ``sub_code`` / ``sub_msg``
    that surface post-validation hints in the web UI).
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a dict")

    if session is None:
        session = get_web_session()

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/create"),
            referer=_ray_referer(),
            body=body,
            timeout=60,
        ),
        context="create",
    )
    return data.get("data") or {}


def list_ray_job_events(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = -1,
    sort_ascending: bool = True,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch job-level events for a Ray cluster.

    Unlike HPC / train_job events (which take a generic
    ``{filter:{object_ids, object_type}, sorter:[...]}`` envelope), Ray's
    events endpoint is bespoke: body is ``{ray_job_id, page_num, page_size,
    sorter}``. No ``object_type`` — passing one returns ``参数错误``.

    Returned events follow the K8s-event shape: ``reason`` / ``type`` /
    ``message`` / ``first_timestamp`` / ``last_timestamp`` / ``count``. The
    critical signals are ``CreatedRayCluster`` (Normal) on submit and
    ``FailedScheduling`` (Warning) when the scheduler can't bind a pod to a
    node — the latter is almost always how you diagnose a job stuck in
    PENDING.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    sort = "ascend" if sort_ascending else "descend"
    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/events/list"),
            referer=_ray_referer(),
            body={
                "ray_job_id": ray_job_id,
                "page_num": page_num,
                "page_size": page_size,
                "sorter": [{"field": "last_timestamp", "sort": sort}],
            },
            timeout=30,
        ),
        context="events",
    )
    payload = data.get("data") or {}
    return payload.get("items") or payload.get("list") or []


def list_ray_job_instances(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = -1,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch the pod-level view of a Ray job (head + worker instances).

    Each entry is a K8s pod-like record: ``instance_id`` / ``instance_type``
    ("head" or "worker") / ``worker_group_name`` / ``status`` ("pending" /
    "running" / ...) / ``cpu_count`` / ``memory_size`` / ``gpu_count`` /
    ``priority`` / ``priority_level`` / ``created_at``. Useful when head is
    up but one worker group is stuck, or to confirm auto-scaling brought
    new pods online.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/instances/list"),
            referer=_ray_referer(),
            body={
                "ray_job_id": ray_job_id,
                "page_num": page_num,
                "page_size": page_size,
            },
            timeout=30,
        ),
        context="instances",
    )
    payload = data.get("data") or {}
    return payload.get("items") or payload.get("list") or []


def list_ray_job_scaling_histories(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
    """Fetch the elastic-scaling event history for a Ray job.

    The SPA hits ``/ray_job/scaling_histories/list`` to render the
    "扩缩容历史" tab on a Ray detail page — each entry is a worker-group
    instance count change driven by platform-side load signals. Useful for
    post-mortem on whether ``min_replicas`` / ``max_replicas`` ever moved.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("ray_job_id is required")

    if session is None:
        session = get_web_session()

    data = _assert_ok(
        _request_json(
            session,
            "POST",
            _browser_api_path("/ray_job/scaling_histories/list"),
            referer=_ray_referer(),
            body={
                "ray_job_id": ray_job_id,
                "page_num": page_num,
                "page_size": page_size,
            },
            timeout=30,
        ),
        context="scaling_histories",
    )
    payload = data.get("data") or {}
    items = payload.get("items") or payload.get("list") or []
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return list(items), total
