import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.context import EXIT_CONFIG_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.hpc_jobs import HPCJobInfo


class DummyHPCAPI:
    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}

    def create_hpc_job(
        self, *, payload: dict[str, Any], session: object | None = None
    ) -> dict[str, Any]:
        del session
        self.calls["create_hpc_job"] = payload
        return {"job_id": "hpc-job-123", "status": "QUEUING"}

    def get_hpc_job_detail(self, job_id: str, session: object | None = None) -> dict[str, Any]:
        del session
        self.calls["get_hpc_job_detail"] = job_id
        return {"job_id": job_id, "name": "hpc-demo", "status": "RUNNING"}

    def stop_hpc_job(self, job_id: str, session: object | None = None) -> bool:
        del session
        self.calls["stop_hpc_job"] = job_id
        return True


def patch_hpc_config_and_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DummyHPCAPI:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        log_cache_dir=str(tmp_path / "logs"),
    )
    config.projects = {"alias-project": "project-alias"}
    config.compute_groups = [{"id": "lcg-123", "name": "CG-123"}]

    def fake_from_files_and_env(
        cls,
        require_credentials: bool = True,
    ) -> tuple[config_module.Config, dict[str, str]]:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )

    api = DummyHPCAPI()

    # Stub session + quota resolver so the test never hits the real platform.
    import importlib

    hpc_mod = importlib.import_module("inspire.cli.commands.hpc.hpc_commands")
    quota_mod = importlib.import_module("inspire.cli.utils.quota_resolver")

    class _FakeWebSession:
        # The HPC create flow needs an active workspace from the web session.
        # Use the alias-mapped id we set above so resolve_workspace_id /
        # quota lookup find a real value.
        workspace_id = "ws-00000000-0000-0000-0000-000000000002"
        all_workspace_names = {workspace_id: "cpu-room"}
        all_workspace_ids = [workspace_id]

    monkeypatch.setattr(hpc_mod, "get_web_session", lambda: _FakeWebSession())
    monkeypatch.setattr(
        hpc_mod.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )
    monkeypatch.setattr(
        hpc_mod.browser_api_module,
        "create_hpc_job",
        lambda *, payload, session=None: api.create_hpc_job(payload=payload, session=session),
    )
    monkeypatch.setattr(
        hpc_mod.browser_api_module,
        "get_hpc_job_detail",
        lambda job_id, session=None: api.get_hpc_job_detail(job_id, session=session),
    )
    monkeypatch.setattr(
        hpc_mod.browser_api_module,
        "stop_hpc_job",
        lambda job_id, session=None: api.stop_hpc_job(job_id, session=session),
    )

    def _fake_resolve_quota(*, spec, workspace_id, session=None, **_):  # noqa: ANN001
        return quota_mod.ResolvedQuota(
            quota_id="spec-test-default",
            logic_compute_group_id="lcg-123",
            compute_group_name="CG-123",
            gpu_count=spec.gpu_count,
            cpu_count=spec.cpu_count,
            memory_gib=spec.memory_gib,
            gpu_type="" if spec.gpu_count == 0 else "H200",
            raw_price={"cpu_info": {"cpu_type": "Test"}},
        )

    monkeypatch.setattr(quota_mod, "resolve_quota", _fake_resolve_quota)
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
            "--group",
            "CG-123",
            "--quota",
            "0,32,256",
            "--project",
            "alias-project",
            "--workspace",
            "cpu-room",
            "--image",
            "registry.local/hpc:latest",
            "--cpus-per-task",
            "8",
            "--memory-per-cpu",
            "4",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "QUEUING"

    call = api.calls["create_hpc_job"]
    assert call["job_name"] == "hpc-demo"
    assert call["project_id"] == "project-alias"
    assert call["workspace_id"] == "ws-00000000-0000-0000-0000-000000000002"
    assert call["logic_compute_group_id"] == "lcg-123"
    assert call["slurm_cluster_spec"]["image"] == "registry.local/hpc:latest"
    assert call["slurm_cluster_spec"]["predef_quota_id"] == "spec-test-default"
    assert call["slurm_cluster_spec"]["spec_price"]["logic_compute_group_id"] == "lcg-123"
    # Slurm-level knobs are forwarded as-is, independent of the node spec.
    assert call["sbatch_script"]["cpus_per_task"] == 8
    assert call["sbatch_script"]["memory_per_cpu"] == "4G"


