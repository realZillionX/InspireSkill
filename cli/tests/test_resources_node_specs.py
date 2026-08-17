from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import FullFreeNodeCount, GPUAvailability, NodeSpec
from inspire.platform.web.browser_api.availability import api
from inspire.platform.web.session import TransientAPIError

_WS = "ws-00000000-0000-0000-0000-0000000000aa"
_GROUP = "cg-11111111-1111-1111-1111-111111111111"


class _Session:
    workspace_id = _WS
    all_workspace_ids = [_WS]
    all_workspace_names = {_WS: "Default WS"}


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _spec_row(
    *,
    cpu: float,
    memory: float,
    gpu: int = 8,
    job_type: str = "distributed_training",
) -> dict:
    return {
        "cpu_count": cpu,
        "memory_size": memory,
        "gpu_count": gpu,
        # Live rows leave the flat fields blank; the model name is nested.
        "gpu_type": "",
        "gpu_memory_size": 0,
        "gpu_info": {
            "gpu_product_simple": "H200",
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "gpu_type_display": "NVIDIA H200 (141GB)",
        },
        "cpu_info": {"cpu_type": ""},
        "node_type": "gpu",
        "support_job_type": job_type,
    }


# --- wrapper ---------------------------------------------------------------


def test_group_node_specs_scope_at_the_top_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """`filter` nesting is rejected here, unlike the dimension Actions."""
    seen: list[tuple[str, dict]] = []

    def _fake(_session, _method, path, *, referer, body, timeout):
        seen.append((path, body))
        return {"ResponseMetadata": {}, "Result": {"node_specs": []}}

    monkeypatch.setattr(api, "_request_json", _fake)

    api.list_node_specs("ws-1", logic_compute_group_id="lcg-1", session=object())  # type: ignore[arg-type]

    path, body = seen[0]
    assert "Action=GetLogicComputeGroupNodeSpecs" in path
    assert body == {"workspace_id": "ws-1", "logic_compute_group_id": "lcg-1"}
    assert "filter" not in body


def test_workspace_node_specs_use_the_workspace_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict]] = []

    def _fake(_session, _method, path, *, referer, body, timeout):
        seen.append((path, body))
        return {"ResponseMetadata": {}, "Result": {"node_specs": []}}

    monkeypatch.setattr(api, "_request_json", _fake)

    api.list_node_specs("ws-1", session=object())  # type: ignore[arg-type]

    path, body = seen[0]
    assert "Action=GetWorkspaceNodeSpecs" in path
    assert body == {"workspace_id": "ws-1"}


