"""Cluster resource metrics (time-series) queries.

Backs the web UI "资源视图" tab: `POST /api/v1/cluster_metric/resource_metric_by_time`.
Browser-API only — no OpenAPI equivalent.

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

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
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
            try:
                ts = int(row.get("timestamp"))
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

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/cluster_metric/resource_metric_by_time"),
        referer=_metrics_referer(task_type, task_id),
        body=body,
        timeout=timeout,
    )

    if data.get("code") != 0:
        raise ValueError(
            f"metric '{metric_type}' failed: {data.get('message') or 'unknown error'}"
        )

    payload = data.get("data") or {}
    # Upstream response key is "time_seris_metric_groups" (typo). Keep a
    # fallback for a future fix.
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

    Backing endpoint: ``POST /api/v1/cluster_metric/resource_metric_by_time``.

    The ``metric_types`` iterable is fanned out into one request per entry
    (a single multi-metric request silently returns data only for the first
    metric, confirmed by probe on 2026-04). Results are concatenated; if one
    metric errors the whole call raises ``ValueError``.

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
