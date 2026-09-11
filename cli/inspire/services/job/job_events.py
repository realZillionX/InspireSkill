"""Event filtering, instance selection and collection shared by CLI and SDK."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Callable
from inspire.services.utils.raw_ids import scrub_raw_ids

_JOB_INSTANCE_PAGE_SIZE = 200


def event_sort_key(event: dict) -> tuple[int, int, int]:
    """Order a merged event stream oldest-first.

    Controller-level and per-pod events come from different calls (and, on
    HPC, one call per instance), so the chronology that makes ``--tail`` mean
    "most recent" has to be imposed here rather than trusted from the
    platform's own ordering.

    Timestamps are per-second, so a container's `Pulled` / `Created` /
    `Started` trio usually shares one — hence the ``id`` tiebreaker, which Ray
    fills with a monotonic counter. Without it the causal order of a same-second
    burst flips depending on how the rows were fetched.
    """

    def _epoch(value: object) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 0

    return (
        _epoch(event.get("last_timestamp")),
        _epoch(event.get("first_timestamp")),
        _epoch(event.get("id")),
    )


def event_type(event: dict) -> str:
    """Read the Normal / Warning field under either spelling.

    Node events call it ``event_type``; every workload Action calls it
    ``type``. The difference has to be absorbed in one place, or `--type
    Warning` silently empties the node stream instead of filtering it.
    """
    return str(event.get("type") or event.get("event_type") or "")


def matching_events(
    events: list[dict],
    *,
    type_filter: Optional[str] = None,
    reason_filter: Optional[str] = None,
    keyword_filter: Optional[str] = None,
) -> list[dict]:
    """Apply event filters without imposing an output window."""
    out = events
    if type_filter:
        needle = type_filter.lower()
        out = [e for e in out if event_type(e).lower() == needle]
    if reason_filter:
        needle = reason_filter.lower()
        out = [e for e in out if needle in str(e.get("reason", "")).lower()]
    if keyword_filter:
        needle = keyword_filter.lower()
        out = [
            e
            for e in out
            if needle in event_type(e).lower()
            or any(
                needle in str(e.get(key, "")).lower()
                for key in ("reason", "message", "from", "content")
            )
        ]
    return out


class JobInstanceSelectionError(ValueError):
    """A `--instance` selector matched no instance in the job."""


@dataclass
class JobInstanceView:
    """One instance, split into what the Agent sees and what the API needs."""

    handle: str
    label: str
    role: str


def job_instance_views(
    instances: Sequence[dict[str, Any]], project: Callable[[list[dict]], list[dict]] | None = None
) -> list[JobInstanceView]:
    """Project instance rows onto the (label, handle) pairs commands address.

    The label is exactly what `inspire job instances` prints in its Name
    column: the platform's own name when it survives the output boundary, and
    ``rank=N`` when it does not.
    """
    project = project or instance_labels
    views: list[JobInstanceView] = []
    rows = list(instances)
    for public, raw in zip(project(rows), rows):
        handle = str(raw.get("name") or "").strip()
        if not handle:
            continue
        label = str(public.get("name") or "").strip()
        if not label:
            rank = public.get("rank")
            if rank is None:
                continue
            label = f"rank={rank}"
        views.append(
            JobInstanceView(
                handle=handle,
                label=label,
                role=str(public.get("role") or public.get("type") or "").strip(),
            )
        )
    return views


def select_job_instance_views(
    views: list[JobInstanceView],
    selectors: Sequence[str],
) -> list[JobInstanceView]:
    """Resolve `--instance` selectors, or fail instead of silently widening.

    ``rank=0`` and a bare ``0`` both address the same instance — the first is
    what the instance table prints, the second is what a person types. A role
    name (``worker``) selects every instance in that role, the way `hpc
    instances` roles do.
    """
    if not selectors:
        return list(views)

    selected: list[JobInstanceView] = []
    seen: set[str] = set()
    for selector in selectors:
        needle = str(selector or "").strip().lower()
        if not needle:
            continue
        bare_rank = f"rank={needle}" if needle.isdigit() else ""
        matches = [
            view
            for view in views
            if view.label.lower() == needle
            or (bare_rank and view.label.lower() == bare_rank)
            or (view.role and view.role.lower() == needle)
        ]
        if not matches:
            known = ", ".join(view.label for view in views) or "none"
            raise JobInstanceSelectionError(
                f"No job instance matches {selector!r}. Known instances: {known}. "
                "List them with `inspire job instances <job-name> --workspace <workspace>`."
            )
        for view in matches:
            if view.handle not in seen:
                seen.add(view.handle)
                selected.append(view)
    return selected


def labelled_instance_events(
    events: list[dict],
    views: list[JobInstanceView],
) -> list[dict]:
    """Name each per-pod row with the instance it belongs to.

    Every pod's events land in one timeline, and the only field that says
    which pod a row came from is ``object_id`` — the handle, which the shared
    public projection drops. Attaching the label here is what makes "which
    worker failed to schedule" answerable from the output rather than from a
    second query. A pod the instance list does not know keeps no label rather
    than falling back to its handle.
    """
    labels = {view.handle: view.label for view in views}
    labelled: list[dict] = []
    for event in events:
        row = dict(event)
        label = labels.get(str(row.get("object_id") or "").strip())
        if label:
            row["instance"] = label
        labelled.append(row)
    return labelled


def list_all_job_instances(
    job_id: str, *, session: Any, fetch: Callable[..., tuple[list[dict], int]] | None = None
) -> list[dict]:  # noqa: ANN001
    """Page through every instance or fail instead of returning a partial scope."""
    if fetch is None:
        from inspire.platform.web import browser_api

        fetch = browser_api.list_job_instances
    rows: list[dict] = []
    seen: set[str] = set()
    page_num = 1
    while True:
        instances, total = fetch(
            job_id,
            limit=_JOB_INSTANCE_PAGE_SIZE,
            page_num=page_num,
            session=session,
        )
        added = 0
        for item in instances:
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                rows.append(item)
                added += 1
        if not instances or added == 0:
            if len(rows) >= total:
                return rows
            raise RuntimeError("Could not retrieve the complete job instance list.")
        if len(instances) < _JOB_INSTANCE_PAGE_SIZE:
            if len(rows) >= total:
                return rows
            raise RuntimeError("Could not retrieve the complete job instance list.")
        page_num += 1


def instance_labels(rows: list[dict]) -> list[dict]:
    result = []
    for position, row in enumerate(rows):
        name = scrub_raw_ids(str(row.get("name") or ""))
        rank = next(
            (
                row[k]
                for k in ("rank", "instance_rank", "global_rank", "index", "replica_index")
                if str(row.get(k, "")).isdigit()
            ),
            position,
        )
        result.append(
            dict(
                name="" if "<redacted>" in name else name,
                rank=rank,
                role=row.get("role") or row.get("component") or row.get("worker_group_name"),
                type=row.get("type") or row.get("instance_type"),
            )
        )
    return result


def collect_job_events(
    job_id: str,
    *,
    session: Any,
    workload_level: bool = False,
    conflict_message: str = "--workload-level and --instance cannot be used together.",
    instance: Sequence[str] = (),
    list_instances: Callable[..., list[dict]] | None = None,
    views_factory: Callable[..., list[JobInstanceView]] | None = None,
    workload_events: Callable[..., list[dict]] | None = None,
    instance_events: Callable[..., list[dict]] | None = None,
) -> list[dict]:
    from inspire.platform.web.browser_api import jobs

    if workload_level and instance:
        raise ValueError(conflict_message)
    workload_events = workload_events or jobs.list_job_events
    instance_events = instance_events or jobs.list_job_instance_events
    if workload_level:
        return sorted(workload_events(job_id, session=session), key=event_sort_key)
    list_instances = list_instances or list_all_job_instances
    views_factory = views_factory or job_instance_views
    views = select_job_instance_views(
        views_factory(list_instances(job_id, session=session)), instance
    )
    rows = labelled_instance_events(
        instance_events(job_id, [v.handle for v in views], session=session), views
    )
    if not instance:
        rows = workload_events(job_id, session=session) + rows
    return sorted(rows, key=event_sort_key)
