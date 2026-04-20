"""Helpers for auto-selecting compute groups based on browser availability."""

from __future__ import annotations

from typing import Any

from inspire.platform.web import browser_api as browser_api_module


def find_best_compute_group_location(
    api: Any,  # InspireAPI (avoid import cycle)
    *,
    gpu_type: str,
    min_gpus: int,
    instance_count: int = 1,
    include_preemptible: bool = True,
) -> tuple[object | None, str | None, str]:
    """Return (best, selected_location, selected_group_name).

    `selected_location` is the value expected by ResourceManager for prefer_location. It can be None
    if the selected group cannot be mapped to a configured compute group.
    """
    best = browser_api_module.find_best_compute_group_accurate(
        gpu_type=gpu_type,
        min_gpus=min_gpus,
        include_preemptible=include_preemptible,
        instance_count=instance_count,
    )
    if not best:
        return None, None, ""

    selected_group_name = getattr(best, "group_name", "") or ""
    selected_location = None

    for group in getattr(getattr(api, "resource_manager", None), "compute_groups", []) or []:
        if getattr(group, "compute_group_id", None) == getattr(best, "group_id", None):
            selected_group_name = getattr(group, "name", selected_group_name) or selected_group_name
            selected_location = getattr(group, "location", None)
            break

    return best, selected_location, selected_group_name


__all__ = ["find_best_compute_group_location"]
