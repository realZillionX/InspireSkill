"""Browser API client for Ray (弹性计算) jobs.

The web UI exposes Ray-cluster job management under ``/api/v1/ray_job/*`` for
users running hybrid CPU-decode / GPU-inference streaming pipelines (what the
UI labels "弹性计算"). Because this endpoint family is web-session only —
there is no OpenAPI equivalent — we hit it the same way the SPA does, with
stored Playwright cookies and a matching ``Referer``.

Only read-only + lifecycle operations are exposed here (list / detail / stop /
delete / users). ``ray_job/create`` is intentionally not wrapped: the request
payload is proto-typed with nested ``head`` / ``worker`` specs, shm size,
priority levels, and elastic min/max instance counts per worker group, and
without authoritative schema docs (or a successfully submitted job to copy
from in this workspace) any wrapper risks hiding submission failures. Users
who need ``create`` should use the web UI for now and manage existing jobs
from the CLI.
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
    "delete_ray_job",
    "get_ray_job_detail",
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
