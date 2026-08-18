"""Cluster resource metrics (time-series) queries.

Backs the web UI "资源视图" tab. Each service owns its own metrics Action, so
the route is chosen from `task_type`. Used by the notebook / job / HPC /
serving / Ray pages.

The UI fans out one request per metric_type, because the `GetTaskMetric` it
calls answers for the first entry of `metric_types` and drops the rest
(confirmed empirically 2026-04). This wrapper does not: `GetTaskMetricBatch`
honours the whole list, so the eight types the UI offers cost two requests
here rather than eight. That Action was in `/discovery` the whole time — the
2026-04 note ruled out multi-metric requests by testing the singular Action
alone, which is the same "only tested the same-named Action" mistake the
Browser API reference warns about.

Callers still get a single flat list of :class:`MetricGroup`.

Rate metrics (``*_usage_rate``) are 0-1 ratios; I/O metrics are bytes/second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import WebSession, get_web_session

_log = logging.getLogger(__name__)

__all__ = [
    "METRIC_TYPES",
    "INTERVAL_CHOICES",
    "TASK_TYPE_BY_RESOURCE",
    "MetricSample",
    "MetricGroup",
    "get_resource_metrics_by_time",
]

# All 8 metric types exposed by the UI 资源视图 tab.
METRIC_TYPES: tuple[str, ...] = (
    "gpu_usage_rate",
    "gpu_memory_usage_rate",
    "cpu_usage_rate",
    "memory_usage_rate",
    "disk_io_read",
    "disk_io_write",
    "network_tcp_ip_io_read",
    "network_tcp_ip_io_write",
)

# Interval options offered by the UI selector (seconds).
INTERVAL_CHOICES: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "30m": 1800,
    "1h": 3600,
}

# CLI resource noun → platform task_type (verified via direct probe 2026-04).
# Passing an unsupported task_type returns code=100000 with a Prometheus 422
# referencing an empty label name — fail fast rather than forwarding garbage.
TASK_TYPE_BY_RESOURCE: dict[str, str] = {
    "notebook": "interactive_modeling",
    "job": "distributed_training",
    "hpc": "hpc_job",
    "serving": "inference_serving",
    "ray": "ray_job",
}


@dataclass
class MetricSample:
    """Single time-stamped value."""

    timestamp: int  # unix seconds
    value: float


@dataclass
class MetricGroup:
    """Per-pod (group) time series for one metric_type.

    For multi-pod instances (distributed training / multi-replica serving)
    you get one group per pod; single-instance notebooks return exactly one.
    """

    group_name: str  # upstream pod name
    metric_type: str
    resource_name: str  # e.g. "GPU" / "GPU_Memory" / "CPU" / "Memory" / "Disk" / "Network"
    samples: list[MetricSample]

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "MetricGroup":
        raw = item.get("time_series") or []
        samples: list[MetricSample] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            raw_timestamp = row.get("timestamp")
            if raw_timestamp is None:
                continue
            try:
                ts = int(raw_timestamp)
            except (TypeError, ValueError):
                continue
            try:
                val = float(row.get("data", 0))
            except (TypeError, ValueError):
                val = 0.0
            samples.append(MetricSample(timestamp=ts, value=val))
        return cls(
            group_name=str(item.get("group_name", "")),
            metric_type=str(item.get("metric_type", "")),
            resource_name=str(item.get("resource_name", "")),
            samples=samples,
        )


# There is no cluster-wide metrics endpoint: each service owns a `GetTaskMetric`
# Action taking the same per-task filter. `workspace.GetOverviewResourceMetricByTime`
# is NOT one -- it is a workspace-level overview and answers AccessForbidden to
# ordinary members.
_METRIC_ROUTE_BY_TASK_TYPE: dict[str, str] = {
    "interactive_modeling": "notebook",
    "distributed_training": "train",
    "hpc_job": "hpc",
    "inference_serving": "inference_serving",
    "ray_job": "ray",
}


def _metrics_referer(task_type: str, task_id: str) -> str:
    base = _get_base_url()
    # Any valid qz.sii.edu.cn page works; pick the canonical detail page per
    # task_type so debug traces match what DevTools shows for the real UI.
    ref_map = {
        "interactive_modeling": f"{base}/jobs/interactiveModelDetail/{task_id}",
        "distributed_training": f"{base}/jobs/distributedTrainingDetail/{task_id}",
        "hpc_job": f"{base}/jobs/hpcDetail/{task_id}",
        "inference_serving": f"{base}/jobs/modelDeploymentDetail/{task_id}",
    }
    return ref_map.get(task_type, f"{base}/jobs/interactiveModeling")


#: `GetTaskMetricBatch` answers `InternalError: 指标查询暂不可用` to any request
#: carrying more than five metric types. Reproduced 3/3 rounds on 2026-08-18,
#: each round preceded by a singular `GetTaskMetric` call that succeeded — so it
#: is the request shape, not a flapping backend. The platform spends an
#: `InternalError` rather than `InvalidParameter` on it and documents nothing,
#: which is why this is a ceiling to stay under rather than a bug to report.
_MAX_METRIC_TYPES_PER_REQUEST = 5


def _metric_groups_from(payload: Any) -> list[dict]:
    """Read the group list out of either spelling of the platform's key."""
    # The platform emits the misspelled `seris`; accept the corrected spelling
    # too so the wrapper follows either response shape.
    raw = payload.get("time_seris_metric_groups")
    if raw is None:
        raw = payload.get("time_series_metric_groups")
    return [g for g in raw if isinstance(g, dict)] if isinstance(raw, list) else []


