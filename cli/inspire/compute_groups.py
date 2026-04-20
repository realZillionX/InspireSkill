"""Shared compute group definitions used across CLI and API helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComputeGroupDefinition:
    name: str
    compute_group_id: str
    gpu_type: str
    location: str = ""


# Default empty tuple - compute groups are loaded from config
COMPUTE_GROUPS: tuple[ComputeGroupDefinition, ...] = ()


def load_compute_groups_from_config(raw_list: list[dict]) -> tuple[ComputeGroupDefinition, ...]:
    """Load compute groups from config file.

    Args:
        raw_list: List of compute group dicts from config.toml

    Returns:
        Tuple of ComputeGroupDefinition objects

    Example config.toml:
        [[compute_groups]]
        name = "H100 (CUDA 12.8)"
        id = "lcg-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        gpu_type = "H100"
        location = "CUDA 12.8"
    """
    groups = []
    for item in raw_list:
        try:
            groups.append(
                ComputeGroupDefinition(
                    name=item.get("name", ""),
                    compute_group_id=item.get("id", ""),
                    gpu_type=item.get("gpu_type", ""),
                    location=item.get("location", ""),
                )
            )
        except (KeyError, TypeError):
            # Skip invalid entries
            continue
    return tuple(groups)


def compute_group_name_map(
    groups: tuple[ComputeGroupDefinition, ...] = COMPUTE_GROUPS,
) -> dict[str, str]:
    """Create a mapping from compute group ID to name.

    Args:
        groups: Tuple of ComputeGroupDefinition objects

    Returns:
        Dict mapping compute_group_id to name
    """
    return {group.compute_group_id: group.name for group in groups}