def test_node_specs_fold_job_type_and_fractional_memory_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform emits one row per (shape x job type) and per memory decimal."""
    rows = [
        _spec_row(cpu=183, memory=1888.17, job_type="distributed_training"),
        _spec_row(cpu=183, memory=1888.26, job_type="distributed_training"),
        _spec_row(cpu=183, memory=1888.17, job_type="interactive_modeling"),
        _spec_row(cpu=183, memory=1888.26, job_type="interactive_modeling"),
        _spec_row(cpu=119, memory=1888.20, job_type="distributed_training"),
    ]
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_a, **_k: {"ResponseMetadata": {}, "Result": {"node_specs": rows}},
    )

    specs = api.list_node_specs("ws-1", session=object())  # type: ignore[arg-type]

    assert len(specs) == 2
    assert specs[0] == NodeSpec(
        node_type="gpu",
        gpu_type="H200",
        gpu_count=8,
        cpu_count=183.0,
        memory_gib=1888.0,
        job_types=("distributed_training", "interactive_modeling"),
    )
    assert specs[1].cpu_count == 119.0
    assert specs[0].label == "H200x8 183C 1888G"


def test_node_specs_sort_largest_shape_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _spec_row(cpu=183, memory=1888.0, gpu=0),
        _spec_row(cpu=183, memory=1888.0, gpu=7),
        _spec_row(cpu=183, memory=1888.0, gpu=8),
    ]
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_a, **_k: {"ResponseMetadata": {}, "Result": {"node_specs": rows}},
    )

    specs = api.list_node_specs("ws-1", session=object())  # type: ignore[arg-type]

    assert [spec.gpu_count for spec in specs] == [8, 7, 0]


def test_node_specs_return_empty_only_for_a_successful_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_a, **_k: {"ResponseMetadata": {}, "Result": {"node_specs": []}},
    )
    assert api.list_node_specs("ws-1", session=object()) == []  # type: ignore[arg-type]

    def _boom(*_args, **_kwargs):
        raise TransientAPIError("throttled")

    monkeypatch.setattr(api, "_request_json", _boom)
    with pytest.raises(TransientAPIError):
        api.list_node_specs("ws-1", session=object())  # type: ignore[arg-type]


def test_node_specs_surface_an_access_denial_instead_of_no_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_a, **_k: {
            "ResponseMetadata": {
                "Error": {"Code": "AccessForbidden", "Message": "Access denied"}
            }
        },
    )

    with pytest.raises(ValueError, match="AccessForbidden"):
        api.list_node_specs("ws-1", logic_compute_group_id="lcg-1", session=object())  # type: ignore[arg-type]


# --- command ---------------------------------------------------------------


def _patch_nodes_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    specs: list[NodeSpec],
) -> list[tuple[str, str]]:
    from inspire.cli.commands.resources import resources_nodes as nodes_module

    _patch_config(monkeypatch)
    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id=_GROUP,
                group_name="H200-1号机房",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=16,
                available_gpus=48,
                low_priority_gpus=0,
                workspace_id=_WS,
                workspace_name="Default WS",
            )
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node, **_kwargs: [
            FullFreeNodeCount(
                group_id=_GROUP,
                group_name="H200-1号机房",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=6,
            )
        ],
    )

    calls: list[tuple[str, str]] = []

    def _list_node_specs(workspace_id, *, logic_compute_group_id=None, **_kwargs):
        calls.append((workspace_id, logic_compute_group_id))
        return specs

    monkeypatch.setattr(
        nodes_module.browser_api_module, "list_node_specs", _list_node_specs
    )
    return calls


def test_resources_nodes_reports_the_largest_shape_per_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_nodes_command(
        monkeypatch,
        specs=[
            NodeSpec(
                node_type="gpu",
                gpu_type="H200",
                gpu_count=8,
                cpu_count=183.0,
                memory_gib=1888.0,
                job_types=("distributed_training",),
            ),
            NodeSpec(
                node_type="gpu",
                gpu_type="H200",
                gpu_count=8,
                cpu_count=119.0,
                memory_gib=1888.0,
                job_types=("distributed_training",),
            ),
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["resources", "nodes", "--workspace", "Default WS"]
    )

    assert result.exit_code == 0, result.output
    assert "Node Spec" in result.output
    assert "H200x8 183C 1888G" in result.output
    # Specs are asked for per group, scoped to that group's own workspace.
    assert calls == [(_WS, _GROUP)]


def test_resources_nodes_json_carries_every_distinct_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nodes_command(
        monkeypatch,
        specs=[
            NodeSpec(
                node_type="gpu",
                gpu_type="H200",
                gpu_count=8,
                cpu_count=183.0,
                memory_gib=1888.0,
                job_types=("distributed_training", "interactive_modeling"),
            ),
            NodeSpec(
                node_type="gpu",
                gpu_type="H200",
                gpu_count=7,
                cpu_count=183.0,
                memory_gib=1888.0,
                job_types=("distributed_training",),
            ),
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "nodes", "--workspace", "Default WS"]
    )

    assert result.exit_code == 0, result.output
    row = json.loads(result.output)["data"]["items"][0]
    assert [spec["gpu_count"] for spec in row["node_specs"]] == [8, 7]
    assert row["node_specs"][0]["job_types"] == [
        "distributed_training",
        "interactive_modeling",
    ]
    # A shape count is not a node count and must never be published as one.
    assert "node_count" not in row["node_specs"][0]
    assert _GROUP not in result.output


def test_resources_nodes_shows_a_placeholder_when_no_shape_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nodes_command(monkeypatch, specs=[])

    result = CliRunner().invoke(
        cli_main, ["resources", "nodes", "--workspace", "Default WS"]
    )

    assert result.exit_code == 0, result.output
    assert "Node Spec" in result.output
    assert "H200-1号机房" in result.output
