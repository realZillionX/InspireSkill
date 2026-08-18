"""Browser (web-session) APIs for HPC jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.batch_query import (
    fetch_events_by_ids,
    fetch_jobs_by_ids,
)
from inspire.platform.web.browser_api.core import (
    _coerce_total,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import (
    SessionExpiredError,
    TransientAPIError,
    WebSession,
    get_web_session,
)

__all__ = [
    "HPC_LOG_MAX_WINDOW_MS",
    "HPCJobInfo",
    "create_hpc_job",
    "delete_hpc_job",
    "get_hpc_job_detail",
    "list_hpc_instance_events",
    "list_hpc_job_events_by_ids",
    "list_hpc_job_instances",
    "list_hpc_jobs",
    "list_hpc_jobs_by_ids",
    "list_hpc_job_events",
    "list_hpc_job_logs",
    "stop_hpc_job",
]

# ``hpc.GetJobLog`` refuses any window wider than one month with
# ``InternalError: 日志查询时间区间不能超过1个月``. ``InternalError`` is on the
# transient list, so an over-wide window does not fail fast: the transport
# burns its three retries first and only then surfaces a message that reads
# like a platform outage. Callers clamp before sending; 31 days is accepted
# and 40 is not, so the ceiling sits one day inside the accepted range.
HPC_LOG_MAX_WINDOW_MS = 30 * 24 * 60 * 60 * 1000


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
            # The wire field is `job_name`; `name` has never been populated, so
            # reading it left every HPC job nameless -- the list rendered N/A
            # and the Name Resolver could not match anything.
            name=data.get("job_name") or data.get("name") or "",
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
    """Create an HPC job via the current Web UI v2 Action API.

    The payload carries the priority as ``priority``. There used to be a retry
    here that stripped ``task_priority`` when the platform complained about an
    unknown ``task`` field; it never fired, because v2 answers the misnamed
    field with ``priority must be set`` rather than an unknown-field error, and
    the retry then sent no priority at all. The field is spelled correctly at
    the call site now, so the retry is gone.
    """
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=CreateJobConsole",
            referer=f"{_get_base_url()}/jobs/hpc",
            body=payload,
            timeout=60,
        )
    )


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
        current_user = _v2_result(
            _request_json(
                session,
                "POST",
                "/api/v2/user?Action=GetUserDetail",
                referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
                body={},
                timeout=30,
            )
        )
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

    # `hpc.ListJobs` reports `total` as a **string** ("202"), so an isinstance
    # check against int silently replaces the real total with the page length.
    # Every caller then concludes it has seen everything after one page.
    total = _coerce_total(payload.get("total"), len(jobs_data))

    jobs = [HPCJobInfo.from_api_response(item) for item in jobs_data if isinstance(item, dict)]
    return jobs, total


def list_hpc_jobs_by_ids(
    job_ids: list[str],
    *,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, dict[str, Any]]:
    """Fetch full HPC job records for many ids at once.

    Action: ``ListJobs`` with ``job_ids``; the batch form of
    :func:`get_hpc_job_detail`. The `hpc` route validates `job_ids` with the
    same rules as `train` -- workspace required, twenty per request -- so see
    :mod:`inspire.platform.web.browser_api.batch_query` for the traps.
    """
    if session is None:
        session = get_web_session()
    return fetch_jobs_by_ids(
        session,
        route="hpc",
        workspace_id=workspace_id,
        job_ids=job_ids,
        referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
    )


def list_hpc_job_events_by_ids(
    job_ids: list[str],
    session: Optional[WebSession] = None,
) -> tuple[dict[str, list[dict]], list[str]]:
    """List platform events for many HPC jobs at once.

    Action: ``ListJobEvents`` with ``filter.object_type="HPC_JOB"``. The batch
    form of :func:`list_hpc_job_events`; returns
    ``({job_id: events}, missing_ids)``.

    ``hpc.ListJobEvents`` wants camelCase paging keys, unlike the `train`
    route -- passing ``page_num`` here silently returns the first page for
    every page asked for.
    """
    if session is None:
        session = get_web_session()
    return fetch_events_by_ids(
        session,
        route="hpc",
        object_type="HPC_JOB",
        object_ids=job_ids,
        referer=f"{_get_base_url()}/jobs/highPerformanceComputing",
        page_key="pageNum",
        page_size_key="pageSize",
        sorter=[{"field": "last_timestamp", "sort": "ascend"}],
    )


def list_hpc_job_events(
    job_id: str,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List platform events for an HPC job.

    Action: ``ListJobEvents``. This wrapper fetches job-level events. Use
    :func:`list_hpc_job_instances` for the component inventory shown on the
    job detail page.

    Returns ``[]`` when the platform answers with nothing: it garbage-collects
    events for completed jobs, so a not-found answer is a normal steady state
    after event retention expires. An expired session or a rate-limited
    platform raises instead, so "no events" is never an invented answer.
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
    except (SessionExpiredError, TransientAPIError):
        raise
    except Exception:
        return []


def list_hpc_instance_events(
    instance_ids: list[str],
    session: Optional[WebSession] = None,
    *,
    job_id: str | None = None,
    page_size: int = 200,
    max_pages: int | None = None,
) -> list[dict]:
    """List per-pod platform events for HPC job instances.

    Action: ``ListSlurmdPodEvent``. It takes exactly one ``instance_id`` per
    request — the namespaced instance name from
    :func:`list_hpc_job_instances` (``<namespace>/<pod>``). The bare pod name
    and the job id both resolve to an empty list rather than an error, so the
    caller must pass the name through unmodified.

    Two paging facts, both measured:

    * ``page_size`` is mandatory in practice. Omitting it returns an empty
      ``events`` list next to a non-zero ``total`` — the shape that reads as
      "this instance has no events" while the platform is saying the opposite.
    * paging itself works, and all three spellings (``PageNumber`` /
      ``page_num`` / ``page``) are honoured. This is the opposite of
      ``hpc.ListJobEvents``, which wants camelCase ``pageNum``.

    Rows are raw Kubernetes event occurrences: ``reason`` / ``message`` /
    ``from`` / ``first_timestamp`` / ``last_timestamp`` / ``age`` /
    ``object_id`` / ``object_type`` (``HPC_JOB_INSTANCE``). There is no
    ``type`` and no ``count`` — the platform repeats an identical row once per
    occurrence instead of aggregating it, so a pod with 20 distinct events can
    answer with 106 rows. Collapsing that is a presentation decision and stays
    in the command layer.

    Best-effort like :func:`list_hpc_job_events`: an expired session or a
    rate-limited platform raises rather than reading as "no events".
    """
    clean_ids = list(
        dict.fromkeys(
            str(value or "").strip() for value in instance_ids if str(value or "").strip()
        )
    )
    if not clean_ids:
        return []
    if page_size < 1 and page_size != -1:
        raise ValueError("page_size must be positive")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")

    detail = f"/jobs/hpcDetail/{job_id}" if job_id else "/jobs/highPerformanceComputing"
    try:
        if session is None:
            session = get_web_session()

        def _instance_events(instance_id: str) -> list[dict]:
            page_num = 1
            instance_events: list[dict] = []
            while max_pages is None or page_num <= max_pages:
                payload = _v2_result(
                    _request_json(
                        session,
                        "POST",
                        "/api/v2/hpc?Action=ListSlurmdPodEvent",
                        referer=f"{_get_base_url()}{detail}",
                        body={
                            "instance_id": instance_id,
                            "page_size": page_size,
                            "PageNumber": page_num,
                        },
                        timeout=30,
                    )
                )
                page_events: list[dict] = []
                for key in ("events", "items", "list"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        page_events = value
                        break
                instance_events.extend(item for item in page_events if isinstance(item, dict))

                # `total` arrives as a string ("106") on this Action.
                total = _coerce_total(payload.get("total"), -1)
                if (
                    page_size == -1
                    or not page_events
                    or len(page_events) < page_size
                    or (total >= 0 and len(instance_events) >= total)
                ):
                    break
                page_num += 1
            return instance_events

        # This Action takes exactly one instance per request, so a job's whole
        # event history costs one round trip per pod — and reading every pod is
        # now the default. Sequentially that is ~0.3s × node count on the
        # command's critical path; the pool keeps a wide job answering in about
        # the time a narrow one takes. Results are reassembled in input order,
        # not completion order.
        if len(clean_ids) == 1:
            return _instance_events(clean_ids[0])
        collected: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=min(len(clean_ids), 8)) as pool:
            futures = {
                pool.submit(_instance_events, instance_id): instance_id
                for instance_id in clean_ids
            }
            for future in as_completed(futures):
                collected[futures[future]] = future.result()
        return [event for instance_id in clean_ids for event in collected.get(instance_id, [])]
    except (SessionExpiredError, TransientAPIError):
        raise
    except Exception:
        return []


def list_hpc_job_instances(
    job_id: str,
    *,
    limit: int = 500,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List pod/component instances for an HPC job.

    Action: ``ListJobInstances``, with body ``{jobId, page_num, page_size}`` --
    this one keeps the camelCase ``jobId``, unlike its neighbours.
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
    rows = [item for item in items if isinstance(item, dict)]
    return rows, _coerce_total(payload.get("total"), len(rows))


def list_hpc_job_logs(
    *,
    pod_names: list[str],
    start_timestamp_ms: int | str,
    end_timestamp_ms: int | str,
    page_size: int = 200,
    job_id: str | None = None,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch aggregated HPC logs for one job's pods.

    Action: ``GetJobLog``. Four measured constraints shape the call:

    * **The sorter is all-or-nothing.** The only accepted value is the console's
      own pair, ``[{"field": "@timestamp"}, {"field": "log-id.keyword"}]``;
      either field on its own answers ``InternalError: 日志排序字段不合法，仅支持
      按时间 + log-id 排序``. Omitting it entirely is accepted too, and that is
      what this wrapper does — rows then arrive newest-last in practice with
      nothing guaranteeing it, so sort client-side.
    * **The two timestamps are string fields carrying epoch milliseconds**, the
      window may not exceed a month (:data:`HPC_LOG_MAX_WINDOW_MS`), and start
      must be older than end — an inverted pair is
      ``InternalError: 日志查询时间参数不合法``.
    * **``podNames`` wants the namespaced instance names** from
      :func:`list_hpc_job_instances`. Bare pod names come back as
      ``InvalidParameter: Invalid instance names …`` because the platform
      resolves them to exactly one HPC job id, and they resolve to none.
    * **``page_size`` is the only lever, and ``-1`` is not "everything" here.**
      Omitting it or sending ``-1`` both cap the answer at 100 rows while
      ``total`` reports the real count; ``PageNumber`` is ignored outright. To
      read past 100 rows, re-request with ``page_size`` at least ``total``.
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
    rows = [item for item in logs if isinstance(item, dict)]
    return rows, _coerce_total(payload.get("total"), len(rows))


def delete_hpc_job(
    job_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Permanently delete an HPC job entry from the platform.

    Action: ``DeleteJob``.

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
