"""Browser API client for Ray (弹性计算) jobs.

The web UI exposes Ray-cluster job management under ``/api/v2/ray`` for users
running hybrid CPU-decode / GPU-inference streaming pipelines (what the UI
labels "弹性计算"). This route is web-session only, so we hit it the same way
the SPA does, with stored Playwright cookies and a matching ``Referer``.

Every Action here was verified against a live job before being wired up; the
v2 responses are field-for-field identical to the ``/api/v1/ray_job/*`` ones
they replace, so the normalization below is unchanged from the v1 wrapper.
Wire details that differ from sibling domains:

- The resource key is ``ray_job_id`` on every Action *except* ``GetJobLog``.
  ``job_id`` and ``id`` are both rejected with ``unknown field``, unlike
  ``train`` / ``hpc``. ``GetJobLog`` is the lone inversion — see
  :func:`list_ray_job_logs`.
- **Field-existence probes lie on this route when ``ray_job_id`` is set.** The
  gateway's authorization middleware leniently pre-reads ``ray_job_id`` and
  answers ``ResourceNotFound: ray job not found`` *before* the strict proto
  unmarshal runs, so a nonexistent id masks every ``unknown field`` the body
  would otherwise report. Probe ``ray`` field contracts with the id omitted.
- Workspace scoping is a **top-level** ``workspace_id``. The nested ``filter``
  envelope that ``workspace.*`` Actions require is rejected here.
- There is no ``CreateJobConsole`` variant — ``ray`` answers ``InvalidAction``
  for it, so creation goes through plain ``CreateJob``.
- ``UpdateJob`` exists but is metadata only: it accepts ``ray_job_id`` / ``name``
  / ``description`` and rejects ``worker_groups``, ``head_node``,
  ``entrypoint``, ``min_replicas``, ``replicas``, ``task_priority`` and
  ``project_id`` with ``unknown field``. It is not the elastic worker-count
  lever, so it stays unwrapped.

Create payload shape was reverse-engineered from the SPA's own submit handler
(``/assets/constant.BP_zw-df.js``) and is accepted verbatim by v2. Wire
surprises worth remembering: ``head_node`` (singular, not ``head``);
``mirror_id`` = internal ``image_id`` (not the Docker URL — resolve via
``image/list`` first); worker side is ``worker_groups[]`` with ``group_name``
/ ``min_replicas`` / ``max_replicas`` / ``quota_id``; command is
``entrypoint`` (form renames it from ``command``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import WebSession, get_web_session

__all__ = [
    "RAY_LOG_MAX_WINDOW_MS",
    "RayJobInfo",
    "create_ray_job",
    "delete_ray_job",
    "get_ray_job_detail",
    "list_ray_job_events",
    "list_ray_job_instances",
    "list_ray_job_logs",
    "list_ray_job_scaling_histories",
    "list_ray_job_users",
    "list_ray_jobs",
    "start_ray_job",
    "stop_ray_job",
]


_RAY_JOB_REFERER_PATH = "/jobs/ray"

# The sibling log Actions on this backend (`train` / `hpc` `GetJobLog`) refuse
# any window wider than a month. `ray` cannot be probed for it — instance-name
# resolution runs first and rejects every synthetic pod name — so callers clamp
# defensively rather than discover the ceiling from a live failure.
RAY_LOG_MAX_WINDOW_MS = 30 * 24 * 60 * 60 * 1000


def _ray_referer() -> str:
    return f"{_get_base_url()}{_RAY_JOB_REFERER_PATH}"


@dataclass
class RayJobInfo:
    """Summary view of a Ray job returned by ``ListJobs``.

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
        # The wire fields are `creator` and `priority_name`. `created_by` and
        # `priority` are always null, so reading only those left every job
        # ownerless (the list printed N/A) and every priority None.
        created_by = data.get("creator") or data.get("created_by") or {}
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
            priority=_int_or_none(
                data.get("priority") if data.get("priority") is not None
                else data.get("priority_name")
            ),
            raw=data,
        )


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ray_v2(
    session: WebSession,
    action: str,
    body: dict[str, Any],
    *,
    context: str,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call one `/api/v2/ray` Action and return its unwrapped ``Result``.

    Keeps the ``Ray Job <context> failed`` message shape the v1 wrapper used so
    command-layer error text is unchanged.
    """
    data = _request_json(
        session,
        "POST",
        f"/api/v2/ray?Action={action}",
        referer=_ray_referer(),
        body=body,
        timeout=timeout,
    )
    try:
        return _v2_result(data)
    except ValueError as exc:
        raise ValueError(f"Ray Job {context} failed: {exc}") from exc


def _ray_page(
    payload: dict[str, Any],
    list_key: str = "items",
) -> tuple[list[dict], Optional[int]]:
    """Split a paged ray Result into its item list and total.

    ``ray`` reports ``total`` as a string ("3"), so it is coerced here rather
    than at each call site. The list key is per-Action and never inferred:
    every Action on this route answers ``items`` except ``GetJobLog``, which
    answers ``logs``.
    """
    items = payload.get(list_key)
    if not isinstance(items, list):
        items = []
    items = [item for item in items if isinstance(item, dict)]

    raw_total = payload.get("total")
    if raw_total is None:
        return items, None
    try:
        return items, int(str(raw_total))
    except (TypeError, ValueError):
        return items, None


def list_ray_jobs(
    workspace_id: Optional[str] = None,
    *,
    user_ids: Optional[list[str]] = None,
    page_num: int = 1,
    page_size: int = 20,
    session: Optional[WebSession] = None,
) -> tuple[list[RayJobInfo], int]:
    """List Ray (弹性计算) jobs in a workspace.

    Returns ``(jobs, total)`` where ``total`` is the server-reported match
    count, useful for paging.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")
    if not user_ids:
        current_user = _v2_result(
            _request_json(
                session,
                "POST",
                "/api/v2/user?Action=GetUserDetail",
                referer=_ray_referer(),
                body={},
                timeout=30,
            )
        )
        current_user_id = str(
            current_user.get("id") or current_user.get("user_id") or ""
        ).strip()
        if not current_user_id:
            raise ValueError("Current user could not be resolved for Ray listing.")
        user_ids = [current_user_id]

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
        "filter_by": {"user_id": list(user_ids)},
    }

    payload = _ray_v2(session, "ListJobs", body, context="list")
    items, total = _ray_page(payload)
    return [RayJobInfo.from_api_response(item) for item in items], total or 0


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
        raise ValueError("Workspace selection is required.")

    payload = _ray_v2(
        session,
        "ListJobCreators",
        {"workspace_id": workspace_id},
        context="users",
        timeout=15,
    )
    items, _ = _ray_page(payload)
    return items


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
        raise ValueError("Ray job selection is required.")

    if session is None:
        session = get_web_session()

    return _ray_v2(
        session, "GetJob", {"ray_job_id": ray_job_id}, context="detail"
    )


