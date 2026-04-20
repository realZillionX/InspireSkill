import json
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.context import EXIT_CONFIG_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils import auth as auth_module
from inspire.platform.web.browser_api.hpc_jobs import HPCJobInfo


class DummyHPCAPI:
    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}

    def create_hpc_job(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["create_hpc_job"] = kwargs
        return {"data": {"job_id": "hpc-job-123", "status": "QUEUING"}}

    def get_hpc_job_detail(self, job_id: str) -> dict[str, Any]:
        self.calls["get_hpc_job_detail"] = job_id
        return {"data": {"job_id": job_id, "name": "hpc-demo", "status": "RUNNING"}}

    def stop_hpc_job(self, job_id: str) -> bool:
        self.calls["stop_hpc_job"] = job_id
        return True


def patch_hpc_config_and_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DummyHPCAPI:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        job_project_id="project-default",
        job_workspace_id="ws-00000000-0000-0000-0000-000000000001",
        job_image="registry.local/hpc:latest",
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "logs"),
    )
    config.projects = {"alias-project": "project-alias"}
    config.workspaces = {"cpu-room": "ws-00000000-0000-0000-0000-000000000002"}

    def fake_from_files_and_env(
        cls,
        require_target_dir: bool = False,
        require_credentials: bool = True,
    ) -> tuple[config_module.Config, dict[str, str]]:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )

    api = DummyHPCAPI()

    def fake_get_api(
        self_or_cls,
        cfg: Optional[config_module.Config] = None,
    ) -> DummyHPCAPI:  # type: ignore[override]
        assert cfg is config or cfg is None
        return api

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)
    auth_module.AuthManager.clear_cache()
    return api


def test_hpc_create_json_uses_alias_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "create",
            "-n",
            "hpc-demo",
            "-c",
            "bash run_hpc.sh",
            "--logic-compute-group-id",
            "lcg-123",
            "--spec-id",
            "spec-123",
            "--project",
            "alias-project",
            "--workspace",
            "cpu-room",
            "--cpus-per-task",
            "32",
            "--memory-per-cpu",
            "8",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["job_id"] == "hpc-job-123"

    call = api.calls["create_hpc_job"]
    assert call["project_id"] == "project-alias"
    assert call["workspace_id"] == "ws-00000000-0000-0000-0000-000000000002"
    assert call["image"] == "registry.local/hpc:latest"
    assert call["cpus_per_task"] == 32
    assert call["memory_per_cpu"] == 8


def test_hpc_create_help_highlights_slurm_body() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["hpc", "create", "--help"])

    assert result.exit_code == 0
    assert "Slurm script body" in result.output
    assert "predef_quota_id" in result.output
    assert "higher numbers request" in result.output
    assert "higher priority; project quota may cap it" in result.output


def test_hpc_create_human_output_includes_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "hpc",
            "create",
            "-n",
            "hpc-demo",
            "-c",
            "srun python train.py",
            "--logic-compute-group-id",
            "lcg-123",
            "--spec-id",
            "spec-123",
            "--cpus-per-task",
            "32",
            "--memory-per-cpu",
            "8",
            "--priority",
            "7",
        ],
    )

    assert result.exit_code == 0
    assert "Requested Priority: 7" in result.output
    assert "Entry:     srun python train.py" in result.output


def test_hpc_create_rejects_priority_11() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "hpc",
            "create",
            "-n",
            "hpc-demo",
            "-c",
            "srun python train.py",
            "--logic-compute-group-id",
            "lcg-123",
            "--spec-id",
            "spec-123",
            "--cpus-per-task",
            "32",
            "--memory-per-cpu",
            "8",
            "--priority",
            "11",
        ],
    )

    assert result.exit_code != 0
    assert "1<=x<=10" in result.output


def test_hpc_create_rejects_full_slurm_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "hpc",
            "create",
            "-n",
            "hpc-demo",
            "-c",
            "#!/bin/bash\n#SBATCH --time=1:00:00\nsrun python train.py",
            "--logic-compute-group-id",
            "lcg-123",
            "--spec-id",
            "spec-123",
            "--cpus-per-task",
            "32",
            "--memory-per-cpu",
            "8",
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "HPC entrypoint must be the Slurm body" in result.output


def test_hpc_status_human_output_shows_priority_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    api.get_hpc_job_detail = lambda job_id: {
        "data": {
            "job_id": job_id,
            "name": "hpc-demo",
            "status": "RUNNING",
            "priority": 7,
            "priority_name": "7",
            "priority_level": "HIGH",
        }
    }
    runner = CliRunner()

    result = runner.invoke(cli_main, ["hpc", "status", "hpc-job-123"])

    assert result.exit_code == 0
    assert "Requested Priority: 7" in result.output
    assert "Priority Name: 7" in result.output
    assert "Priority Level: HIGH" in result.output


def test_hpc_status_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "status", "hpc-job-123"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "RUNNING"
    assert api.calls["get_hpc_job_detail"] == "hpc-job-123"


def test_hpc_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    class _DummySession:
        workspace_id = "ws-session-default"

    monkeypatch.setattr(hpc_cmd_module, "get_web_session", lambda: _DummySession())
    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_jobs",
        lambda **kwargs: (
            [
                HPCJobInfo(
                    job_id="hpc-job-001",
                    name="prep",
                    status="RUNNING",
                    entrypoint="bash prep.sh",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_name="tester",
                    created_by_id="user-1",
                    project_id="project-1",
                    project_name="Project 1",
                    compute_group_name="CPU资源-2",
                    workspace_id=kwargs.get("workspace_id") or "ws-session-default",
                )
            ],
            1,
        ),
    )

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "list",
            "--workspace",
            "cpu-room",
            "--status",
            "RUNNING",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    assert payload["data"]["jobs"][0]["job_id"] == "hpc-job-001"
    assert payload["data"]["jobs"][0]["workspace_id"] == "ws-00000000-0000-0000-0000-000000000002"


def test_hpc_stop_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "stop", "hpc-job-999"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["stopped"] is True
    assert api.calls["stop_hpc_job"] == "hpc-job-999"