def test_hpc_create_slurm_knobs_default_from_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without --cpus-per-task / --memory-per-cpu, the CLI fills them from --quota."""
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
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
            "--group",
            "CG-123",
            "--quota",
            "0,32,256",
            "--workspace",
            "cpu-room",
            "--project",
            "alias-project",
            "--image",
            "registry.local/hpc:latest",
        ],
    )
    assert result.exit_code == 0, result.output
    call = api.calls["create_hpc_job"]
    # Defaults: cpus_per_task = quota.cpu, memory_per_cpu = mem // cpu
    assert call["sbatch_script"]["cpus_per_task"] == 32
    assert call["sbatch_script"]["memory_per_cpu"] == "8G"


def test_hpc_create_help_highlights_slurm_body() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["hpc", "create", "--help"])

    assert result.exit_code == 0
    assert "Slurm script body" in result.output
    # Help must explain the two-layer model: --quota for node spec,
    # slurm knobs for in-node subdivision.
    assert "--quota" in result.output
    assert "gpu,cpu,mem" in result.output


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
            "--group",
            "CG-123",
            "--quota",
            "0,32,256",
            "--priority",
            "7",
            "--workspace",
            "cpu-room",
            "--project",
            "alias-project",
            "--image",
            "registry.local/hpc:latest",
        ],
    )

    assert result.exit_code == 0, result.output
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
            "--group",
            "CG-123",
            "--quota",
            "0,32,256",
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
            "--group",
            "CG-123",
            "--quota",
            "0,32,256",
            "--workspace",
            "cpu-room",
            "--project",
            "alias-project",
            "--image",
            "registry.local/hpc:latest",
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "HPC entrypoint must be the Slurm body" in result.output


def test_hpc_status_human_output_shows_priority_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-123")
    api.get_hpc_job_detail = lambda job_id, session=None: {
        "job_id": job_id,
        "name": "hpc-demo",
        "status": "RUNNING",
        "priority": 7,
        "priority_name": "7",
        "priority_level": "HIGH",
    }
    runner = CliRunner()

    result = runner.invoke(cli_main, ["hpc", "status", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    assert "Requested Priority: 7" in result.output
    assert "Priority Name: 7" in result.output
    assert "Priority Level: HIGH" in result.output


def test_hpc_status_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-123")
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "status", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "RUNNING"
    assert api.calls["get_hpc_job_detail"] == "hpc-job-123"


@pytest.mark.parametrize("command", ["status", "events", "instances", "stop", "delete"])
def test_hpc_name_commands_reject_handles_before_web_session(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod
    from inspire.cli.commands.hpc import hpc_events as hpc_events_mod

    def fail_session():  # noqa: ANN001
        raise AssertionError("web session should not be opened for handle-shaped input")

    monkeypatch.setattr(hpc_mod, "get_web_session", fail_session)
    monkeypatch.setattr(hpc_events_mod, "get_web_session", fail_session)

    args = [
        "--json",
        "hpc",
        command,
        "hpc-job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf",
        "--workspace",
        "cpu-room",
    ]
    if command == "delete":
        args.append("--yes")

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code != 0
    assert "ValidationError" in result.output
    assert "hpc name" in result.output


def test_hpc_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    class _DummySession:
        workspace_id = "ws-session-default"
        all_workspace_names = {"ws-session-default": "cpu-room"}
        all_workspace_ids = ["ws-session-default"]

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
    assert payload["data"]["jobs"][0]["name"] == "prep"
    assert "job_id" not in payload["data"]["jobs"][0]
    assert "workspace_id" not in payload["data"]["jobs"][0]


def test_hpc_instances_requires_workspace_and_uses_num(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()
    captured: dict[str, Any] = {}

    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    def fake_list_hpc_jobs(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return (
            [
                HPCJobInfo(
                    job_id="hpc-job-001",
                    name="prep",
                    status="RUNNING",
                    entrypoint="srun python prep.py",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_name="tester",
                    created_by_id="user-1",
                    project_id="project-1",
                    project_name="Project 1",
                    compute_group_name="CPU资源-2",
                    workspace_id=kwargs["workspace_id"],
                )
            ],
            1,
        )

    def fake_list_hpc_job_instances(job_id, *, limit, session):  # noqa: ANN001
        captured["instances"] = {"job_id": job_id, "limit": limit, "session": session}
        return (
            [
                {
                    "name": "launcher",
                    "component": "launcher",
                    "status": "Running",
                    "node": "cpu-node-a",
                    "created_at": 1770000000,
                }
            ],
            1,
        )

    monkeypatch.setattr(hpc_cmd_module.browser_api_module, "list_hpc_jobs", fake_list_hpc_jobs)
    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_job_instances",
        fake_list_hpc_job_instances,
    )

    missing_workspace = runner.invoke(cli_main, ["hpc", "instances", "prep"])
    assert missing_workspace.exit_code != 0
    assert "Missing option '--workspace'" in missing_workspace.output

    result = runner.invoke(
        cli_main,
        ["hpc", "instances", "prep", "--workspace", "cpu-room", "--limit", "42"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["workspace_id"] == "ws-00000000-0000-0000-0000-000000000002"
    assert captured["resolve"]["created_by"] == "user-1"
    assert captured["resolve"]["page_num"] == 1
    assert captured["resolve"]["page_size"] == 42
    assert captured["instances"]["job_id"] == "hpc-job-001"
    assert captured["instances"]["limit"] == 42
    assert "HPC Instances" in result.output
    assert "launcher" in result.output


def test_hpc_stop_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-999")
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "stop", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["stopped"] is True
    assert api.calls["stop_hpc_job"] == "hpc-job-999"
