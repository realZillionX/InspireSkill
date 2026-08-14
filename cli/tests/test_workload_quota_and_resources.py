from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main


_FORBIDDEN_PUBLIC_KEYS = {
    "id",
    "workspace_id",
    "logic_compute_group_id",
    "quota_id",
    "raw",
    "payload",
    "result",
    "scanned",
    "source",
}


def _json_data(output: str):  # type: ignore[no-untyped-def]
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _assert_compact_public_payload(value):  # type: ignore[no-untyped-def]
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in _FORBIDDEN_PUBLIC_KEYS
            assert not key.endswith("_id")
            assert not key.endswith("_ids")
            _assert_compact_public_payload(child)
    elif isinstance(value, list):
        for child in value:
            _assert_compact_public_payload(child)


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


_WS_DEFAULT = "ws-00000000-0000-0000-0000-0000000000aa"
_WS_CPU = "ws-22222222-2222-2222-2222-222222222222"
_WS_TRAIN = "ws-11111111-1111-1111-1111-111111111111"


class _Session:
    workspace_id = _WS_DEFAULT
    all_workspace_ids = [_WS_DEFAULT, _WS_CPU, _WS_TRAIN]
    all_workspace_names = {
        _WS_DEFAULT: "Default WS",
        _WS_CPU: "CPU资源空间",
        _WS_TRAIN: "分布式训练空间",
    }


def _stub_quota_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups_by_ws: dict[str, list[dict]],
    prices_fn,
    schedule_config_specs_fn=None,
    group_node_gpu_type_fn=None,
) -> None:
    from inspire.cli.commands import workload_quota as quota_module

    monkeypatch.setattr(quota_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        quota_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: groups_by_ws.get(kwargs["workspace_id"], []),
    )
    monkeypatch.setattr(quota_module.browser_api_module, "get_resource_prices", prices_fn)
    # Train quota reads from GetScheduleConfig (workspace-level spec menu) and
    # back-fills blank gpu_type from the group's nodes; stub both at the same
    # platform boundary so no test ever hits the network.
    if schedule_config_specs_fn is None:
        schedule_config_specs_fn = lambda **kwargs: []
    monkeypatch.setattr(
        quota_module.browser_api_module,
        "get_schedule_config_specs",
        schedule_config_specs_fn,
    )
    if group_node_gpu_type_fn is None:
        group_node_gpu_type_fn = lambda *args, **kwargs: ""
    monkeypatch.setattr(
        quota_module.browser_api_module,
        "get_group_node_gpu_type",
        group_node_gpu_type_fn,
    )


def _make_price(*, qid: str, gpu: int, cpu: int, mem: int, gpu_type: str = "") -> dict:
    return {
        "quota_id": qid,
        "cpu_count": cpu,
        "memory_size_gib": mem,
        "gpu_count": gpu,
        "gpu_info": {"gpu_type_display": gpu_type or "CPU"},
    }


def test_job_quota_workspace_all_sweeps_visible_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    queried_workspaces: list[str] = []

    def list_groups(**kwargs):
        queried_workspaces.append(kwargs["workspace_id"])
        return []

    from inspire.cli.commands import workload_quota as quota_module

    monkeypatch.setattr(quota_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        quota_module.browser_api_module, "list_notebook_compute_groups", list_groups
    )
    monkeypatch.setattr(quota_module.browser_api_module, "get_resource_prices", lambda **_: [])

    result = CliRunner().invoke(cli_main, ["--json", "job", "quota", "--workspace", "all"])
    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {"items": []}
    assert "workload" not in payload
    assert "total" not in payload
    assert "workspace_names" not in payload
    _assert_compact_public_payload(payload)
    assert sorted(queried_workspaces) == sorted([_WS_DEFAULT, _WS_CPU, _WS_TRAIN])


def test_quota_requires_explicit_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main, ["job", "quota"])
    assert result.exit_code != 0
    assert "Missing option '--workspace'" in result.output


