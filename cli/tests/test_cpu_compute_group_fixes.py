"""Tests for CPU compute group discovery, filtering, and selection fixes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from inspire.cli.commands.init.discover import _merge_compute_groups
from inspire.cli.commands.notebook.notebook_create_flow import (
    _match_cpu_only_compute_group,
    resolve_notebook_compute_group,
)


# ---------------------------------------------------------------------------
# _merge_compute_groups: workspace_ids union
# ---------------------------------------------------------------------------


def test_merge_compute_groups_unions_workspace_ids() -> None:
    existing = [{"id": "g1", "name": "CPU", "workspace_ids": ["ws-a"]}]
    discovered = [{"id": "g1", "name": "CPU", "workspace_ids": ["ws-b"]}]
    result = _merge_compute_groups(existing, discovered)
    assert len(result) == 1
    assert result[0]["workspace_ids"] == ["ws-a", "ws-b"]


def test_merge_compute_groups_deduplicates_workspace_ids() -> None:
    existing = [{"id": "g1", "name": "CPU", "workspace_ids": ["ws-a", "ws-b"]}]
    discovered = [{"id": "g1", "name": "CPU", "workspace_ids": ["ws-b", "ws-c"]}]
    result = _merge_compute_groups(existing, discovered)
    assert result[0]["workspace_ids"] == ["ws-a", "ws-b", "ws-c"]


def test_merge_compute_groups_existing_has_no_workspace_ids() -> None:
    existing = [{"id": "g1", "name": "CPU"}]
    discovered = [{"id": "g1", "name": "CPU", "workspace_ids": ["ws-a"]}]
    result = _merge_compute_groups(existing, discovered)
    assert result[0]["workspace_ids"] == ["ws-a"]


# ---------------------------------------------------------------------------
# _config_compute_groups_fallback: workspace filtering
# ---------------------------------------------------------------------------


def test_config_compute_groups_fallback_filters_by_workspace() -> None:
    from inspire.platform.web.browser_api.notebooks import _config_compute_groups_fallback

    class FakeConfig:
        compute_groups = [
            {"id": "g1", "name": "GPU Group", "gpu_type": "H200", "workspace_ids": ["ws-gpu"]},
            {"id": "g2", "name": "CPU Group", "gpu_type": "", "workspace_ids": ["ws-cpu"]},
            {
                "id": "g3",
                "name": "Shared",
                "gpu_type": "A100",
                "workspace_ids": ["ws-gpu", "ws-cpu"],
            },
        ]

    with patch(
        "inspire.platform.web.browser_api.notebooks.Config.from_files_and_env",
        return_value=(FakeConfig(), {}),
    ):
        result = _config_compute_groups_fallback(workspace_id="ws-cpu")

    names = [g["name"] for g in result]
    assert "GPU Group" not in names
    assert "CPU Group" in names
    assert "Shared" in names


def test_config_compute_groups_fallback_no_filter_when_no_workspace() -> None:
    from inspire.platform.web.browser_api.notebooks import _config_compute_groups_fallback

    class FakeConfig:
        compute_groups = [
            {"id": "g1", "name": "GPU Group", "gpu_type": "H200", "workspace_ids": ["ws-gpu"]},
            {"id": "g2", "name": "CPU Group", "gpu_type": ""},
        ]

    with patch(
        "inspire.platform.web.browser_api.notebooks.Config.from_files_and_env",
        return_value=(FakeConfig(), {}),
    ):
        result = _config_compute_groups_fallback(workspace_id=None)

    assert len(result) == 2


def test_config_compute_groups_fallback_no_filter_when_group_has_no_workspace_ids() -> None:
    from inspire.platform.web.browser_api.notebooks import _config_compute_groups_fallback

    class FakeConfig:
        compute_groups = [
            {"id": "g1", "name": "CPU Group", "gpu_type": ""},
        ]

    with patch(
        "inspire.platform.web.browser_api.notebooks.Config.from_files_and_env",
        return_value=(FakeConfig(), {}),
    ):
        result = _config_compute_groups_fallback(workspace_id="ws-any")

    assert len(result) == 1


# ---------------------------------------------------------------------------
# _match_cpu_only_compute_group: prefer CPU-named groups
# ---------------------------------------------------------------------------


def test_match_cpu_only_prefers_cpu_resource_2() -> None:
    groups = [
        {"name": "语音项目测试专用", "gpu_type_stats": []},
        {"name": "CPU资源", "gpu_type_stats": []},
        {"name": "CPU资源-2", "gpu_type_stats": []},
    ]
    group, _ = _match_cpu_only_compute_group(groups)
    assert group is not None
    assert group["name"] == "CPU资源-2"


def test_match_cpu_only_falls_back_to_first_non_gpu() -> None:
    groups = [
        {"name": "语音项目测试专用", "gpu_type_stats": []},
        {"name": "其他资源", "gpu_type_stats": []},
    ]
    group, _ = _match_cpu_only_compute_group(groups)
    assert group is not None
    assert group["name"] == "语音项目测试专用"


def test_match_cpu_only_skips_gpu_groups() -> None:
    groups = [
        {"name": "GPU-H200", "gpu_type_stats": [{"gpu_info": {"gpu_type": "H200"}}]},
        {"name": "CPU资源", "gpu_type_stats": []},
        {"name": "HPC-可上网区资源-2", "gpu_type_stats": []},
    ]
    group, _ = _match_cpu_only_compute_group(groups)
    assert group is not None
    assert group["name"] == "HPC-可上网区资源-2"


def test_match_cpu_only_returns_none_when_all_gpu() -> None:
    groups = [
        {"name": "GPU-H200", "gpu_type_stats": [{"gpu_info": {"gpu_type": "H200"}}]},
    ]
    group, _ = _match_cpu_only_compute_group(groups)
    assert group is None


def test_resolve_notebook_compute_group_cpu_uses_cpu_selector(monkeypatch) -> None:  # noqa: ANN001
    from inspire.cli.commands.notebook import notebook_create_flow as flow_module

    monkeypatch.setattr(
        flow_module,
        "_auto_select_compute_group",
        lambda *args, **kwargs: (None, "", "CPU"),
    )
    monkeypatch.setattr(
        flow_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"name": "CPU资源", "logic_compute_group_id": "lcg-cpu"},
            {"name": "CPU资源-2", "logic_compute_group_id": "lcg-cpu-2"},
        ],
    )

    # CPU path should not call generic GPU-type matching.
    monkeypatch.setattr(
        flow_module,
        "_match_compute_group_by_gpu_type",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        flow_module,
        "_match_cpu_only_compute_group",
        lambda *args, **kwargs: (
            {"name": "CPU资源-2", "logic_compute_group_id": "lcg-cpu-2"},
            "",
        ),
    )

    result = resolve_notebook_compute_group(
        SimpleNamespace(json_output=False),
        session=object(),
        workspace_id="ws-cpu",
        gpu_count=0,
        gpu_pattern="CPU",
        requested_cpu_count=4,
        auto=False,
        json_output=False,
    )
    assert result is not None
    logic_compute_group_id, _, _, _ = result
    assert logic_compute_group_id == "lcg-cpu-2"
