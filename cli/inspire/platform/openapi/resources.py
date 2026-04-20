"""Resource parsing, matching, and display for the Inspire OpenAPI client."""

from __future__ import annotations

import re
from typing import Optional

from inspire.platform.openapi.models import ComputeGroup, GPUType, ResourceSpec
from inspire.compute_groups import load_compute_groups_from_config

# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def build_default_resource_specs() -> list[ResourceSpec]:
    # These spec_ids are quota_ids shared across all compute groups (H100/H200).
    # Fetched from: POST /api/v1/resource_prices/logic_compute_groups/
    #   with schedule_config_type=SCHEDULE_CONFIG_TYPE_TRAIN
    return [
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=1,
            cpu_cores=15,
            memory_gb=200,
            gpu_memory_gb=141,
            spec_id="4dd0e854-e2a4-4253-95e6-64c13f0b5117",
            description="1 × NVIDIA H200 (141GB) + 15 CPU cores + 200GB RAM",
        ),
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=2,
            cpu_cores=30,
            memory_gb=400,
            gpu_memory_gb=141,
            spec_id="7166bd2e-6cbe-4bd9-be38-762d11003e7f",
            description="2 × NVIDIA H200 (141GB) + 30 CPU cores + 400GB RAM",
        ),
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=4,
            cpu_cores=60,
            memory_gb=800,
            gpu_memory_gb=141,
            spec_id="45ab2351-fc8a-4d50-a30b-b39a5306c906",
            description="4 × NVIDIA H200 (141GB) + 60 CPU cores + 800GB RAM",
        ),
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=8,
            cpu_cores=120,
            memory_gb=1600,
            gpu_memory_gb=141,
            spec_id="f23c8d53-395f-473c-81e0-dbd132711861",
            description="8 × NVIDIA H200 (141GB) + 120 CPU cores + 1600GB RAM",
        ),
    ]


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


def find_matching_specs(
    resource_specs: list[ResourceSpec],
    *,
    gpu_type: GPUType,
    gpu_count: int,
) -> list[ResourceSpec]:
    matching_specs = []

    for spec in resource_specs:
        if spec.gpu_type == gpu_type or (
            gpu_type == GPUType.H100 and spec.gpu_type == GPUType.H200
        ):
            if spec.gpu_count >= gpu_count:
                matching_specs.append(spec)

    matching_specs.sort(key=lambda x: x.gpu_count)
    return matching_specs


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
# Display
# ---------------------------------------------------------------------------


def display_available_resources(
    *,
    resource_specs: list[ResourceSpec],
    compute_groups: list[ComputeGroup],
) -> None:
    """Print all available resource configurations."""
    print("\n📊 Available Resource Configurations:")
    print("=" * 60)

    print("\n🖥️  GPU Spec Configurations:")
    for spec in resource_specs:
        print(f"  • {spec.description}")
        print(f"    Spec ID: {spec.spec_id}")

    print("\n🏢 Compute Groups:")
    for group in compute_groups:
        print(f"  • {group.name} ({group.location})")
        print(f"    Compute Group ID: {group.compute_group_id}")

    print("\n💡 Usage Examples:")
    print("  • --resource 'H200'     -> 1x H200 GPU")
    print("  • --resource '4xH200'   -> 4x H200 GPU")
    print("  • --resource '8 H200'   -> 8x H200 GPU")
    print("  • --resource 'H100'     -> 1x H100 GPU")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Resource manager - handles resource spec and compute group matching."""

    def __init__(self, compute_groups_raw: Optional[list[dict]] = None):
        self.resource_specs = build_default_resource_specs()

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

    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> list[ResourceSpec]:
        return find_matching_specs(self.resource_specs, gpu_type=gpu_type, gpu_count=gpu_count)

    def find_compute_groups(self, gpu_type: GPUType) -> list[ComputeGroup]:
        return find_compute_groups(self.compute_groups, gpu_type=gpu_type)

    def get_recommended_config(
        self, resource_str: str, prefer_location: Optional[str] = None
    ) -> tuple[str, str]:
        gpu_type, gpu_count = self.parse_resource_request(resource_str)

        matching_specs = self.find_matching_specs(gpu_type, gpu_count)
        if not matching_specs:
            available_configs = [
                f"{spec.gpu_count}x{spec.gpu_type.value}" for spec in self.resource_specs
            ]
            raise ValueError(
                f"No configuration found matching {gpu_count}x{gpu_type.value}. "
                f"Available configurations: {', '.join(available_configs)}"
            )

        selected_spec = matching_specs[0]

        matching_groups = self.find_compute_groups(gpu_type)
        if not matching_groups:
            raise ValueError(f"No compute group found supporting {gpu_type.value}")

        selected_group = select_compute_group(matching_groups, prefer_location=prefer_location)
        return selected_spec.spec_id, selected_group.compute_group_id

    def display_available_resources(self) -> None:
        display_available_resources(
            resource_specs=self.resource_specs,
            compute_groups=self.compute_groups,
        )


__all__ = ["ResourceManager"]
