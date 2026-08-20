"""Cluster resource metrics (time-series) queries.

Backs the web UI "资源视图" tab. Each service owns its own metrics Action, so
the route is chosen from `task_type`. Used by the notebook / job / HPC /
serving / Ray pages.

The UI fans out one request per metric_type (confirmed empirically 2026-04:
sending a list of 5 metric types in one call only returns results for the
first). This wrapper loops per-metric and aggregates so callers get a single
flat list of :class:`MetricGroup`.

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


def _request_one_metric(
    session: WebSession,
    *,
    task_id: str,
    task_type: str,
    logic_compute_group_id: str,
    metric_type: str,
    start_timestamp: int,
    end_timestamp: int,
    interval_second: int,
    timeout: int,
) -> list[MetricGroup]:
    body = {
        "filter": {
            "logic_compute_group_id": logic_compute_group_id,
            "task_id": task_id,
            "task_type": task_type,
        },
        "metric_types": [metric_type],
        "time_range": {
            "start_timestamp": int(start_timestamp),
            "end_timestamp": int(end_timestamp),
            "interval_second": int(interval_second),
        },
    }

    route = _METRIC_ROUTE_BY_TASK_TYPE.get(task_type)
    if route is None:
        raise ValueError(f"metric '{metric_type}' failed: unknown task type {task_type!r}")

    data = _request_json(
        session,
        "POST",
        f"/api/v2/{route}?Action=GetTaskMetric",
        referer=_metrics_referer(task_type, task_id),
        body=body,
        timeout=timeout,
    )

    try:
        payload = _v2_result(data)
    except ValueError as exc:
        raise ValueError(f"metric '{metric_type}' failed: {exc}") from exc

    # The platform currently emits the misspelled key; accept the corrected
    # spelling as well so the wrapper follows either response shape.
    raw_groups = payload.get("time_seris_metric_groups")
    if raw_groups is None:
        raw_groups = payload.get("time_series_metric_groups")
    if not isinstance(raw_groups, list):
        return []
    return [MetricGroup.from_api(g) for g in raw_groups if isinstance(g, dict)]


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

    Backing Action: ``GetTaskMetric`` on the service that owns the workload.

    The ``metric_types`` iterable is fanned out into one request per entry
    (a single multi-metric request silently returns data only for the first
    metric, confirmed by probe on 2026-04 and again on 2026-08-18 with two,
    four and eight types). Results are concatenated; if one metric errors the
    whole call raises ``ValueError``.

    **The fan-out is not an oversight, and ``GetTaskMetricBatch`` does not fix
    it.** That Action exists on every one of these routes and answers a
    different, worse dataset: measured on `train` against four running jobs
    over the same window, ``disk_io_read`` and ``disk_io_write`` return zero
    samples where this path returns 61, both ``network_tcp_ip_io_*`` types
    fail with ``InternalError``, and every group comes back without its
    ``group_name`` so the per-pod split is gone. Its response is shaped
    differently too -- ``task_metrics[].time_series_metric_groups`` rather than
    the top-level ``time_seris_metric_groups`` read here -- which is how it can
    look empty rather than broken to a reader expecting this shape.

    **The four types that do answer are not the same numbers.** Over a fixed
    past window each endpoint is deterministic -- two calls agree byte for
    byte -- and they still disagree with each other, because the batch Action
    aggregates half the samples: the common denominator of everything this
    path returns is consistently twice the batch one (``200 × gpu_count``
    against ``100 × gpu_count``, on all four jobs). Long-run means stay close;
    individual points reach 0.206 against 0.5. So a chart drawn from the batch
    Action is not the chart the console shows. The console never calls it.

    ``task_type`` must be one of :data:`TASK_TYPE_BY_RESOURCE` values:
    ``interactive_modeling`` / ``distributed_training`` / ``hpc_job`` /
    ``inference_serving``.
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
    for metric in metrics:
        all_groups.extend(
            _request_one_metric(
                session,
                task_id=task_id,
                task_type=task_type,
                logic_compute_group_id=logic_compute_group_id,
                metric_type=metric,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                interval_second=interval_second,
                timeout=timeout,
            )
        )
    return all_groups