def test_each_workload_quota_uses_its_schedule_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    # Only hpc and ray still hit the per-group v1 ``schedule_config_type``
    # endpoint; notebook / job / serving read the workspace-level
    # GetScheduleConfig menus (``quota`` / ``predef_train_spec`` /
    # ``serving_quota`` respectively).
    expected = {
        "hpc": "SCHEDULE_CONFIG_TYPE_HPC",
        "ray": "SCHEDULE_CONFIG_TYPE_RAY_JOB",
    }
    seen: dict[str, str] = {}
    schedule_config_fields: dict[str, str] = {}

    def prices(**kwargs):
        seen[current_workload] = kwargs["schedule_config_type"]
        return [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)]

    def schedule_config_specs(**kwargs):
        schedule_config_fields[current_workload] = kwargs["spec_field"]
        return []

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-1", "name": "CPU资源-2"}]},
        prices_fn=prices,
        schedule_config_specs_fn=schedule_config_specs,
    )
    workloads = ("notebook", "job", "serving", "hpc", "ray")
    for current_workload in workloads:
        result = CliRunner().invoke(
            cli_main,
            ["--json", current_workload, "quota", "--workspace", "CPU资源空间"],
        )
        assert result.exit_code == 0, result.output
    assert seen == expected
    # Notebook / job / serving ask GetScheduleConfig for their menu field
    # instead of the per-group v1 schedule_config_type endpoint.
    assert schedule_config_fields == {
        "notebook": "quota",
        "job": "predef_train_spec",
        "serving": "serving_quota",
    }


