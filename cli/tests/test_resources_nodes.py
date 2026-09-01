from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import FullFreeNodeCount, GPUAvailability, NodeSpec


_WS_DEFAULT = "ws-00000000-0000-0000-0000-0000000000aa"

_NODE_SPEC = NodeSpec(
    node_type="gpu",
    gpu_type="H200",
    gpu_count=8,
    cpu_count=183.0,
    memory_gib=1888.0,
    job_types=("distributed_training",),
)


def _patch_node_specs(monkeypatch: pytest.MonkeyPatch, nodes_module) -> None:
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "list_task_usage",
        lambda _workspace_id, **_kwargs: [],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "list_node_specs",
        lambda _workspace_id, **_kwargs: [_NODE_SPEC],
    )


class _Session:
    workspace_id = _WS_DEFAULT
    all_workspace_ids = [_WS_DEFAULT]
    all_workspace_names = {_WS_DEFAULT: "Default WS"}


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = config_module.Config(
        username="user",
        password="pass",
        base_url="https://qz.sii.edu.cn",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )


def test_resources_nodes_filters_and_returns_compact_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id="cg-11111111-1111-1111-1111-111111111111",
                group_name="H200-2号机房",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=16,
                available_gpus=48,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            ),
            GPUAvailability(
                group_id="cg-22222222-2222-2222-2222-222222222222",
                group_name="H200-1号机房",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=56,
                available_gpus=8,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            ),
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node, **_kwargs: [
            FullFreeNodeCount(
                group_id="cg-11111111-1111-1111-1111-111111111111",
                group_name="H200-2号机房",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=1,
                reclaimable_nodes=1,
            ),
            FullFreeNodeCount(
                group_id="cg-22222222-2222-2222-2222-222222222222",
                group_name="H200-1号机房",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=1,
            ),
        ],
    )

    _patch_node_specs(monkeypatch, nodes_module)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "resources",
            "nodes",
            "--workspace",
            "Default WS",
            "--min-nodes",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert [row["compute_group"] for row in data["items"]] == ["H200-2号机房"]
    assert data["items"][0]["full_free_nodes"] == 1
    assert data["items"][0]["reclaimable_nodes"] == 1
    assert data["items"][0]["high_priority_free_nodes"] == 2
    assert data["items"][0]["full_free_gpus"] == 8
    assert data["items"][0]["high_priority_free_gpus"] == 16
    assert data["items"][0]["workspace"] == "Default WS"
    assert "group" not in data["items"][0]
    assert set(data) == {"items"}
    assert "group_id" not in result.output
    assert "workspace_id" not in result.output
    assert "cg-11111111" not in result.output


def test_resources_nodes_human_scrubs_raw_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    raw_group_id = "cg-11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id=raw_group_id,
                group_name=f"H200 {raw_group_id}",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=16,
                available_gpus=48,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            )
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node, **_kwargs: [
            FullFreeNodeCount(
                group_id=raw_group_id,
                group_name=f"H200 {raw_group_id}",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=6,
                reclaimable_nodes=2,
            )
        ],
    )

    _patch_node_specs(monkeypatch, nodes_module)

    result = CliRunner().invoke(
        cli_main,
        ["resources", "nodes", "--workspace", "Default WS", "--min-nodes", "2"],
    )

    assert result.exit_code == 0, result.output
    assert raw_group_id not in result.output
    assert "<raw-id>" not in result.output
    assert "cg-" not in result.output
    assert "H200" in result.output
    assert "Recommended:" not in result.output


def test_resources_nodes_rejects_group_id_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    raw_group_id = "lcg-11111111-1111-1111-1111-111111111111"

    result = CliRunner().invoke(
        cli_main,
        [
            "resources",
            "nodes",
            "--workspace",
            "Default WS",
            "--group",
            raw_group_id,
        ],
    )

    assert result.exit_code != 0
    assert "compute group name" in result.output
    assert raw_group_id not in result.output


def test_resources_nodes_reports_business_value_errors_as_api_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: (_ for _ in ()).throw(ValueError("permission denied")),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "nodes", "--workspace", "Default WS"],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["type"] == "APIError"


def test_resources_nodes_defaults_to_twenty_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id=f"cg-{index:08x}-1111-1111-1111-111111111111",
                group_name=f"Group {index:02d}",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=index,
                available_gpus=64 - index,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            )
            for index in range(25)
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node, **_kwargs: [
            FullFreeNodeCount(
                group_id=group_id,
                group_name=f"Group {index:02d}",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=25 - index,
            )
            for index, group_id in enumerate(group_ids)
        ],
    )

    _patch_node_specs(monkeypatch, nodes_module)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "nodes", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert len(data["items"]) == 20
    assert data["shown"] == 20
    assert data["total"] == 25
    assert data["truncated"] is True
