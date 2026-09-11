"""Adapt CLI-owned instance views without changing their selection contract."""
from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence
from inspire.services.job.job_events import job_instance_views
from inspire.services.hpc.hpc_instances import hpc_instance_views, hpc_instance_rank
from inspire.services.ray.ray_instances import ray_instance_views, ray_instance_rank
from inspire.services.serving.serving_instances import serving_instance_views
from .models import Instance


class Identity(Protocol):
    @property
    def handle(self) -> str: ...
    @property
    def label(self) -> str: ...


def label_selectors(views: Sequence[Identity], instance: str | Sequence[str] | None) -> tuple[str, ...]:
    """Translate opaque handles to public labels before shared service selection."""
    if instance is None or instance == "all":
        return ()
    selectors = (instance,) if isinstance(instance, str) else instance
    labels = {view.handle: view.label for view in views}
    return tuple(labels.get(selector, selector) for selector in selectors)


def sdk_instances(workload: str, rows: list[dict[str, Any]]) -> tuple[Instance, ...]:
    projects: dict[str, Callable[[list[dict[str, Any]]], Any]] = {"job": job_instance_views, "hpc": hpc_instance_views,
               "ray": ray_instance_views, "serving": serving_instance_views}
    views = projects[workload](rows)
    raw_by_handle = {
        str(row.get("name") or row.get("instance_name") or row.get("pod_name") or "").strip(): (i, row)
        for i, row in enumerate(rows)
    }
    result = []
    for view in views:
        i, raw = raw_by_handle[view.handle]
        rank = (hpc_instance_rank(raw, i) if workload == "hpc" else
                ray_instance_rank(raw, i) if workload == "ray" else raw.get("rank", i))
        result.append(Instance(
            label=view.label, handle=view.handle, role=view.role,
            pod=getattr(view, "pod", ""), kind=getattr(view, "kind", ""),
            status=str(raw.get("status") or raw.get("instance_status") or raw.get("phase") or ""),
            node=str(raw.get("node") or raw.get("node_name") or raw.get("host_name") or ""),
            rank=rank, raw=dict(raw),
        ))
    return tuple(result)
