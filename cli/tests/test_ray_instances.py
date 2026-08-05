import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.ray import ray_commands
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.ray_jobs import RayJobInfo


class _FakeSession:
    workspace_id = "ws-session"
    all_workspace_names = {"ws-ray": "Ray资源空间"}
    all_workspace_ids = ["ws-ray"]


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )

    def fake_from_files_and_env(
        cls,
        require_credentials: bool = True,
    ) -> tuple[config_module.Config, dict[str, str]]:  # type: ignore[override]
        del cls, require_credentials
        return config, {}

    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )


def test_ray_list_all_expands_and_limit_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    session = _FakeSession()
    calls: list[int] = []
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )

    def fake_list_ray_jobs(**kwargs):  # noqa: ANN001
        calls.append(kwargs["page_size"])
        count = kwargs["page_size"]
        return (
            [
                RayJobInfo(
                    ray_job_id=f"ray-job-{index}",
                    name=f"job-{index}",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=1,
                    raw={},
                )
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(ray_commands.browser_api_module, "list_ray_jobs", fake_list_ray_jobs)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "list", "--workspace", "Ray资源空间", "--all"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert len(payload["items"]) == 25
    assert "truncated" not in payload
    assert payload["items"][0] == {
        "name": "job-0",
        "status": "RUNNING",
        "project": "Project",
        "workspace": "Ray资源空间",
        "compute_group": "",
        "created_by": "tester",
    }

    calls.clear()
    conflict = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "list",
            "--workspace",
            "Ray资源空间",
            "--all",
            "--limit",
            "3",
        ],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
    assert calls == []


def test_ray_list_workspace_all_fans_out_and_uses_visible_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)

    class _AllWorkspaceSession:
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "Ray East", "ws-b": "Ray West"}

    calls: list[str] = []
    monkeypatch.setattr(
        ray_commands,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )

    def fake_list_ray_jobs(**kwargs):  # noqa: ANN001
        workspace_id = kwargs["workspace_id"]
        calls.append(workspace_id)
        return (
            [
                RayJobInfo(
                    ray_job_id=f"ray-{workspace_id}",
                    name=f"job-{workspace_id[-1]}",
                    status="RUNNING",
                    workspace_id=workspace_id,
                    project_id="project-1",
                    project_name="Project",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=1,
                    raw={},
                )
            ],
            1,
        )

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_jobs",
        fake_list_ray_jobs,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "list", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["data"]["items"]
    assert calls == ["ws-a", "ws-b"]
    assert {row["workspace"] for row in rows} == {"Ray East", "Ray West"}
    assert all(
        set(row)
        == {
            "name",
            "status",
            "project",
            "workspace",
            "compute_group",
            "created_by",
        }
        for row in rows
    )
    assert "workspace_id" not in result.output