def start_ray_job(
    ray_job_id: str,
    *,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Restart a stopped Ray job from its stored configuration.

    The counterpart to :func:`stop_ray_job`: the platform keeps the head and
    worker-group spec on the record, so restarting needs nothing but the id.
    A job that is already RUNNING answers ``Conflict``; the returned payload is
    the refreshed ``ray_job`` object.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("Ray job selection is required.")

    if session is None:
        session = get_web_session()

    return _ray_v2(session, "StartJob", {"ray_job_id": ray_job_id}, context="start")


def stop_ray_job(
    ray_job_id: str,
    *,
    session: Optional[WebSession] = None,
) -> None:
    """Stop a running Ray job (does not remove the record)."""
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("Ray job selection is required.")

    if session is None:
        session = get_web_session()

    _ray_v2(session, "StopJob", {"ray_job_id": ray_job_id}, context="stop")


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
        raise ValueError("Ray job selection is required.")

    if session is None:
        session = get_web_session()

    _ray_v2(session, "DeleteJob", {"ray_job_id": ray_job_id}, context="delete")


def create_ray_job(
    body: dict[str, Any],
    *,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Submit a new Ray (弹性计算) job.

    ``body`` is posted verbatim to ``ray?Action=CreateJob``. Callers are
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

    return _ray_v2(session, "CreateJob", body, context="create", timeout=60)


def list_ray_job_events(
    ray_job_id: str,
    *,
    page_num: int = 1,
    page_size: int = 200,
    max_pages: int = 1,
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
        raise ValueError("Ray job selection is required.")
    if page_num < 1:
        raise ValueError("page_num must be positive")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    if session is None:
        session = get_web_session()

    sort = "ascend" if sort_ascending else "descend"
    events: list[dict] = []
    for current_page in range(page_num, page_num + max_pages):
        payload = _ray_v2(
            session,
            "ListJobEvents",
            {
                "ray_job_id": ray_job_id,
                "page_num": current_page,
                "page_size": page_size,
                "sorter": [{"field": "last_timestamp", "sort": sort}],
            },
            context="events",
        )
        page_events, total = _ray_page(payload)
        events.extend(page_events)
        if (
            not page_events
            or len(page_events) < page_size
            or (total is not None and len(events) >= total)
        ):
            break
    return events


def list_ray_job_instances(
    ray_job_id: str,
    *,
    limit: int = 500,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
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
        raise ValueError("Ray job selection is required.")
    if limit < 1:
        raise ValueError("limit must be positive")

    if session is None:
        session = get_web_session()

    payload = _ray_v2(
        session,
        "ListJobInstances",
        {
            "ray_job_id": ray_job_id,
            "page_num": 1,
            "page_size": limit,
        },
        context="instances",
    )
    items, total = _ray_page(payload)
    return items, total if total is not None else len(items)


def list_ray_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
    """Fetch aggregated program output for a Ray cluster's pods.

    Action: ``GetJobLog``. This is the one Action on ``/api/v2/ray`` that does
    **not** take ``ray_job_id`` — the field is not in its contract at all
    (``unknown field "ray_job_id"``), which inverts the rule every sibling
    Action follows. Its declared id key is ``job_id``, exactly as ``discovery``
    says, but that key does not scope anything: sent alone the Action answers
    ``InternalError``, and sent alongside pod names it neither narrows nor
    resolves the query. The console omits it for ``ray`` and so do we.

    **The query is scoped by ``filter.podNames``**, which the backend resolves
    back to exactly one Ray job — a mixed list answers ``InvalidParameter:
    Invalid instance names, the ray job ids length of instances expect 1``.
    That resolution is also where the permission check lands, so an empty pod
    list is not a harmless no-op: the platform answers a clean
    ``{"logs": [], "total": 0}`` for it, which reads exactly like "this job
    produced no output". Callers therefore never get to make that mistake —
    an empty ``pod_names`` raises here instead.

    ``start_timestamp_ms`` / ``end_timestamp_ms`` are string fields carrying
    epoch milliseconds (an int is rejected with ``invalid value for string
    field endTimestampMs``), matching ``train`` / ``hpc``. No sorter is sent;
    records are ordered client-side.
    """
    clean_pods = [str(name or "").strip() for name in pod_names if str(name or "").strip()]
    if not clean_pods:
        raise ValueError(
            "Ray job log selection is required: at least one instance name."
        )

    if session is None:
        session = get_web_session()

    payload = _ray_v2(
        session,
        "GetJobLog",
        {
            "page_size": page_size,
            "filter": {
                "podNames": clean_pods,
                "start_timestamp_ms": str(start_timestamp_ms),
                "end_timestamp_ms": str(end_timestamp_ms),
            },
        },
        context="logs",
    )
    logs, total = _ray_page(payload, "logs")
    return logs, total if total is not None else len(logs)


def list_ray_job_scaling_histories(
    ray_job_id: str,
    *,
    worker_group_name: str = "",
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], int]:
    """Fetch the elastic-scaling event history for a Ray job.

    The SPA hits ``ListJobScalingHistories`` to render the "扩缩容历史"
    popover on a Ray detail page — each entry is a worker-group replica-count
    change driven by platform-side load signals. This is the only view of
    whether the elastic range a job was submitted with (``min_replicas`` /
    ``max_replicas``) was ever exercised.

    Entries carry ``event_time`` (epoch milliseconds), ``event_type``
    (``initialized`` / ``scale_up`` / ``scale_down``) and the
    ``replicas_before`` / ``replicas_after`` pair.

    ``worker_group_name`` is a **top-level** filter, not a nested ``filter``
    envelope — this Action declares neither ``filter`` nor ``sorter``. The
    console always sends it (empty string when unfiltered), and empty means
    every group.
    """
    ray_job_id = str(ray_job_id or "").strip()
    if not ray_job_id:
        raise ValueError("Ray job selection is required.")

    if session is None:
        session = get_web_session()

    payload = _ray_v2(
        session,
        "ListJobScalingHistories",
        {
            "ray_job_id": ray_job_id,
            "page_num": page_num,
            "page_size": page_size,
            "worker_group_name": str(worker_group_name or "").strip(),
        },
        context="scaling_histories",
    )
    items, raw_total = _ray_page(payload)
    total = raw_total or 0
    return list(items), total
