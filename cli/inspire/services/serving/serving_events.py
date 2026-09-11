from __future__ import annotations

from typing import Any
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.job.job_events import event_sort_key
from inspire.services.serving.serving_instances import select_serving_instance_views, serving_instance_views

_INSTANCE_EVENT_FETCH_SIZE = 200


def serving_events(
    serving_id: str,
    *,
    session,  # noqa: ANN001
    selectors: tuple[str, ...] = (),
    workload_level: bool = False,
) -> list[dict[str, Any]]:
    """Read deployment events, replica events, or one replica's, in order.

    The two levels are separate calls against the same Action, so the merged
    chronology is imposed here. Instance rows are labelled with the identity
    `inspire serving instances` prints, because their `object_id` is the
    namespaced pod handle and never reaches output.
    """
    if workload_level:
        return sorted(
            browser_api_module.list_serving_events(serving_id, session=session),
            key=event_sort_key,
        )

    instances, _total = browser_api_module.list_serving_instances(
        serving_id,
        page_size=_INSTANCE_EVENT_FETCH_SIZE,
        session=session,
    )
    views = select_serving_instance_views(serving_instance_views(instances), selectors)
    labels = {view.handle: view.label for view in views}

    instance_events: list[dict[str, Any]] = []
    if views:
        for event in browser_api_module.list_serving_events(
            serving_id,
            pod_names=[view.handle for view in views],
            session=session,
        ):
            row = dict(event)
            label = labels.get(str(row.get("object_id") or "").strip())
            if label:
                row["instance"] = label
            instance_events.append(row)

    if selectors:
        return sorted(instance_events, key=event_sort_key)
    merged = browser_api_module.list_serving_events(serving_id, session=session) + instance_events
    return sorted(merged, key=event_sort_key)
