from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = config_module.Config(
        username="user",
        password="pass",
        base_url="https://qz.sii.edu.cn",
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "logs"),
    )
    cfg.workspaces = {
        "分布式训练空间": "ws-00000000-0000-0000-0000-000000000003",
    }
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )


def test_resources_specs_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"},
            {"id": "lcg-hpc-2", "name": "HPC-可上网区资源-2"},
        ],
    )

    calls: list[tuple[str, str]] = []

    def _fake_prices(**kwargs):
        gid = kwargs["logic_compute_group_id"]
        schedule = kwargs["schedule_config_type"]
        calls.append((gid, schedule))
        if schedule == "SCHEDULE_CONFIG_TYPE_HPC":
            if gid == "lcg-cpu-2":
                return [
                    {
                        "quota_id": "quota-hpc-120-500",
                        "cpu_count": 120,
                        "memory_size_gib": 500,
                        "gpu_count": 0,
                        "gpu_info": {"gpu_type_display": "CPU"},
                    }
                ]
            return [
                {
                    "quota_id": "quota-hpc-40-200",
                    "cpu_count": 40,
                    "memory_size_gib": 200,
                    "gpu_count": 0,
                    "gpu_info": {"gpu_type_display": "CPU"},
                }
            ]
        if gid == "lcg-cpu-2":
            return [
                {
                    "quota_id": "quota-cpu-55-500",
                    "cpu_count": 55,
                    "memory_size_gib": 500,
                    "gpu_count": 0,
                    "gpu_info": {"gpu_type_display": "CPU"},
                }
            ]
        return [
            {
                "quota_id": "quota-cpu-32-256",
                "cpu_count": 32,
                "memory_size_gib": 256,
                "gpu_count": 0,
                "gpu_info": {"gpu_type_display": "CPU"},
            }
        ]

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--json", "resources", "specs", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["workspace_id"] == "ws-00000000-0000-0000-0000-000000000003"
    assert payload["data"]["usage_filter"] == "auto"
    assert payload["data"]["total"] == 2
    first = payload["data"]["specs"][0]
    assert first["usage"] == "hpc"
    assert first["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_HPC"
    assert "logic_compute_group_id" in first
    assert "spec_id" in first
    assert "cpu_count" in first
    assert "memory_size_gib" in first
    assert "gpu_count" in first
    assert "gpu_type" in first
    assert [row["cpu_count"] for row in payload["data"]["specs"]] == [120, 40]
    assert calls == [
        ("lcg-cpu-2", "SCHEDULE_CONFIG_TYPE_HPC"),
        ("lcg-hpc-2", "SCHEDULE_CONFIG_TYPE_HPC"),
    ]


def test_resources_specs_notebook_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-hpc-2", "name": "HPC-可上网区资源-2"},
        ],
    )

    def _fake_prices(**kwargs):
        assert kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_DSW"
        return [
            {
                "quota_id": "quota-dsw-55-300",
                "cpu_count": 55,
                "memory_size_gib": 300,
                "gpu_count": 0,
                "gpu_info": {"gpu_type_display": "CPU"},
            }
        ]

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "分布式训练空间",
            "--usage",
            "notebook",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["usage_filter"] == "notebook"
    assert payload["data"]["specs"][0]["usage"] == "notebook"
    assert payload["data"]["specs"][0]["cpu_count"] == 55


def test_resources_specs_include_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-empty", "name": "Empty Group"},
        ],
    )
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "get_resource_prices",
        lambda **kwargs: [],
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "resources", "specs", "--include-empty"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    row = payload["data"]["specs"][0]
    assert row["usage"] == "auto"
    assert row["schedule_config_type"] == ""
    assert row["logic_compute_group_id"] == "lcg-empty"
    assert row["spec_id"] == ""


def test_resources_specs_help_explains_auto_mode() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["resources", "specs", "--help"])

    assert result.exit_code == 0
    assert "auto = HPC first" in result.output
    assert "notebook/DSW" in result.output
    # ray family must be discoverable from --help.
    assert "ray" in result.output


def test_resources_specs_hpc_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-hpc-2", "name": "HPC-可上网区资源-2"},
        ],
    )

    calls: list[str] = []

    def _fake_prices(**kwargs):
        calls.append(kwargs["schedule_config_type"])
        assert kwargs["logic_compute_group_id"] == "lcg-hpc-2"
        return [
            {
                "quota_id": "quota-hpc-40-200",
                "cpu_count": 40,
                "memory_size_gib": 200,
                "gpu_count": 0,
                "gpu_info": {"gpu_type_display": "CPU"},
            }
        ]

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "分布式训练空间",
            "--usage",
            "hpc",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["usage_filter"] == "hpc"
    assert payload["data"]["total"] == 1
    row = payload["data"]["specs"][0]
    assert row["usage"] == "hpc"
    assert row["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_HPC"
    assert row["spec_id"] == "quota-hpc-40-200"
    assert calls == ["SCHEDULE_CONFIG_TYPE_HPC"]


def test_resources_specs_ray_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"},
        ],
    )

    calls: list[str] = []

    def _fake_prices(**kwargs):
        calls.append(kwargs["schedule_config_type"])
        assert kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_RAY_JOB"
        return [
            {
                "quota_id": "quota-ray-head-32-256",
                "cpu_count": 32,
                "memory_size_gib": 256,
                "gpu_count": 0,
                "gpu_info": {"gpu_type_display": "CPU"},
            },
            {
                "quota_id": "quota-ray-worker-8-64",
                "cpu_count": 8,
                "memory_size_gib": 64,
                "gpu_count": 0,
                "gpu_info": {"gpu_type_display": "CPU"},
            },
        ]

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "分布式训练空间",
            "--usage",
            "ray",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["usage_filter"] == "ray"
    assert payload["data"]["total"] == 2
    rows = payload["data"]["specs"]
    assert {row["usage"] for row in rows} == {"ray"}
    assert {row["schedule_config_type"] for row in rows} == {"SCHEDULE_CONFIG_TYPE_RAY_JOB"}
    assert {row["spec_id"] for row in rows} == {
        "quota-ray-head-32-256",
        "quota-ray-worker-8-64",
    }
    assert calls == ["SCHEDULE_CONFIG_TYPE_RAY_JOB"]