def test_train_quota_reads_workspace_schedule_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Job quota flattens the workspace-level spec menu onto candidate groups.

    ``predef_train_spec`` declares group ownership via ``logic_compute_group_ids``;
    an empty that list means the spec is open to every group, and a blank
    ``gpu_type`` is back-filled from the group's nodes (the authoritative
    hardware source). Train groups the v1 endpoint used to hide — the ones the
    user can submit to but that currently have no allocatable capacity — must
    appear.
    """
    _patch_config(monkeypatch, tmp_path)

    specs = [
        # 8卡 whole-node quota belongs to the three 训练区 groups; lcg-full-N
        # has no matching spec row. No allowed_priority_levels → unrestricted.
        {
            "id": "q-8",
            "cpu_count": 160,
            "memory_size": 1800,
            "gpu_count": 8,
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "logic_compute_group_ids": ["lcg-full-1", "lcg-full-2", "lcg-full-3"],
        },
        # Partial-node quotas in 训练区 are low-only (QZ policy, mirrored
        # verbatim by GetScheduleConfig in allowed_priority_levels).
        {
            "id": "q-1",
            "cpu_count": 20,
            "memory_size": 200,
            "gpu_count": 1,
            "gpu_type": "",  # node's gpu_info fills this in
            "logic_compute_group_ids": ["lcg-full-1", "lcg-full-2"],
            "allowed_priority_levels": ["low"],
        },
        # A partial-node quota in 开发区 with no priority restriction covers
        # the remaining groups (empty ownership array = every group).
        {
            "id": "q-1-dev",
            "cpu_count": 20,
            "memory_size": 200,
            "gpu_count": 1,
            "gpu_type": "",
            "logic_compute_group_ids": [],
        },
    ]

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={
            _WS_TRAIN: [
                {"logic_compute_group_id": "lcg-full-1", "name": "训练区-H200-1号机房"},
                {"logic_compute_group_id": "lcg-full-2", "name": "训练区-H200-3号机房-2-cuda12.8"},
                {"logic_compute_group_id": "lcg-full-3", "name": "开发区-H200-3号机房"},
                # A group the spec menu does not cover at all — it's still a
                # valid candidate in the workspace, just with an empty catalog.
                {"logic_compute_group_id": "lcg-full-N", "name": "开发区-H200-N号机房"},
            ],
        },
        prices_fn=lambda **_: [],
        schedule_config_specs_fn=lambda **_: specs,
        group_node_gpu_type_fn=lambda *args, **kw: "NVIDIA_H100_80GB",
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "job", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    by_group: dict[str, set[str]] = {}
    for row in payload["items"]:
        by_group.setdefault(row["compute_group"], set()).add(row["quota"])
    assert by_group["训练区-H200-1号机房"] == {"8,160,1800", "1,20,200"}
    assert by_group["训练区-H200-3号机房-2-cuda12.8"] == {"8,160,1800", "1,20,200"}
    assert by_group["开发区-H200-3号机房"] == {"8,160,1800", "1,20,200"}
    # Group without any matching spec row answers empty, not hidden.
    assert by_group["开发区-H200-N号机房"] == {"1,20,200"}
    # Empty ownership marks unrestricted; "low" marks low-only.
    priority_by_group: dict[str, set[str]] = {}
    for row in payload["items"]:
        priority_by_group.setdefault(row["compute_group"], set()).add(
            row["allowed_priority"]
        )
    assert priority_by_group["训练区-H200-1号机房"] == {"", "low"}
    assert priority_by_group["训练区-H200-3号机房-2-cuda12.8"] == {"", "low"}
    assert priority_by_group["开发区-H200-3号机房"] == {""}
    assert priority_by_group["开发区-H200-N号机房"] == {""}


def test_quota_json_rows_carry_quota_and_no_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    # Notebook quota reads from ``GetScheduleConfig.quota`` — the v1
    # prices_fn has no say here. Use the schedule-config fixture instead,
    # with an explicit ownership row so the row lands on lcg-secret.
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-secret", "name": "CPU资源-2"}]},
        prices_fn=lambda **_: [],
        schedule_config_specs_fn=lambda **_: [
            {
                "id": "q-1",
                "cpu_count": 4,
                "memory_size": 16,
                "gpu_count": 0,
                "gpu_type": "",
                "logic_compute_group_ids": ["lcg-secret"],
            }
        ],
    )
    result = CliRunner().invoke(
        cli_main, ["--json", "notebook", "quota", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    row = payload["items"][0]
    assert row.keys() == {
        "workspace",
        "compute_group",
        "gpu_type",
        "quota",
        "allowed_priority",
    }
    assert row["workspace"] == "CPU资源空间"
    assert row["compute_group"] == "CPU资源-2"
    assert row["quota"] == "0,4,16"
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert "lcg-secret" not in result.output
    assert "q-1" not in result.output


def test_qz_quota_human_output_explains_scheduling_zones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    # 开发区 carries a partial-node H100 quota and a full-node H100 quota; the
    # 训练区 group owns the 8卡 H200 whole-node quota. Ownership comes from
    # ``logic_compute_group_ids`` on each spec row, not from per-group prices.
    specs = [
        {
            "id": "q-dev-4",
            "cpu_count": 55,
            "memory_size": 900,
            "gpu_count": 4,
            "gpu_type": "NVIDIA_H100_80GB",
            "logic_compute_group_ids": ["lcg-dev"],
        },
        {
            "id": "q-dev-8",
            "cpu_count": 110,
            "memory_size": 1800,
            "gpu_count": 8,
            "gpu_type": "NVIDIA_H100_80GB",
            "logic_compute_group_ids": ["lcg-dev"],
        },
        {
            "id": "q-train",
            "cpu_count": 160,
            "memory_size": 1800,
            "gpu_count": 8,
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "logic_compute_group_ids": ["lcg-train"],
        },
    ]

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={
            _WS_TRAIN: [
                {
                    "logic_compute_group_id": "lcg-dev",
                    "name": "开发区-H100-cuda12.8版本-119核",
                },
                {"logic_compute_group_id": "lcg-train", "name": "训练区-H200-1号机房"},
            ]
        },
        prices_fn=lambda **_: [],
        schedule_config_specs_fn=lambda **_: specs,
    )

    result = CliRunner().invoke(
        cli_main, ["job", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "QZ scheduling zones:" in result.output
    assert "开发区 supports both full-node and partial-node GPU workloads" in result.output
    assert "训练区 prioritizes full-node workloads" in result.output
    assert "partial-node GPU workloads there require LOW priority" in result.output
    assert "1 in fair-scheduling workspaces, preemptible" in result.output
    assert "per instance/node quota, not aggregate GPU count" in result.output
    assert "Use --group and --quota from the same live quota row" in result.output


def test_qz_quota_hint_is_human_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_TRAIN: [{"logic_compute_group_id": "lcg-dev", "name": "开发区-H200"}]},
        prices_fn=lambda **_: [],
        schedule_config_specs_fn=lambda **_: [
            {
                "id": "q-small",
                "cpu_count": 20,
                "memory_size": 200,
                "gpu_count": 1,
                "gpu_type": "NVIDIA H200",
                "logic_compute_group_ids": ["lcg-dev"],
            }
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "notebook", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    row = payload["items"][0]
    assert row.keys() == {
        "workspace",
        "compute_group",
        "gpu_type",
        "quota",
        "allowed_priority",
    }
    assert "QZ scheduling zones" not in result.output
    _assert_compact_public_payload(payload)


def test_non_qz_quota_human_output_has_no_card_area_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-cpu", "name": "CPU资源-2"}]},
        prices_fn=lambda **_: [],
        schedule_config_specs_fn=lambda **_: [
            {
                "id": "q-cpu",
                "cpu_count": 20,
                "memory_size": 80,
                "gpu_count": 0,
                "gpu_type": "",
                "logic_compute_group_ids": ["lcg-cpu"],
            }
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["notebook", "quota", "--workspace", "CPU资源空间"]
    )

    assert result.exit_code == 0, result.output
    assert "QZ scheduling zones" not in result.output


def test_group_keyword_filter_skips_non_matching_compute_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    queried_groups: list[str] = []

    def prices(**kwargs):
        queried_groups.append(kwargs["logic_compute_group_id"])
        return [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)]

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={
            _WS_CPU: [
                {"logic_compute_group_id": "lcg-cpu-1", "name": "CPU资源-1"},
                {"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"},
                {"logic_compute_group_id": "lcg-hpc-2", "name": "HPC-可上网区资源-2"},
            ]
        },
        prices_fn=prices,
    )
    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "quota", "--workspace", "CPU资源空间", "--group", "HPC"],
    )
    assert result.exit_code == 0, result.output
    assert queried_groups == ["lcg-hpc-2"]


@pytest.mark.parametrize(
    "group_handle",
    [
        "lcg-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ],
)
def test_group_filter_rejects_platform_handles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    group_handle: str,
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={
            _WS_CPU: [
                {
                    "logic_compute_group_id": "lcg-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "name": "CPU资源-2",
                }
            ]
        },
        prices_fn=lambda **_: [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "quota",
            "--workspace",
            "CPU资源空间",
            "--group",
            group_handle,
        ],
    )

    assert result.exit_code != 0
    assert "takes a compute group name" in result.output
    assert group_handle not in result.output


def test_quota_help_explains_group_keyword() -> None:
    result = CliRunner().invoke(cli_main, ["job", "quota", "--help"])
    output = " ".join(result.output.split())
    assert result.exit_code == 0
    assert "--workspace NAME|all" in result.output
    assert "compute group name keyword/substring" in output
    assert "full name is not required" in output


@pytest.mark.parametrize(
    "workload",
    ("notebook", "job", "hpc", "ray", "serving"),
)
def test_each_workload_quota_uses_workspace_name_or_all_metavar(
    workload: str,
) -> None:
    result = CliRunner().invoke(cli_main, [workload, "quota", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "--workspace TEXT" not in result.output


@pytest.mark.parametrize("command", ("availability", "nodes"))
def test_resource_queries_use_workspace_name_or_all_metavar(command: str) -> None:
    result = CliRunner().invoke(cli_main, ["resources", command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "--workspace TEXT" not in result.output


def test_resources_availability_human_hides_raw_group_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from inspire.cli.commands.resources import resources_list as list_module
    from inspire.platform.web.browser_api.availability.models import GPUAvailability

    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(list_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        list_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id="lcg-secret-raw-id",
                group_name="中文资源组",
                gpu_type="H200",
                total_gpus=16,
                used_gpus=16,
                available_gpus=0,
                low_priority_gpus=4,
                workspace_id=_WS_CPU,
                workspace_name="CPU资源空间",
            )
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["resources", "availability", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0, result.output
    assert "中文资源组" in result.output
    assert "Available" in result.output
    assert "Reclaimable" in result.output
    assert "Usage:" not in result.output
    assert "Legend:" not in result.output
    assert "lcg-secret-raw-id" not in result.output

    json_result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "resources",
            "availability",
            "--workspace",
            "CPU资源空间",
        ],
    )
    assert json_result.exit_code == 0, json_result.output
    row = _json_data(json_result.output)["items"][0]
    assert row["compute_group"] == "中文资源组"
    assert "group" not in row
    _assert_compact_public_payload(row)


def test_availability_uses_workspace_actions_for_groups_and_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api.availability import api as availability_api

    calls: list[str] = []

    class _AvailabilitySession:
        workspace_id = _WS_CPU
        all_workspace_ids = [_WS_CPU]
        all_workspace_names = {_WS_CPU: "CPU资源空间"}

    def fake_request(session, method, path, *, referer, body=None, timeout=30):
        calls.append(path)
        if path.endswith("Action=ListLogicComputeGroups"):
            return {
                "Result": {
                    "logic_compute_groups": [
                        {
                            "logic_compute_group_id": "lcg-live",
                            "name": "实时资源组",
                        }
                    ]
                },
            }
        if path.endswith("Action=GetLogicComputeGroupResource"):
            return {
                "Result": {
                    "logic_resouces": {
                        "gpu_total": 16,
                        "gpu_used": 4,
                        "gpu_low_priority_used": 1,
                        "cpu_total": 80,
                        "cpu_used": 20,
                        "memory_gi_total": 800,
                        "memory_gi_used": 200,
                    },
                    "gpu_type_stats": [{"gpu_info": {"gpu_type_display": "H200"}}],
                },
            }
        if path.endswith("Action=ListNodeDimension"):
            # ListNodeDimension nests the GPU counts under `gpu`.
            return {
                "Result": {
                    "total": "2",
                    "node_dimensions": [
                        {
                            "gpu": {"total": 8, "used": 0},
                            "status": "READY",
                            "task_list": [],
                            "resource_pool": "online",
                        },
                        {
                            "gpu": {"total": 8, "used": 8},
                            "status": "READY",
                            "task_list": [{"name": "busy"}],
                            "resource_pool": "online",
                        },
                    ],
                },
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(availability_api, "_request_json", fake_request)
    rows = availability_api.get_accurate_resource_availability(
        workspace_id=_WS_CPU,
        session=_AvailabilitySession(),
        include_cpu=False,
    )

    assert [row.group_name for row in rows] == ["实时资源组"]
    assert rows[0].available_gpus == 12
    assert rows[0].ready_nodes == 2
    assert rows[0].free_nodes == 1
    assert any(path.endswith("Action=ListLogicComputeGroups") for path in calls)
    assert any(path.endswith("Action=ListNodeDimension") for path in calls)
    # Nothing should touch /api/v1 in this path any more.
    assert not any("/api/v1" in path for path in calls)
