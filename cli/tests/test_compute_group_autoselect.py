from __future__ import annotations

from types import SimpleNamespace

from inspire.config import Config
from inspire.platform.web import resources as resources_mod
from inspire.platform.web.browser_api.availability import select as select_mod
from inspire.platform.web.browser_api.availability.models import GPUAvailability


def test_fetch_resource_availability_known_only_uses_resolved_config(monkeypatch) -> None:
    cfg = Config(
        username="user",
        password="pass",
        base_url="https://example.com",
        compute_groups=[
            {"id": "lcg-known", "name": "H200-known", "gpu_type": "H200"},
        ],
    )

    monkeypatch.setattr(
        resources_mod.Config,
        "from_files_and_env",
        lambda *args, **kwargs: (cfg, {}),
    )
    monkeypatch.setattr(
        resources_mod,
        "get_web_session",
        lambda require_workspace=True: SimpleNamespace(workspace_id="ws-test"),
    )
    monkeypatch.setattr(
        resources_mod,
        "fetch_workspace_availability",
        lambda session, base_url=None: [
            {
                "gpu_count": 8,
                "logic_compute_group_id": "lcg-known",
                "logic_compute_group_name": "",
                "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
                "resource_pool": "online",
                "status": "READY",
                "task_list": [],
            },
            {
                "gpu_count": 8,
                "logic_compute_group_id": "lcg-unknown",
                "logic_compute_group_name": "H200-unknown",
                "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
                "resource_pool": "online",
                "status": "READY",
                "task_list": [],
            },
        ],
    )

    resources_mod.clear_availability_cache()
    availability = resources_mod.fetch_resource_availability(known_only=True)

    assert [group.group_id for group in availability] == ["lcg-known"]
    assert availability[0].free_nodes == 1
    assert availability[0].free_gpus == 8


def test_find_best_compute_group_uses_node_availability_without_explicit_config(
    monkeypatch,
) -> None:
    cfg = Config(
        username="user",
        password="pass",
        base_url="https://example.com",
        compute_groups=[
            {"id": "lcg-h200-1", "name": "H200-1号机房", "gpu_type": "H200"},
            {"id": "lcg-h200-2", "name": "H200-2号机房", "gpu_type": "H200"},
        ],
    )

    monkeypatch.setattr(
        resources_mod.Config,
        "from_files_and_env",
        lambda *args, **kwargs: (cfg, {}),
    )
    monkeypatch.setattr(
        resources_mod,
        "get_web_session",
        lambda require_workspace=True: SimpleNamespace(workspace_id="ws-test"),
    )
    monkeypatch.setattr(
        resources_mod,
        "fetch_workspace_availability",
        lambda session, base_url=None: [
            {
                "gpu_count": 8,
                "logic_compute_group_id": "lcg-h200-1",
                "logic_compute_group_name": "H200-1号机房",
                "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
                "resource_pool": "online",
                "status": "READY",
                "task_list": [],
            },
            {
                "gpu_count": 8,
                "logic_compute_group_id": "lcg-h200-2",
                "logic_compute_group_name": "H200-2号机房",
                "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
                "resource_pool": "online",
                "status": "READY",
                "task_list": [],
            },
            {
                "gpu_count": 8,
                "logic_compute_group_id": "lcg-h200-2",
                "logic_compute_group_name": "H200-2号机房",
                "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
                "resource_pool": "online",
                "status": "READY",
                "task_list": [],
            },
        ],
    )
    monkeypatch.setattr(
        select_mod,
        "get_accurate_gpu_availability",
        lambda: (_ for _ in ()).throw(AssertionError("should use node availability first")),
    )

    resources_mod.clear_availability_cache()
    selected = select_mod.find_best_compute_group_accurate(gpu_type="H200", min_gpus=8)

    assert selected is not None
    assert selected.group_id == "lcg-h200-2"
    assert selected.selection_source == "nodes"
    assert selected.free_nodes == 2


def test_find_best_compute_group_prefers_actual_free_gpus_before_preemptible(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        select_mod,
        "get_accurate_gpu_availability",
        lambda: [
            GPUAvailability(
                group_id="lcg-h200-3",
                group_name="H200-3号机房",
                gpu_type="NVIDIA H200 (141GB)",
                total_gpus=4509,
                used_gpus=4529,
                available_gpus=-20,
                low_priority_gpus=553,
            ),
            GPUAvailability(
                group_id="lcg-h200-3-2",
                group_name="H200-3号机房-2",
                gpu_type="NVIDIA H200 (141GB)",
                total_gpus=1672,
                used_gpus=1598,
                available_gpus=74,
                low_priority_gpus=16,
            ),
        ],
    )

    selected = select_mod.find_best_compute_group_accurate(
        gpu_type="H200",
        min_gpus=8,
        include_preemptible=True,
        prefer_full_nodes=False,
    )

    assert selected is not None
    assert selected.group_id == "lcg-h200-3-2"


def test_fetch_resource_availability_excludes_broken_nodes(monkeypatch) -> None:
    """Nodes with cordon_type, is_maint, or fault resource_pool should NOT count as free."""
    cfg = Config(
        username="user",
        password="pass",
        base_url="https://example.com",
        compute_groups=[
            {"id": "lcg-test", "name": "TestGroup", "gpu_type": "H200"},
        ],
    )

    monkeypatch.setattr(
        resources_mod.Config,
        "from_files_and_env",
        lambda *args, **kwargs: (cfg, {}),
    )
    monkeypatch.setattr(
        resources_mod,
        "get_web_session",
        lambda require_workspace=True: SimpleNamespace(workspace_id="ws-test"),
    )

    def fake_nodes(session, base_url=None):
        base = {
            "gpu_count": 8,
            "logic_compute_group_id": "lcg-test",
            "logic_compute_group_name": "TestGroup",
            "gpu_info": {"gpu_type_display": "NVIDIA H200 (141GB)"},
            "status": "READY",
            "task_list": [],
        }
        return [
            # Truly free node
            {**base, "resource_pool": "online", "cordon_type": "", "is_maint": False},
            # Hardware-fault cordon — should NOT be free
            {**base, "resource_pool": "online", "cordon_type": "hardware-fault", "is_maint": False},
            # Software-fault cordon — should NOT be free
            {**base, "resource_pool": "online", "cordon_type": "software-fault", "is_maint": False},
            # Maintenance — should NOT be free
            {**base, "resource_pool": "online", "cordon_type": "", "is_maint": True},
            # Fault pool — should NOT be free
            {**base, "resource_pool": "fault", "cordon_type": "", "is_maint": False},
            # Busy node (has tasks) — should NOT be free
            {
                **base,
                "resource_pool": "online",
                "cordon_type": "",
                "is_maint": False,
                "task_list": [{"task_id": "t1"}],
            },
        ]

    monkeypatch.setattr(resources_mod, "fetch_workspace_availability", fake_nodes)

    resources_mod.clear_availability_cache()
    availability = resources_mod.fetch_resource_availability(config=cfg, known_only=True)

    assert len(availability) == 1
    group = availability[0]
    assert group.total_nodes == 6
    assert group.ready_nodes == 6  # all have status=READY
    assert group.free_nodes == 1  # only the truly free node
    assert group.free_gpus == 8  # 1 free node * 8 GPUs
