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

    def fake_list_ray_job_instances(ray_job_id, *, num, session):  # noqa: ANN001
        captured["instances"] = {"ray_job_id": ray_job_id, "num": num, "session": session}
        return (
            [
                {
                    "instance_id": "rj-abc-head-1",
                    "instance_type": "head",
                    "status": "running",
                    "cpu_count": 2,
                    "gpu_count": 0,
                    "memory_size": 8,
                    "created_at": 1770000000,
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
        ["ray", "instances", "elastic-a", "--workspace", "Ray资源空间", "--num", "42"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["workspace_id"] == "ws-ray"
    assert captured["resolve"]["user_ids"] == ["user-1"]
    assert captured["resolve"]["page_num"] == 1
    assert captured["resolve"]["page_size"] == 42
    assert captured["instances"]["ray_job_id"] == "rj-abc"
    assert captured["instances"]["num"] == 42
    assert captured["instances"]["session"] is session
    assert "Ray Instances" in result.output
    assert "head" in result.output


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
        lambda ray_job_id, *, num, session: (
            [{"instance_id": "rj-abc-head-1", "instance_type": "head"}],
            1,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "instances", "elastic-a", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "ray_job_id" not in payload["data"]
    assert "instance_id" not in payload["data"]["instances"][0]
