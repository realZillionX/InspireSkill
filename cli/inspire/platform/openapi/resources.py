"""Resource parsing, matching, and display for the Inspire OpenAPI client."""

from __future__ import annotations

import re
from typing import Optional

from inspire.platform.openapi.models import ComputeGroup, GPUType, ResourceSpec
from inspire.compute_groups import load_compute_groups_from_config

# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


# The old ``build_default_resource_specs`` returned a hardcoded list of
# ``(gpu_type, gpu_count) -> spec_id`` rows baked in at build time. Platform
# rotations made it rot silently; the CLI now resolves specs live via
# ``inspire.cli.utils.spec_resolver.resolve_train_spec``.


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_resource_request(resource_str: str) -> tuple[GPUType, int]:
    """Parse natural language resource request into a (GPU type, count) tuple."""
    if not resource_str:
        raise ValueError("Resource description cannot be empty")

    resource_str = resource_str.upper().strip()

    patterns = [
        r"^(\d+)[xX]?(H100|H200)$",
        r"^(H100|H200)[xX]?(\d+)?$",
        r"^(\d+)\s+(H100|H200)$",
    ]

    gpu_count = 1
    gpu_type_str = None

    for pattern in patterns:
        match = re.match(pattern, resource_str.replace(" ", ""))
        if match:
            groups = match.groups()
            if len(groups) == 2:
                if groups[0].isdigit():
                    gpu_count = int(groups[0])
                    gpu_type_str = groups[1]
                elif groups[1] and groups[1].isdigit():
                    gpu_type_str = groups[0]
                    gpu_count = int(groups[1])
                else:
                    gpu_type_str = groups[0] if not groups[0].isdigit() else groups[1]
            break

    if not gpu_type_str:
        if "H200" in resource_str:
            gpu_type_str = "H200"
        elif "H100" in resource_str:
            gpu_type_str = "H100"

    if not gpu_type_str:
        raise ValueError(f"Unrecognized GPU type: {resource_str}")

    try:
        gpu_type = GPUType(gpu_type_str)
    except ValueError as e:
        raise ValueError(
            f"Unsupported GPU type: {gpu_type_str}, supported types: H100, H200"
        ) from e

    if gpu_count <= 0:
        raise ValueError(f"GPU count must be positive: {gpu_count}")

    return gpu_type, gpu_count


def normalize_gpu_type(gpu_type_raw: str) -> str:
    """Normalize raw GPU labels to OpenAPI enum values."""
    normalized = (gpu_type_raw or "").strip().upper()
    if not normalized:
        return ""
    if "H200" in normalized:
        return GPUType.H200.value
    if "H100" in normalized:
        return GPUType.H100.value
    return normalized


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


def find_compute_groups(
    compute_groups: list[ComputeGroup], *, gpu_type: GPUType
) -> list[ComputeGroup]:
    return [group for group in compute_groups if group.gpu_type == gpu_type]


def select_compute_group(
    matching_groups: list[ComputeGroup],
    *,
    prefer_location: Optional[str] = None,
) -> ComputeGroup:
    selected_group = matching_groups[0]

    if not prefer_location:
        return selected_group

    matched = False
    prefer_location = prefer_location.strip()
    prefer_location_lower = prefer_location.lower()

    for group in matching_groups:
        for candidate in _group_match_candidates(group):
            if prefer_location_lower in candidate.lower():
                selected_group = group
                matched = True
                break
        if matched:
            break

    if not matched:
        numbers = re.findall(r"\d+", prefer_location)
        if numbers:
            for num in numbers:
                for group in matching_groups:
                    for candidate in _group_match_candidates(group):
                        if num in candidate:
                            selected_group = group
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    break

    if not matched:
        available_locations = []
        seen = set()
        for group in matching_groups:
            label = _group_display_label(group)
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            available_locations.append(label)
        raise ValueError(
            f"Location '{prefer_location}' not found for {selected_group.gpu_type.value}. "
            f"Available locations: {', '.join(available_locations)}"
        )

    return selected_group


def _group_match_candidates(group: ComputeGroup) -> list[str]:
    candidates = []
    for value in (group.location, group.name):
        text = (value or "").strip()
        if text:
            candidates.append(text)
    return candidates


def _group_display_label(group: ComputeGroup) -> str:
    for value in (group.location, group.name, group.compute_group_id):
        text = (value or "").strip()
        if text:
            return text
    return "unknown"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Resource manager — parses resource strings and picks compute groups.

    Spec-id resolution moved out to
    :mod:`inspire.cli.utils.spec_resolver` (live platform query). This
    class is now just a thin wrapper around compute-group lookup.
    """

    def __init__(self, compute_groups_raw: Optional[list[dict]] = None):
        compute_groups_tuples = load_compute_groups_from_config(compute_groups_raw or [])
        self.compute_groups = []
        for group in compute_groups_tuples:
            if not group.compute_group_id:
                continue

            gpu_type_raw = normalize_gpu_type(group.gpu_type or "")
            if not gpu_type_raw:
                continue

            try:
                gpu_type = GPUType(gpu_type_raw)
            except ValueError:
                # Ignore non-OpenAPI-only groups (e.g. CPU or unsupported GPU families).
                continue

            self.compute_groups.append(
                ComputeGroup(
                    name=group.name,
                    compute_group_id=group.compute_group_id,
                    gpu_type=gpu_type,
                    location=group.location,
                )
            )

    def parse_resource_request(self, resource_str: str) -> tuple[GPUType, int]:
        return parse_resource_request(resource_str)

    def find_compute_groups(self, gpu_type: GPUType) -> list[ComputeGroup]:
        return find_compute_groups(self.compute_groups, gpu_type=gpu_type)


__all__ = ["ResourceManager"]