def test_resources_specs_all_includes_ray(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"},
        ],
    )

    queried: list[str] = []

    def _fake_prices(**kwargs):
        queried.append(kwargs["schedule_config_type"])
        return []

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "分布式训练空间",
            "--usage",
            "all",
        ],
    )

    assert result.exit_code == 0
    assert sorted(queried) == sorted(
        [
            "SCHEDULE_CONFIG_TYPE_DSW",
            "SCHEDULE_CONFIG_TYPE_HPC",
            "SCHEDULE_CONFIG_TYPE_RAY_JOB",
        ]
    )


def test_resources_specs_ray_auto_cross_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without --workspace, --usage ray must search every workspace."""
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-default"
        all_workspace_ids = ["ws-default", "ws-cpu", "ws-train"]
        all_workspace_names = {
            "ws-default": "Default WS",
            "ws-cpu": "CPU资源空间",
            "ws-train": "分布式训练空间",
        }

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())

    queried_workspaces: list[str] = []

    def _fake_groups(**kwargs):
        queried_workspaces.append(kwargs["workspace_id"])
        if kwargs["workspace_id"] == "ws-cpu":
            return [{"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"}]
        return []

    def _fake_prices(**kwargs):
        if (
            kwargs["workspace_id"] == "ws-cpu"
            and kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_RAY_JOB"
        ):
            return [
                {
                    "quota_id": "quota-ray-1",
                    "cpu_count": 4,
                    "memory_size_gib": 16,
                    "gpu_count": 0,
                    "gpu_info": {"gpu_type_display": "CPU"},
                }
            ]
        return []

    monkeypatch.setattr(
        specs_module.browser_api_module, "list_notebook_compute_groups", _fake_groups
    )
    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "resources", "specs", "--usage", "ray"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    # Must have searched every workspace, not just the default.
    assert sorted(queried_workspaces) == sorted(["ws-default", "ws-cpu", "ws-train"])
    # Must have surfaced the ray quota that lives in ws-cpu.
    assert payload["data"]["total"] == 1
    row = payload["data"]["specs"][0]
    assert row["workspace_id"] == "ws-cpu"
    assert row["workspace_name"] == "CPU资源空间"
    assert row["spec_id"] == "quota-ray-1"
    # And the response carries the list form.
    assert sorted(payload["data"]["workspace_ids"]) == sorted(
        ["ws-default", "ws-cpu", "ws-train"]
    )


def test_resources_specs_explicit_workspace_skips_cross_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--workspace pins to one workspace even when --usage ray would auto-cross."""
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-default"
        all_workspace_ids = ["ws-default", "ws-other"]
        all_workspace_names = {"ws-default": "Default WS", "ws-other": "Other WS"}

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())

    queried_workspaces: list[str] = []

    def _fake_groups(**kwargs):
        queried_workspaces.append(kwargs["workspace_id"])
        return []

    monkeypatch.setattr(
        specs_module.browser_api_module, "list_notebook_compute_groups", _fake_groups
    )
    monkeypatch.setattr(
        specs_module.browser_api_module, "get_resource_prices", lambda **_: []
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "resources", "specs", "--workspace", "分布式训练空间", "--usage", "ray"],
    )
    assert result.exit_code == 0
    # Should hit only the explicit workspace (resolved to a real ws-id by select_workspace_id).
    assert len(queried_workspaces) == 1


def test_resources_specs_auto_falls_back_to_notebook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_specs as specs_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: [
            {"logic_compute_group_id": "lcg-gpu-1", "name": "cuda12.8版本H100"},
        ],
    )

    calls: list[str] = []

    def _fake_prices(**kwargs):
        calls.append(kwargs["schedule_config_type"])
        if kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_HPC":
            return []
        return [
            {
                "quota_id": "quota-dsw-h100",
                "cpu_count": 8,
                "memory_size_gib": 64,
                "gpu_count": 1,
                "gpu_info": {"gpu_type_display": "NVIDIA H100 (80GB)"},
            }
        ]

    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", _fake_prices)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "分布式训练空间",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["usage_filter"] == "auto"
    row = payload["data"]["specs"][0]
    assert row["usage"] == "notebook"
    assert row["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_DSW"
    assert row["gpu_count"] == 1
    assert calls == ["SCHEDULE_CONFIG_TYPE_HPC", "SCHEDULE_CONFIG_TYPE_DSW"]
