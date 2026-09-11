"""Ray event collection shared by CLI and SDK."""

from __future__ import annotations
from typing import Sequence
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.job.job_events import event_sort_key
from inspire.services.ray.ray_instances import (
    RayInstanceView,
    ray_instance_views,
    select_ray_instance_views,
)

_RAY_EVENT_PAGE_SIZE = 200
_RAY_EVENT_MAX_PAGES = 5
_DEFAULT_INSTANCE_SCAN_LIMIT = 500


def labelled_ray_events(
    events: list[dict],
    views: Sequence[RayInstanceView],
) -> list[dict]:
    """Name each pod row with the identity `inspire ray instances` prints.

    One call returns controller rows and pod rows in the same list, told apart
    only by ``object_type`` / ``object_id`` — and ``object_id`` is the pod
    handle, which never reaches output. Controller rows keep no label: they
    are about the cluster, not about any one pod.
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


def fetch_recent_ray_events(
    ray_job_id: str,
    *,
    session,  # noqa: ANN001
    selectors: Sequence[str] = (),
    workload_level: bool = False,
) -> list[dict]:
    """Fetch a bounded newest-first window and restore chronological output.

    The cluster level is a client-side split, not a second call: one
    ``ListJobEvents`` already returns both, told apart by ``object_type``.
    """
    if workload_level:
        events = browser_api_module.list_ray_job_events(
            ray_job_id,
            page_size=_RAY_EVENT_PAGE_SIZE,
            max_pages=_RAY_EVENT_MAX_PAGES,
            sort_ascending=False,
            session=session,
        )
        cluster_rows = [
            event
            for event in events
            if str(event.get("object_type") or "").strip().lower() != "instance"
        ]
        return sorted(cluster_rows, key=event_sort_key)
    instances, _total = browser_api_module.list_ray_job_instances(
        ray_job_id,
        limit=_DEFAULT_INSTANCE_SCAN_LIMIT,
        session=session,
    )
    views = ray_instance_views(instances)
    pod_names = None
    if selectors:
        views = select_ray_instance_views(views, selectors)
        pod_names = [view.handle for view in views]
    events = browser_api_module.list_ray_job_events(
        ray_job_id,
        pod_names=pod_names,
        page_size=_RAY_EVENT_PAGE_SIZE,
        max_pages=_RAY_EVENT_MAX_PAGES,
        sort_ascending=False,
        session=session,
    )
    # Fetched newest-first to bound the window, then restored to chronological
    # order here rather than by reversing: same-second ties come back in an
    # order that depends on the filter, and reversing would flip them.
    return sorted(labelled_ray_events(events, views), key=event_sort_key)
