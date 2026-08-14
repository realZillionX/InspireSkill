"""Browser (web-session) APIs for resource availability."""

from __future__ import annotations

from .api import (
    get_accurate_resource_availability,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    get_group_node_gpu_type,
    get_schedule_config_specs,
    list_node_dimension,
    list_compute_groups,
)
from .models import FullFreeNodeCount, GPUAvailability

__all__ = [
    "FullFreeNodeCount",
    "GPUAvailability",
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "get_group_node_gpu_type",
    "get_schedule_config_specs",
    "list_node_dimension",
    "list_compute_groups",
]
