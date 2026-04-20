"""Compute group selection logic for browser (web-session) availability APIs."""

from __future__ import annotations

import math
from typing import Optional

from .api import get_accurate_gpu_availability
from .models import GPUAvailability


def find_best_compute_group_accurate(
    gpu_type: Optional[str] = None,
    min_gpus: int = 8,
    preferred_groups: Optional[list[str]] = None,
    include_preemptible: bool = True,
    instance_count: int = 1,
    prefer_full_nodes: bool = True,
) -> Optional[GPUAvailability]:
    """Find the best compute group using accurate browser API data."""
    if prefer_full_nodes:
        try:
            from inspire.platform.web.resources import fetch_resource_availability

            node_availability = fetch_resource_availability(known_only=not preferred_groups)
            gpu_type_upper = (gpu_type or "").upper()
            required_instances = max(1, int(instance_count))
            normalized_min_gpus = max(1, int(min_gpus))

            candidates = []
            for group in node_availability:
                if gpu_type_upper and gpu_type_upper != "ANY":
                    if gpu_type_upper not in (group.gpu_type or "").upper():
                        continue

                gpu_per_node = group.gpu_per_node or 0
                if gpu_per_node <= 0:
                    continue

                nodes_per_instance = math.ceil(normalized_min_gpus / gpu_per_node)
                required_nodes = required_instances * nodes_per_instance
                if group.free_nodes < required_nodes:
                    continue

                candidates.append(group)

            if candidates:
                candidates.sort(
                    key=lambda g: (g.free_nodes, g.free_gpus),
                    reverse=True,
                )

                selected = None
                if preferred_groups:
                    for group in candidates:
                        if group.group_id in preferred_groups:
                            selected = group
                            break

                if selected is None:
                    selected = candidates[0]

                total_gpus = selected.total_nodes * selected.gpu_per_node
                used_gpus = max(total_gpus - selected.free_gpus, 0)

                return GPUAvailability(
                    group_id=selected.group_id,
                    group_name=selected.group_name,
                    gpu_type=selected.gpu_type,
                    total_gpus=total_gpus,
                    used_gpus=used_gpus,
                    available_gpus=selected.free_gpus,
                    low_priority_gpus=0,
                    free_nodes=selected.free_nodes,
                    gpu_per_node=selected.gpu_per_node,
                    selection_source="nodes",
                )
        except Exception:
            pass

    availability = get_accurate_gpu_availability()
    if not availability:
        return None

    def actual_available(group: GPUAvailability) -> int:
        return max(int(group.available_gpus), 0)

    def effective_available(group: GPUAvailability) -> int:
        if include_preemptible:
            return actual_available(group) + max(int(group.low_priority_gpus), 0)
        return actual_available(group)

    def _pick_best(
        candidates: list[GPUAvailability], *, allow_preemptible: bool
    ) -> GPUAvailability | None:
        if not candidates:
            return None

        if allow_preemptible:
            candidates = [group for group in candidates if effective_available(group) >= min_gpus]
            sort_key = lambda group: (  # noqa: E731
                effective_available(group),
                actual_available(group),
            )
        else:
            candidates = [group for group in candidates if actual_available(group) >= min_gpus]
            sort_key = lambda group: (  # noqa: E731
                actual_available(group),
                effective_available(group),
            )

        if not candidates:
            return None

        candidates.sort(key=sort_key, reverse=True)

        if preferred_groups:
            for group in candidates:
                if group.group_id in preferred_groups:
                    return group

        return candidates[0]

    if gpu_type and gpu_type.upper() != "ANY":
        gpu_type_upper = gpu_type.upper()
        filtered = [g for g in availability if gpu_type_upper in g.gpu_type.upper()]
    else:
        filtered = list(availability)

    selected = _pick_best(filtered, allow_preemptible=False)
    if selected is not None:
        return selected

    if include_preemptible:
        selected = _pick_best(filtered, allow_preemptible=True)
        if selected is not None:
            return selected

    return None


__all__ = ["find_best_compute_group_accurate"]