def _request_metric_batch(
    session: WebSession,
    *,
    task_ids: list[str],
    task_type: str,
    metric_types: list[str],
    start_timestamp: int,
    end_timestamp: int,
    interval_second: int,
    timeout: int,
) -> dict[str, list[MetricGroup]]:
    """One `GetTaskMetricBatch` call: many tasks x up to five metric types.

    Unlike the singular `GetTaskMetric` this Action really does honour every
    entry of ``metric_types`` (the singular silently answers for the first one
    only), and it takes the task list flat instead of inside a ``filter``. It
    also declares no ``logic_compute_group_id``: the caller's compute group is
    not part of the request.

    Returns groups keyed by task id. A task the platform answered nothing for
    is absent from the mapping — which is not the same as a task whose groups
    came back empty, and callers depending on that distinction must not
    conflate them.
    """
    route = _METRIC_ROUTE_BY_TASK_TYPE.get(task_type)
    if route is None:
        raise ValueError(f"unknown task type {task_type!r}")

    body = {
        "task_type": task_type,
        "task_ids": list(task_ids),
        "metric_types": list(metric_types),
        "time_range": {
            "start_timestamp": int(start_timestamp),
            "end_timestamp": int(end_timestamp),
            "interval_second": int(interval_second),
        },
    }

    label = ", ".join(metric_types)
    data = _request_json(
        session,
        "POST",
        f"/api/v2/{route}?Action=GetTaskMetricBatch",
        referer=_metrics_referer(task_type, task_ids[0]),
        body=body,
        timeout=timeout,
    )
    try:
        payload = _v2_result(data)
    except ValueError as exc:
        raise ValueError(f"metrics '{label}' failed: {exc}") from exc

    raw_tasks = payload.get("task_metrics")
    if not isinstance(raw_tasks, list):
        return {}

    out: dict[str, list[MetricGroup]] = {}
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("task_id") or "")
        groups = [MetricGroup.from_api(g) for g in _metric_groups_from(entry)]
        out.setdefault(key, []).extend(groups)
    return out


def get_resource_metrics_by_time(
    *,
    task_id: str,
    task_type: str,
    logic_compute_group_id: str,
    metric_types: Iterable[str],
    start_timestamp: int,
    end_timestamp: int,
    interval_second: int = 60,
    session: Optional[WebSession] = None,
    timeout: int = 30,
) -> list[MetricGroup]:
    """Query cluster-metric time series for a single task.

    Backing Action: ``GetTaskMetricBatch`` on the service that owns the
    workload. ``metric_types`` goes out in chunks of
    :data:`_MAX_METRIC_TYPES_PER_REQUEST`, so the eight types the UI offers
    cost two requests rather than the eight the singular ``GetTaskMetric``
    needed — it answers for the first metric of a list and drops the rest, so
    fanning out per metric was the only way to use it.

    Results are concatenated in the caller's ``metric_types`` order; if one
    chunk errors the whole call raises ``ValueError``.

    ``logic_compute_group_id`` is accepted for call compatibility and is not
    sent: this Action does not take one.

    ``task_type`` must be one of :data:`TASK_TYPE_BY_RESOURCE` values:
    ``interactive_modeling`` / ``distributed_training`` / ``hpc_job`` /
    ``inference_serving`` / ``ray_job``.
    """
    if session is None:
        session = get_web_session()

    metrics = [m for m in metric_types if m]
    if not metrics:
        raise ValueError("no metric_types provided")

    unknown = [m for m in metrics if m not in METRIC_TYPES]
    if unknown:
        raise ValueError(
            f"unknown metric_type(s): {', '.join(unknown)} "
            f"(valid: {', '.join(METRIC_TYPES)})"
        )

    if task_type not in set(TASK_TYPE_BY_RESOURCE.values()):
        raise ValueError(
            f"unknown task_type '{task_type}' "
            f"(valid: {', '.join(sorted(set(TASK_TYPE_BY_RESOURCE.values())))})"
        )

    all_groups: list[MetricGroup] = []
    for start in range(0, len(metrics), _MAX_METRIC_TYPES_PER_REQUEST):
        chunk = metrics[start : start + _MAX_METRIC_TYPES_PER_REQUEST]
        by_task = _request_metric_batch(
            session,
            task_ids=[task_id],
            task_type=task_type,
            metric_types=chunk,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            interval_second=interval_second,
            timeout=timeout,
        )
        # One task went out, so anything keyed to another id is not ours.
        all_groups.extend(by_task.get(task_id, []))
    return all_groups
