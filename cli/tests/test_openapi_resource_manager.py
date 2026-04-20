import pytest

from inspire.platform.openapi.models import GPUType
from inspire.platform.openapi.resources import ResourceManager


def test_resource_manager_ignores_compute_groups_without_supported_gpu_type() -> None:
    manager = ResourceManager(
        [
            {"name": "CPU", "id": "lcg-cpu", "gpu_type": ""},
            {"name": "4090", "id": "lcg-4090", "gpu_type": "4090"},
            {"name": "H100", "id": "lcg-h100", "gpu_type": "h100"},
        ]
    )

    assert len(manager.compute_groups) == 1
    assert manager.compute_groups[0].compute_group_id == "lcg-h100"
    assert manager.compute_groups[0].gpu_type == GPUType.H100


def test_resource_manager_ignores_compute_group_without_id() -> None:
    manager = ResourceManager([{"name": "H200 missing id", "gpu_type": "H200"}])

    assert manager.compute_groups == []


def test_resource_manager_accepts_discovered_gpu_type_labels() -> None:
    manager = ResourceManager(
        [
            {"name": "H200-1", "id": "lcg-h200-1", "gpu_type": "NVIDIA H200 (141GB)"},
            {"name": "H100-1", "id": "lcg-h100-1", "gpu_type": "NVIDIA H100 (80GB)"},
        ]
    )

    ids_to_types = {group.compute_group_id: group.gpu_type for group in manager.compute_groups}

    assert ids_to_types["lcg-h200-1"] == GPUType.H200
    assert ids_to_types["lcg-h100-1"] == GPUType.H100


def test_resource_manager_matches_group_name_when_location_empty() -> None:
    manager = ResourceManager(
        [
            {"name": "H200-1号机房", "id": "lcg-h200-1", "gpu_type": "H200", "location": ""},
            {"name": "H200-3号机房", "id": "lcg-h200-3", "gpu_type": "H200", "location": ""},
        ]
    )

    spec_id, group_id = manager.get_recommended_config("8xH200", prefer_location="H200-3号机房")

    assert spec_id == "f23c8d53-395f-473c-81e0-dbd132711861"
    assert group_id == "lcg-h200-3"


def test_resource_manager_numeric_match_uses_group_name_when_location_empty() -> None:
    manager = ResourceManager(
        [
            {"name": "H200-1号机房", "id": "lcg-h200-1", "gpu_type": "H200", "location": ""},
            {"name": "H200-3号机房", "id": "lcg-h200-3", "gpu_type": "H200", "location": ""},
        ]
    )

    _, group_id = manager.get_recommended_config("8xH200", prefer_location="3号")
    assert group_id == "lcg-h200-3"


def test_resource_manager_error_lists_non_empty_labels() -> None:
    manager = ResourceManager(
        [
            {"name": "H200-1号机房", "id": "lcg-h200-1", "gpu_type": "H200", "location": ""},
            {"name": "H200-3号机房", "id": "lcg-h200-3", "gpu_type": "H200", "location": ""},
        ]
    )

    with pytest.raises(ValueError) as exc_info:
        manager.get_recommended_config("8xH200", prefer_location="not-found")

    message = str(exc_info.value)
    assert "Available locations: H200-1号机房, H200-3号机房" in message
    assert "Available locations: , " not in message


def test_resource_manager_no_preference_picks_first_group() -> None:
    """Without prefer_location, get_recommended_config picks the first group.

    This documents the behaviour that caused the queuing bug: when
    _resolve_run_resource_and_location lost the auto-selected group name
    (selected_location was empty and selected_group_name was not forwarded),
    the job was submitted to the first config group regardless of availability.
    """
    manager = ResourceManager(
        [
            {"name": "H200-1号机房", "id": "lcg-h200-1", "gpu_type": "H200"},
            {"name": "H200-2号机房", "id": "lcg-h200-2", "gpu_type": "H200"},
        ]
    )

    _, group_id = manager.get_recommended_config("8xH200", prefer_location=None)
    assert group_id == "lcg-h200-1"  # always first → wrong if GPUs are on group 2


def test_autoselect_location_fallback_uses_group_name() -> None:
    """Regression: find_best_compute_group_location must return group name
    when location is empty so that run.py can forward it to the API."""
    from unittest.mock import MagicMock

    from inspire.cli.utils.compute_group_autoselect import find_best_compute_group_location
    from inspire.platform.openapi.models import ComputeGroup

    fake_best = MagicMock()
    fake_best.group_id = "lcg-h200-2"
    fake_best.group_name = "H200-2号机房"

    api = MagicMock()
    api.resource_manager.compute_groups = [
        ComputeGroup(
            name="H200-1号机房",
            compute_group_id="lcg-h200-1",
            gpu_type=GPUType.H200,
            location="",
        ),
        ComputeGroup(
            name="H200-2号机房",
            compute_group_id="lcg-h200-2",
            gpu_type=GPUType.H200,
            location="",
        ),
    ]

    import inspire.cli.utils.compute_group_autoselect as cga_mod

    original = cga_mod.browser_api_module.find_best_compute_group_accurate

    try:
        cga_mod.browser_api_module.find_best_compute_group_accurate = MagicMock(
            return_value=fake_best
        )

        best, selected_location, selected_group_name = find_best_compute_group_location(
            api, gpu_type="H200", min_gpus=8
        )

        assert best is fake_best
        # location is empty because config entries have no location field
        assert selected_location == ""
        # group name must be populated so run.py can use it as fallback
        assert selected_group_name == "H200-2号机房"

        # Verify the fallback produces correct group selection
        location = selected_location or selected_group_name or None
        manager = ResourceManager(
            [
                {"name": "H200-1号机房", "id": "lcg-h200-1", "gpu_type": "H200"},
                {"name": "H200-2号机房", "id": "lcg-h200-2", "gpu_type": "H200"},
            ]
        )
        _, group_id = manager.get_recommended_config("8xH200", prefer_location=location)
        assert group_id == "lcg-h200-2"  # must match auto-selected, not first
    finally:
        cga_mod.browser_api_module.find_best_compute_group_accurate = original
