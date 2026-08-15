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


class _UnrestrictedMenu(dict):
    """A workspace that published every spec, none of them restricted.

    The default for quota tests that are not about priority at all, so their
    rows read `any` instead of the `unknown` a missing stub would produce.
    """

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        return ()


def _stub_quota_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups_by_ws: dict[str, list[dict]],
    prices_fn,
    priority_levels_fn=None,
) -> None:
    from inspire.cli.commands import workload_quota as quota_module

    monkeypatch.setattr(quota_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        quota_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: groups_by_ws.get(kwargs["workspace_id"], []),
    )
    monkeypatch.setattr(quota_module.browser_api_module, "get_resource_prices", prices_fn)
    monkeypatch.setattr(
        quota_module.browser_api_module,
        "get_quota_priority_levels",
        priority_levels_fn or (lambda **_kwargs: _UnrestrictedMenu()),
        raising=False,
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


def test_quota_reports_rate_limiting_instead_of_no_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`No quota rows found.` is a claim about the workspace, not about the API."""
    from inspire.platform.web.session import TransientAPIError

    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_TRAIN: [{"logic_compute_group_id": "lcg-a", "name": "H200"}]},
        prices_fn=lambda **_kwargs: (_ for _ in ()).throw(
            TransientAPIError("API returned 429: Too Many Requests", status=429)
        ),
    )

    result = CliRunner().invoke(
        cli_main, ["job", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code != 0
    assert "No quota rows found" not in result.output
    assert "429" in result.output


def test_quota_requires_explicit_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main, ["job", "quota"])
    assert result.exit_code != 0
    assert "Missing option '--workspace'" in result.output


def test_each_workload_quota_uses_its_schedule_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    expected = {
        "notebook": "SCHEDULE_CONFIG_TYPE_DSW",
        "job": "SCHEDULE_CONFIG_TYPE_TRAIN",
        "serving": "SCHEDULE_CONFIG_TYPE_SERVE",
        "hpc": "SCHEDULE_CONFIG_TYPE_HPC",
        "ray": "SCHEDULE_CONFIG_TYPE_RAY_JOB",
    }
    seen: dict[str, str] = {}

    def prices(**kwargs):
        seen[current_workload] = kwargs["schedule_config_type"]
        return [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)]

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-1", "name": "CPU资源-2"}]},
        prices_fn=prices,
    )
    for current_workload in expected:
        result = CliRunner().invoke(
            cli_main,
            ["--json", current_workload, "quota", "--workspace", "CPU资源空间"],
        )
        assert result.exit_code == 0, result.output
    assert seen == expected


def test_quota_json_rows_carry_quota_and_no_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-secret", "name": "CPU资源-2"}]},
        prices_fn=lambda **_: [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)],
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
        "priority",
        "allowed_priority_levels",
    }
    assert row["workspace"] == "CPU资源空间"
    assert row["compute_group"] == "CPU资源-2"
    assert row["quota"] == "0,4,16"
    assert row["priority"] == "any"
    assert row["allowed_priority_levels"] == []
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert "lcg-secret" not in result.output
    assert "q-1" not in result.output


def _stub_restricted_train_zone(monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
    """One workspace shaped like the live 分布式训练空间.

    The training-zone group offers the same four shapes as the dev-zone one,
    but the platform publishes its three partial-card specs as low-priority
    only. That difference is invisible in the compute group name, the GPU type
    and the triple -- it exists only in the workspace's scheduling record.
    """

    def prices(**price_kwargs):
        shapes = [(1, 20, 200), (2, 40, 400), (4, 80, 900), (8, 160, 1800)]
        zone = "dev" if price_kwargs["logic_compute_group_id"] == "lcg-dev" else "train"
        return [
            _make_price(qid=f"q-{zone}-{gpu}", gpu=gpu, cpu=cpu, mem=mem, gpu_type="H200")
            for gpu, cpu, mem in shapes
        ]

    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={
            _WS_TRAIN: [
                {"logic_compute_group_id": "lcg-dev", "name": "开发区-H200-3号机房"},
                {"logic_compute_group_id": "lcg-train", "name": "训练区-H200-1号机房"},
            ]
        },
        prices_fn=prices,
        **kwargs,
    )


_TRAIN_ZONE_MENU = {
    "q-train-1": ("low",),
    "q-train-2": ("low",),
    "q-train-4": ("low",),
    "q-train-8": (),
    "q-dev-1": (),
    "q-dev-2": (),
    "q-dev-4": (),
    "q-dev-8": (),
}


def test_quota_human_output_shows_the_published_priority_per_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_restricted_train_zone(
        monkeypatch, priority_levels_fn=lambda **_kwargs: dict(_TRAIN_ZONE_MENU)
    )

    result = CliRunner().invoke(
        cli_main, ["job", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "Priority" in result.output
    rows = {
        (parts[0], parts[2]): parts[3]
        for parts in (line.split() for line in result.output.splitlines())
        if len(parts) == 4 and "," in parts[2]
    }
    assert rows[("训练区-H200-1号机房", "1,20,200")] == "low"
    assert rows[("训练区-H200-1号机房", "2,40,400")] == "low"
    assert rows[("训练区-H200-1号机房", "4,80,900")] == "low"
    assert rows[("训练区-H200-1号机房", "8,160,1800")] == "any"
    assert rows[("开发区-H200-3号机房", "1,20,200")] == "any"
    # The old hardcoded hint inferred all of this from the group name.
    assert "QZ scheduling zones" not in result.output


def test_quota_json_carries_the_priority_restriction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_restricted_train_zone(
        monkeypatch, priority_levels_fn=lambda **_kwargs: dict(_TRAIN_ZONE_MENU)
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "job", "quota", "--workspace", "分布式训练空间", "--all"]
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    by_row = {
        (row["compute_group"], row["quota"]): row for row in payload["items"]
    }
    restricted = by_row[("训练区-H200-1号机房", "1,20,200")]
    assert restricted["priority"] == "low"
    assert restricted["allowed_priority_levels"] == ["low"]
    unrestricted = by_row[("训练区-H200-1号机房", "8,160,1800")]
    assert unrestricted["priority"] == "any"
    assert unrestricted["allowed_priority_levels"] == []
    _assert_compact_public_payload(payload)


def test_quota_reports_an_unreadable_priority_menu_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A menu the platform never sent is not a menu without restrictions."""

    def _boom(**_kwargs):
        raise ValueError("API error: RateLimit: too many requests")

    _patch_config(monkeypatch, tmp_path)
    _stub_restricted_train_zone(monkeypatch, priority_levels_fn=_boom)

    result = CliRunner().invoke(
        cli_main, ["job", "quota", "--workspace", "分布式训练空间"]
    )
    json_result = CliRunner().invoke(
        cli_main, ["--json", "job", "quota", "--workspace", "分布式训练空间"]
    )

    # The rows still list -- one unreadable menu must not hide the catalog.
    assert result.exit_code == 0, result.output
    assert "训练区-H200-1号机房" in result.output
    assert "unknown" in result.output
    assert "any" not in result.output
    assert "could not be read" in result.output
    assert json_result.exit_code == 0, json_result.output
    for row in _json_data(json_result.output)["items"]:
        assert row["priority"] == "unknown"
        assert row["allowed_priority_levels"] is None


def test_quota_output_never_guesses_a_restriction_from_a_group_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_quota_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-cpu", "name": "CPU资源-2"}]},
        prices_fn=lambda **_: [_make_price(qid="q-cpu", gpu=0, cpu=20, mem=80)],
    )

    result = CliRunner().invoke(
        cli_main, ["notebook", "quota", "--workspace", "CPU资源空间"]
    )

    assert result.exit_code == 0, result.output
    assert "QZ scheduling zones" not in result.output
    assert "训练区" not in result.output


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


@pytest.mark.parametrize(
    "command", ("availability", "nodes", "quota", "policy", "usage")
)
def test_resource_queries_take_one_workspace(command: str) -> None:
    """Resource facts are per workspace, so these never fan out."""
    result = CliRunner().invoke(cli_main, ["resources", command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME" in result.output
    assert "--workspace NAME|all" not in result.output
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
