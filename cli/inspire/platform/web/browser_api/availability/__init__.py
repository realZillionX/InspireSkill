"""Platform capacity and node-dimension queries with their response models.

Keep route-specific filters and pagination traps here, including the Actions
where page_size=-1 does not enumerate everything. Quota selection and catalogue
caching belong to inspire.services.catalog; CLI presentation belongs to
inspire.cli. A returned capacity snapshot is not a reservation.
"""

from __future__ import annotations

from .api import (
    QUOTA_PRIORITY_SPEC_FIELDS,
    get_accurate_resource_availability,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    get_resource_inventory,
    get_quota_priority_levels,
    list_member_usage,
    list_node_dimension,
    list_node_events,
    list_node_specs,
    list_compute_groups,
    list_task_usage,
)
from .models import (
    FullFreeNodeCount,
    GPUAvailability,
    MemberUsage,
    NodeSpec,
    TaskUsage,
)

__all__ = [
    "QUOTA_PRIORITY_SPEC_FIELDS",
    "FullFreeNodeCount",
    "GPUAvailability",
    "MemberUsage",
    "NodeSpec",
    "TaskUsage",
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "get_resource_inventory",
    "get_quota_priority_levels",
    "list_member_usage",
    "list_node_dimension",
    "list_node_events",
    "list_node_specs",
    "list_compute_groups",
    "list_task_usage",
]