def test_ray_instances_requires_workspace_and_uses_num(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    session = _FakeSession()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )

    def fake_list_ray_jobs(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return (
            [
                RayJobInfo(
                    ray_job_id="rj-abc",
                    name="elastic-a",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project 1",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=7,
                    raw={},
                )
            ],
            1,
        )

    def fake_list_ray_job_instances(ray_job_id, *, limit, session):  # noqa: ANN001
        captured["instances"] = {"ray_job_id": ray_job_id, "limit": limit, "session": session}
        return (
            [
                {
                    "instance_id": "rj-abc-head-1",
                    "name": "head-0",
                    "instance_type": "head",
                    "status": "running",
                    "cpu_count": 2,
                    "gpu_count": 0,
                    "memory_size": 8,
                    "created_at": 1770000000,
                    "node": "ray-node-a",
                    "backend": "browser",
                }
            ],
            1,
        )

    monkeypatch.setattr(ray_commands.browser_api_module, "list_ray_jobs", fake_list_ray_jobs)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        fake_list_ray_job_instances,
    )

    missing_workspace = CliRunner().invoke(cli_main, ["ray", "instances", "elastic-a"])
    assert missing_workspace.exit_code != 0
    assert "Missing option '--workspace'" in missing_workspace.output

    result = CliRunner().invoke(
        cli_main,
        ["ray", "instances", "elastic-a", "--workspace", "Ray资源空间", "--limit", "42"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["workspace_id"] == "ws-ray"
    assert captured["resolve"]["user_ids"] == ["user-1"]
    assert captured["resolve"]["page_num"] == 1
    assert captured["resolve"]["page_size"] == 42
    assert captured["instances"]["ray_job_id"] == "rj-abc"
    assert captured["instances"]["limit"] == 42
    assert captured["instances"]["session"] is session
    assert result.output.splitlines()[0].lstrip().startswith("Name")
    assert "head" in result.output
    assert "2 CPU, 8 GiB, 0 GPU" in result.output
    assert "Ray Instances" not in result.output
    assert "Total:" not in result.output
    assert "rj-abc-head-1" not in result.output
    assert "ray-node-a" not in result.output
    assert "backend" not in result.output


def test_ray_instances_json_omits_platform_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_jobs",
        lambda **kwargs: (
            [
                RayJobInfo(
                    ray_job_id="rj-abc",
                    name="elastic-a",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project 1",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=7,
                    raw={},
                )
            ],
            1,
        ),
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        lambda ray_job_id, *, limit, session: (
            [{"instance_id": "rj-abc-head-1", "instance_type": "head"}],
            1,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "instances", "elastic-a", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload == {
        "name": "elastic-a",
        "items": [{"type": "head", "rank": 0}],
    }
    assert "rj-abc-head-1" not in result.output


def test_ray_instances_default_budget_notifies_and_keeps_resolution_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    session = _FakeSession()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )

    def fake_list_ray_jobs(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return (
            [
                RayJobInfo(
                    ray_job_id="rj-abc",
                    name="elastic-a",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project 1",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=7,
                    raw={},
                )
            ],
            1,
        )

    def fake_list_ray_job_instances(ray_job_id, *, limit, session):  # noqa: ANN001
        captured["instance_limit"] = limit
        return (
            [
                {
                    "name": f"worker-{index}",
                    "instance_type": "worker",
                    "status": "running",
                }
                for index in range(20)
            ],
            25,
        )

    monkeypatch.setattr(ray_commands.browser_api_module, "list_ray_jobs", fake_list_ray_jobs)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        fake_list_ray_job_instances,
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "instances", "elastic-a", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["page_size"] == 500
    assert captured["instance_limit"] == 20
    assert "Showing 20 of 25. Use --all for the full list." in result.output

    json_result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "instances", "elastic-a", "--workspace", "Ray资源空间"],
    )
    assert json_result.exit_code == 0, json_result.output
    metadata = json.loads(json_result.output)["data"]
    assert metadata["name"] == "elastic-a"
    assert len(metadata["items"]) == 20
    assert metadata["shown"] == 20
    assert metadata["total"] == 25
    assert metadata["truncated"] is True
    assert "limit" not in metadata


def test_ray_instances_all_expands_and_json_conflict_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_jobs",
        lambda **kwargs: (
            [
                RayJobInfo(
                    ray_job_id="rj-abc",
                    name="elastic-a",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project 1",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=7,
                    raw={},
                )
            ],
            1,
        ),
    )
    calls: list[int] = []

    def fake_list_ray_job_instances(ray_job_id, *, limit, session):  # noqa: ANN001
        calls.append(limit)
        count = 25 if limit == 25 else 20
        return (
            [
                {
                    "instance_id": f"rj-abc-worker-{index}",
                    "name": f"worker-{index}",
                    "instance_type": "worker",
                    "status": "running",
                }
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        fake_list_ray_job_instances,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "instances",
            "elastic-a",
            "--workspace",
            "Ray资源空间",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert set(payload) == {"name", "items"}
    assert payload["name"] == "elastic-a"
    assert len(payload["items"]) == 25
    assert all(
        set(item) <= {"name", "status", "role", "type", "resource", "rank"}
        for item in payload["items"]
    )
    assert "rj-abc-worker" not in result.output

    conflict = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "instances",
            "elastic-a",
            "--workspace",
            "Ray资源空间",
            "--all",
            "--limit",
            "3",
        ],
    )

    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
