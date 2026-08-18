"""Batch read paths shared by the `train` and `hpc` services.

Both services expose the same two batch reads, with the same server-side
limits and the same two traps, so the request loops live here once and the
domain modules keep only the route/referer/spelling differences.

## What batches and what does not

`ListJobs` takes `job_ids` and `ListJobEvents` takes `filter.object_ids`.
`GetTaskMetricBatch` exists on both routes and is **not** used: measured
against the singular `GetTaskMetric` over the same window, `disk_io_read` and
`disk_io_write` come back with zero samples where the singular answers 61,
both `network_tcp_ip_io_*` types answer `InternalError`, and every group drops
its `group_name` so the per-pod split is gone. Metrics stay fanned out; see
`metrics.get_resource_metrics_by_time`.

## The two traps

**The id cap counts the list, not the set.** Twenty unique ids plus one repeat
is twenty-one items and the platform rejects it, so ids are deduplicated
before they are chunked, never after.

**The two Actions disagree about an id they cannot find.** `ListJobs` drops it
silently — the answer is a shorter list, and only the caller diffing against
what it asked for can tell a deleted job from one it never named. Meanwhile
`ListJobEvents` fails the *whole* request with
`InvalidParameter: job <id> not found`, so one garbage-collected job takes the
other nineteen down with it. `fetch_events_by_ids` recovers from that instead
of returning the empty list that would read as "these jobs have no events".
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inspire.platform.web.browser_api.core import _request_json, _v2_result
from inspire.platform.web.session import WebSession

__all__ = [
    "BATCH_ID_LIMIT",
    "dedupe_ids",
    "fetch_events_by_ids",
    "fetch_jobs_by_ids",
]

#: Server-side ceiling on `job_ids` and on `filter.object_ids`, on both routes:
#: `job_ids count exceeds limit 20` / `object_ids count exceeds limit 20`.
#: It does not apply to `object_type="instance"`, which accepts 500 in one
#: request -- `list_job_instance_events` chunks at 200 and is right to.
BATCH_ID_LIMIT = 20

#: Events are returned page by page with an exact `total`. 200 is the page size
#: the console uses and the largest that is honoured without argument.
_EVENT_PAGE_SIZE = 200

#: A batch of 20 jobs can carry several hundred events; this only exists so a
#: platform that stops advancing `total` cannot spin here forever.
_MAX_EVENT_PAGES = 200


def dedupe_ids(values: Iterable[str]) -> list[str]:
    """Strip, drop blanks, and remove repeats while keeping the caller's order.

    Order is kept because it is the order the caller will print. It is not the
    order the platform answers in: `ListJobs` returns its own, so callers index
    the result by id rather than zipping it against the request.
    """
    return list(
        dict.fromkeys(
            str(value or "").strip() for value in values if str(value or "").strip()
        )
    )


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), BATCH_ID_LIMIT):
        yield values[start : start + BATCH_ID_LIMIT]


def fetch_jobs_by_ids(
    session: WebSession,
    *,
    route: str,
    workspace_id: str,
    job_ids: Iterable[str],
    referer: str,
    timeout: int = 30,
) -> dict[str, dict[str, Any]]:
    """Fetch full job records for `job_ids`, twenty per request.

    Returns `{job_id: record}`. The records are the same objects the detail
    Action returns -- `train.ListJobs` filtered by `job_ids` and
    `train.GetJob` answer field-for-field identical payloads (checked against
    running, stopped and failed jobs) -- so this replaces a `GetJob` fan-out
    rather than approximating one.

    Ids the platform does not know are **missing from the mapping**, and so are
    ids that live in another workspace: `workspace_id` really scopes the query,
    and asking the wrong workspace for a real job returns nothing at all rather
    than an error. Callers must diff the keys against what they asked for and
    report the difference as "not found in this workspace", never as an empty
    record.

    `workspace_id` is mandatory -- the platform answers
    `workspace_id is required when job_ids is set` -- and an empty `job_ids`
    returns `{}` without a request, because a `job_ids: []` body falls through
    to the paging path and fails there with an unrelated message about page
    size.
    """
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required to look jobs up by id.")

    wanted = dedupe_ids(job_ids)
    found: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(wanted):
        payload = _v2_result(
            _request_json(
                session,
                "POST",
                f"/api/v2/{route}?Action=ListJobs",
                referer=referer,
                body={"workspace_id": workspace_id, "job_ids": chunk},
                timeout=timeout,
            )
        )
        rows = payload.get("jobs")
        if not isinstance(rows, list):
            rows = payload.get("items")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("job_id") or row.get("id") or "").strip()
            if job_id:
                found[job_id] = row
    return found


def _event_rows(
    session: WebSession,
    *,
    route: str,
    object_type: str,
    object_ids: list[str],
    referer: str,
    page_key: str,
    page_size_key: str,
    sorter: Optional[list[dict[str, str]]],
    timeout: int,
) -> list[dict[str, Any]]:
    """Page through one chunk's events. Raises whatever the platform raises."""
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= _MAX_EVENT_PAGES:
        body: dict[str, Any] = {
            page_key: page,
            page_size_key: _EVENT_PAGE_SIZE,
            "filter": {"object_type": object_type, "object_ids": object_ids},
        }
        if sorter:
            body["sorter"] = sorter
        payload = _v2_result(
            _request_json(
                session,
                "POST",
                f"/api/v2/{route}?Action=ListJobEvents",
                referer=referer,
                body=body,
                timeout=timeout,
            )
        )
        page_rows: list[Any] = []
        for key in ("events", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                page_rows = value
                break
        rows.extend(row for row in page_rows if isinstance(row, dict))

        raw_total = payload.get("total")
        try:
            total = int(raw_total) if raw_total is not None else None
        except (TypeError, ValueError):
            total = None
        if not page_rows or (total is not None and len(rows) >= total):
            break
        page += 1
    return rows


def _named_ids(message: str, candidates: list[str]) -> list[str]:
    """Which of the ids we sent does this error message name?

    Matching against ids we already hold, rather than parsing the id out of the
    sentence, keeps this working if the platform rewords the message.
    """
    return [value for value in candidates if value and value in message]


def fetch_events_by_ids(
    session: WebSession,
    *,
    route: str,
    object_type: str,
    object_ids: Iterable[str],
    referer: str,
    page_key: str = "page_num",
    page_size_key: str = "page_size",
    sorter: Optional[list[dict[str, str]]] = None,
    timeout: int = 30,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Fetch events for many jobs at once, twenty per request.

    Returns `({job_id: events}, missing_ids)`. Every id asked for is a key, so
    a job with no events is an empty list and is not confused with one the
    platform has never heard of; those are collected in `missing_ids`.

    Batching is exact rather than approximate: the events for N ids in one
    request are the concatenation of the N single-id answers, and every row
    carries the `object_id` it belongs to, which is what makes them separable
    again here.

    A single unknown id fails the whole chunk, so a failure is retried without
    the ids the error names. The platform reports one at a time, so a chunk
    with several dead jobs is retried several times; a message that names none
    of them falls back to one request per id, which bounds the recovery at the
    chunk size either way. Any other failure -- an expired session, a throttled
    platform -- propagates, because "no events" is a fact schedules get read
    out of and must never be manufactured.
    """
    wanted = dedupe_ids(object_ids)
    events: dict[str, list[dict[str, Any]]] = {value: [] for value in wanted}
    missing: list[str] = []

    def _distribute(rows: list[dict[str, Any]], expected: list[str]) -> None:
        for row in rows:
            object_id = str(row.get("object_id") or "").strip()
            # An id that is not one we asked for would be a platform bug; drop
            # it rather than inventing a key no caller can account for.
            if object_id in events:
                events[object_id].append(row)
            elif len(expected) == 1:
                # Single-id requests are the one case where a row may legally
                # arrive without an `object_id` to key on.
                events[expected[0]].append(row)

    for chunk in _chunks(wanted):
        pending = list(chunk)
        while pending:
            try:
                rows = _event_rows(
                    session,
                    route=route,
                    object_type=object_type,
                    object_ids=pending,
                    referer=referer,
                    page_key=page_key,
                    page_size_key=page_size_key,
                    sorter=sorter,
                    timeout=timeout,
                )
            except ValueError as exc:
                message = str(exc)
                if "not found" not in message.lower():
                    raise
                named = _named_ids(message, pending)
                if not named:
                    # The platform did not say which id it choked on. Ask one
                    # at a time so the dead ones are identified exactly.
                    for one in pending:
                        try:
                            rows = _event_rows(
                                session,
                                route=route,
                                object_type=object_type,
                                object_ids=[one],
                                referer=referer,
                                page_key=page_key,
                                page_size_key=page_size_key,
                                sorter=sorter,
                                timeout=timeout,
                            )
                        except ValueError as inner:
                            if "not found" not in str(inner).lower():
                                raise
                            missing.append(one)
                            continue
                        _distribute(rows, [one])
                    break
                missing.extend(named)
                pending = [value for value in pending if value not in set(named)]
                continue
            _distribute(rows, pending)
            break

    for value in missing:
        events.pop(value, None)
    return events, missing
