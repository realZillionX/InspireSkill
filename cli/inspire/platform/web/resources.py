"""Resource availability utilities for smart GPU allocation.

Uses browser API endpoints to get accurate GPU availability across
compute groups (matching the web UI).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional
from enum import Enum

from inspire.config import Config
from inspire.platform.web.session import fetch_workspace_availability, get_web_session
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config


class GPUType(Enum):
    """GPU types available in the cluster."""

    H100 = "H100"
    H200 = "H200"


@dataclass
class ComputeGroupAvailability:
    """GPU availability for a compute group."""

    group_id: str
    group_name: str
    gpu_type: str
    gpu_per_node: int
    total_nodes: int
    ready_nodes: int
    free_nodes: int
    free_gpus: int  # free_nodes * gpu_per_node
    online_nodes: int = 0  # resource_pool == "online"
    backup_nodes: int = 0  # resource_pool == "backup"
    fault_nodes: int = 0  # resource_pool == "fault"


# Known compute groups for smart allocation
# Only these groups will be used for auto-selection
# Will be initialized on first use from config
KNOWN_COMPUTE_GROUPS: dict[str, str] = {}


# Cache for availability data
_availability_cache: Optional[dict] = None
_cache_time: float = 0
_CACHE_TTL = 30  # seconds


def _normalize_gpu_type(display_name: str) -> str:
    """Normalize GPU type display name to short form (H100/H200/PPU ZW810/etc)."""
    display_upper = display_name.upper()
    if "H100" in display_upper:
        return GPUType.H100.value
    elif "H200" in display_upper:
        return GPUType.H200.value
    elif "PPU" in display_upper or "ZW810" in display_upper:
        return "PPU ZW810"
    # For other types, extract the main identifier (before parentheses)
    if "(" in display_name:
        return display_name.split("(")[0].strip()
    return display_name


def fetch_resource_availability(
    config: Optional[Config] = None,
    known_only: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[ComputeGroupAvailability]:
    """Fetch real-time GPU availability from compute groups.

    Uses the browser API workspace node endpoint to compute per-node
    availability and free GPU counts.

    Args:
        config: Optional CLI configuration (used for base_url override and compute_groups)
        known_only: If True, only return known compute groups (for auto-selection)
        progress_callback: Optional callback(fetched, total) for progress updates

    Returns:
        List of ComputeGroupAvailability sorted by free_gpus (descending)
    """
    global _availability_cache, _cache_time, KNOWN_COMPUTE_GROUPS

    resolved_config = config

    if resolved_config is None:
        try:
            resolved_config, _ = Config.from_files_and_env(
                require_credentials=False,
                require_target_dir=False,
            )
        except Exception:
            resolved_config = None

    # Load known compute groups from config
    known_groups_map: dict[str, str] = {}
    if resolved_config is not None and hasattr(resolved_config, "compute_groups"):
        compute_groups_tuples = load_compute_groups_from_config(resolved_config.compute_groups)
        known_groups_map = compute_group_name_map(compute_groups_tuples)
    # Update global for backward compatibility
    KNOWN_COMPUTE_GROUPS = known_groups_map

    # Check cache
    if _availability_cache and (time.time() - _cache_time < _CACHE_TTL):
        cache_key = "known" if known_only else "all"
        if cache_key in _availability_cache:
            return _availability_cache[cache_key]

    base_url = None
    if resolved_config is not None:
        base_url = getattr(resolved_config, "base_url", None)
    if not base_url:
        base_url = os.environ.get("INSPIRE_BASE_URL", "https://api.example.com")

    session = get_web_session(require_workspace=True)
    nodes = fetch_workspace_availability(session, base_url=base_url)

    groups: dict[str, dict] = {}
    total = len(nodes)

    for idx, node in enumerate(nodes):
        if progress_callback and total:
            progress_callback(idx + 1, total)

        # Only include GPU nodes
        if node.get("gpu_count", 0) == 0:
            continue

        group_id = node.get("logic_compute_group_id", "")
        if not group_id:
            continue

        # Skip if known_only and group is not known
        if known_only and group_id not in known_groups_map:
            continue

        if group_id not in groups:
            gpu_info = node.get("gpu_info", {})
            gpu_display = gpu_info.get("gpu_type_display", "Unknown")
            gpu_type = _normalize_gpu_type(gpu_display)

            group_name = node.get("logic_compute_group_name", "")
            if not group_name and group_id in known_groups_map:
                group_name = known_groups_map[group_id]
            if not group_name:
                group_name = "Unknown"

            groups[group_id] = {
                "group_id": group_id,
                "group_name": group_name,
                "gpu_type": gpu_type,
                "gpu_per_node": node.get("gpu_count", 0),
                "total_nodes": 0,
                "ready_nodes": 0,
                "free_nodes": 0,
                "online_nodes": 0,
                "backup_nodes": 0,
                "fault_nodes": 0,
            }

        groups[group_id]["total_nodes"] += 1

        # Count by resource_pool status
        resource_pool = str(node.get("resource_pool", "unknown")).lower()
        if resource_pool == "online":
            groups[group_id]["online_nodes"] += 1
        elif resource_pool == "backup":
            groups[group_id]["backup_nodes"] += 1
        elif resource_pool == "fault":
            groups[group_id]["fault_nodes"] += 1

        if str(node.get("status", "")).upper() == "READY":
            groups[group_id]["ready_nodes"] += 1

            task_list = node.get("task_list", [])
            cordon_type = str(node.get("cordon_type", "")).strip()
            is_maint = node.get("is_maint", False)
            is_truly_free = (
                (not task_list or len(task_list) == 0)
                and not cordon_type  # no cordon (hardware-fault, software-fault, etc.)
                and not is_maint  # not in maintenance
                and resource_pool != "fault"  # not in fault pool
            )
            if is_truly_free:
                groups[group_id]["free_nodes"] += 1

    # Convert to ComputeGroupAvailability objects
    availability_list = []
    for group_data in groups.values():
        free_gpus = group_data["free_nodes"] * group_data["gpu_per_node"]
        availability_list.append(
            ComputeGroupAvailability(
                group_id=group_data["group_id"],
                group_name=group_data["group_name"],
                gpu_type=group_data["gpu_type"],
                gpu_per_node=group_data["gpu_per_node"],
                total_nodes=group_data["total_nodes"],
                ready_nodes=group_data["ready_nodes"],
                free_nodes=group_data["free_nodes"],
                free_gpus=free_gpus,
                online_nodes=group_data["online_nodes"],
                backup_nodes=group_data["backup_nodes"],
                fault_nodes=group_data["fault_nodes"],
            )
        )

    # Sort by free_gpus descending
    availability_list.sort(key=lambda x: x.free_gpus, reverse=True)

    # Update cache
    if _availability_cache is None:
        _availability_cache = {}
    _availability_cache["known" if known_only else "all"] = availability_list
    _cache_time = time.time()

    return availability_list


def clear_availability_cache() -> None:
    """Clear the availability cache."""
    global _availability_cache, _cache_time
    _availability_cache = None
    _cache_time = 0
