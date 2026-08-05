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
        return {
            "job_id": "hpc-job-123",
            "name": "backend-name",
            "status": "QUEUING",
            "message": "backend message",
            "request": {"trace_id": "trace-internal"},
        }

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
    )
    config.projects = {"alias-project": "Project"}
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
    projects_mod = importlib.import_module("inspire.platform.web.browser_api.projects")
    quota_mod = importlib.import_module("inspire.cli.utils.quota_resolver")

    class _FakeWebSession:
        # The HPC create flow needs an active workspace from the web session.
        # Keep the fake session close enough to a real web session for the
        # live project and quota resolvers.
        storage_state: dict[str, Any] = {}
        cookies: dict[str, str] = {}
        workspace_id = "ws-00000000-0000-0000-0000-000000000002"
        all_workspace_names = {workspace_id: "cpu-room"}
        all_workspace_ids = [workspace_id]

    monkeypatch.setattr(hpc_mod, "get_web_session", lambda: _FakeWebSession())
    project = projects_mod.ProjectInfo(
        project_id="project-alias",
        name="Project",
        workspace_id=_FakeWebSession.workspace_id,
        priority_name="10",
    )
    monkeypatch.setattr(projects_mod, "list_projects", lambda **_kwargs: [project])
    monkeypatch.setattr(
        hpc_mod.browser_api_module,
        "list_projects",
        lambda **_kwargs: [project],
    )
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


@pytest.mark.parametrize(
    ("command", "metavar"),
    [
        ("list", "NAME|all"),
        ("create", "NAME"),
        ("quota", "NAME|all"),
        ("status", "NAME"),
        ("instances", "NAME"),
        ("stop", "NAME"),
        ("delete", "NAME"),
        ("events", "NAME"),
        ("metrics", "NAME"),
    ],
)
def test_hpc_workspace_help_uses_name_metavars(
    command: str,
    metavar: str,
) -> None:
    result = CliRunner().invoke(cli_main, ["hpc", command, "--help"])

    assert result.exit_code == 0
    assert f"--workspace {metavar}" in result.output
    assert "--workspace TEXT" not in result.output


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
    assert payload["data"] == {
        "name": "hpc-demo",
        "status": "created",
    }
    assert "hpc-job-123" not in result.output
    assert "backend-name" not in result.output
    assert "QUEUING" not in result.output
    assert "backend message" not in result.output
    assert "trace-internal" not in result.output

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


def test_hpc_create_human_output_is_compact(
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
    assert result.output == "OK HPC created: hpc-demo\n"


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


def test_hpc_status_human_output_is_compact_and_name_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-123")
    api.get_hpc_job_detail = lambda job_id, session=None: {
        "job_id": job_id,
        "name": "hpc-demo",
        "status": "RUNNING",
        "project_name": "Project",
        "logic_compute_group_name": "CPU Group",
        "resource_spec_price": {
            "cpu_count": 32,
            "memory_size_gib": 256,
            "gpu_count": 0,
            "quota_id": "quota-hidden",
        },
        "priority": 7,
        "priority_name": "7",
        "priority_level": "HIGH",
        "created_at": "1770000000",
        "updated_at": "1770000100",
        "request_id": "trace-hidden",
        "internal_path": "/internal/hpc-job-123",
    }
    runner = CliRunner()

    result = runner.invoke(cli_main, ["hpc", "status", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    assert "HPC Job Status" not in result.output
    assert "Name: hpc-demo" in result.output
    assert "Status: RUNNING" in result.output
    assert "Project: Project" in result.output
    assert "Compute Group: CPU Group" in result.output
    assert "Resource: 32 CPU, 256 GiB, 0 GPU" in result.output
    assert "Created: 1770000000" in result.output
    assert "Updated: 1770000100" in result.output
    assert "hpc-job-123" not in result.output
    assert "quota-hidden" not in result.output
    assert "trace-hidden" not in result.output
    assert "/internal/hpc-job-123" not in result.output
    assert "Priority: 7" in result.output
    assert "Priority Level: HIGH" in result.output
    assert "Priority Name:" not in result.output


def test_hpc_status_json_uses_stable_public_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-123")
    detail_calls: list[str] = []

    def _detail(job_id: str, session: object | None = None) -> dict[str, Any]:
        del session
        detail_calls.append(job_id)
        return {
            "job_id": job_id,
            "name": "hpc-demo",
            "status": "RUNNING",
            "project_name": "Project",
            "resource": {
                "cpu_count": 32,
                "memory_size_gib": 256,
                "gpu_count": 1,
                "gpu_info": {
                    "gpu_type_display": "NVIDIA H200",
                    "gpu_type": "NVIDIA_H200_INTERNAL",
                },
            },
            "created_at": "1770000000",
            "updated_at": "1770000100",
            "finished_at": None,
            "payload": {"request_id": "trace-hidden"},
            "workspace_id": "ws-hidden",
            "logic_compute_group_id": "lcg-hidden",
        }

    monkeypatch.setattr(hpc_mod.browser_api_module, "get_hpc_job_detail", _detail)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "status", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"] == {
        "name": "hpc-demo",
        "status": "RUNNING",
        "project": "Project",
        "resource": {
            "cpu": 32,
            "memory_gib": 256,
            "gpu": 1,
            "gpu_type": "NVIDIA H200",
        },
        "created_at": "1770000000",
        "updated_at": "1770000100",
    }
    assert "job_id" not in payload["data"]
    assert "workspace_id" not in payload["data"]
    assert "logic_compute_group_id" not in payload["data"]
    assert "trace-hidden" not in result.output
    assert detail_calls == ["hpc-job-123"]


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
    assert "total" not in payload["data"]
    row = payload["data"]["items"][0]
    assert row == {
        "name": "prep",
        "status": "RUNNING",
        "project": "Project 1",
        "workspace": "cpu-room",
        "compute_group": "CPU资源-2",
        "created_by": "tester",
    }
    assert "job_id" not in row
    assert "workspace_id" not in row


def test_hpc_list_keyword_filters_readable_fields_locally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    class _DummySession:
        all_workspace_names = {"ws-session-default": "cpu-room"}
        all_workspace_ids = ["ws-session-default"]

    monkeypatch.setattr(hpc_cmd_module, "get_web_session", lambda: _DummySession())

    def fake_list_hpc_jobs(**kwargs):  # noqa: ANN001
        jobs = [
            HPCJobInfo(
                job_id="hpc-hidden-1",
                name="unrelated",
                status="RUNNING",
                entrypoint="python train_model.py",
                created_at="1770000002",
                finished_at=None,
                created_by_name="tester",
                created_by_id="user-1",
                project_id="project-1",
                project_name="Project",
                compute_group_name="CPU",
                workspace_id=kwargs["workspace_id"],
            ),
            HPCJobInfo(
                job_id="hpc-hidden-2",
                name="other",
                status="FAILED",
                entrypoint="python eval.py",
                created_at="1770000001",
                finished_at=None,
                created_by_name="tester",
                created_by_id="user-1",
                project_id="project-1",
                project_name="Project",
                compute_group_name="CPU",
                workspace_id=kwargs["workspace_id"],
            ),
        ]
        return jobs[: kwargs["page_size"]], len(jobs)

    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_jobs",
        fake_list_hpc_jobs,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "list",
            "--workspace",
            "cpu-room",
            "--status",
            "running",
            "--keyword",
            "TRAIN_MODEL",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert [item["name"] for item in payload["items"]] == ["unrelated"]
    assert "hpc-hidden-1" not in result.output
    assert "hpc-hidden-2" not in result.output


def test_hpc_list_human_renderer_is_name_first_and_footer_free() -> None:
    from inspire.cli.commands.hpc.hpc_commands import _format_hpc_list_rows

    output = _format_hpc_list_rows(
        [
            {
                "name": "prep",
                "status": "RUNNING",
                "created_at": "2026-08-05 12:00:00",
            }
        ]
    )

    assert output.splitlines()[0].startswith("Name")
    assert "HPC Jobs" not in output
    assert "Total:" not in output


def test_hpc_list_all_expands_and_limit_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    calls: list[int] = []

    def fake_list_hpc_jobs(**kwargs):  # noqa: ANN001
        calls.append(kwargs["page_size"])
        count = kwargs["page_size"]
        return (
            [
                HPCJobInfo(
                    job_id=f"hpc-job-{index}",
                    name=f"job-{index}",
                    status="RUNNING",
                    entrypoint="bash run.sh",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_name="tester",
                    created_by_id="user-1",
                    project_id="project-1",
                    project_name="Project",
                    compute_group_name="CPU",
                    workspace_id=kwargs["workspace_id"],
                )
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(hpc_cmd_module.browser_api_module, "list_hpc_jobs", fake_list_hpc_jobs)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "hpc", "list", "--workspace", "cpu-room", "--all"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert len(payload["items"]) == 25
    assert "truncated" not in payload

    calls.clear()
    conflict = CliRunner().invoke(
        cli_main,
        ["hpc", "list", "--workspace", "cpu-room", "--all", "--limit", "3"],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
    assert calls == []


def test_hpc_list_workspace_all_fans_out_and_uses_visible_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    class _AllWorkspaceSession:
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "CPU East", "ws-b": "CPU West"}

    calls: list[str] = []
    monkeypatch.setattr(
        hpc_cmd_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )

    def fake_list_hpc_jobs(**kwargs):  # noqa: ANN001
        workspace_id = kwargs["workspace_id"]
        calls.append(workspace_id)
        return (
            [
                HPCJobInfo(
                    job_id=f"hpc-{workspace_id}",
                    name=f"job-{workspace_id[-1]}",
                    status="RUNNING",
                    entrypoint="bash run.sh",
                    created_at="1770000000",
                    finished_at=None,
                    created_by_name="tester",
                    created_by_id="user-1",
                    project_id="project-1",
                    project_name="Project",
                    compute_group_name="CPU",
                    workspace_id=workspace_id,
                )
            ],
            1,
        )

    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_jobs",
        fake_list_hpc_jobs,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "hpc", "list", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["data"]["items"]
    assert calls == ["ws-a", "ws-b"]
    assert {row["workspace"] for row in rows} == {"CPU East", "CPU West"}
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
                    "instance_id": "hpc-job-001-launcher-deadbeef",
                    "name": "launcher",
                    "component": "launcher",
                    "instance_type": "pod",
                    "status": "Running",
                    "node": "cpu-node-a",
                    "created_at": 1770000000,
                    "resource_spec": {
                        "cpu_count": 8,
                        "memory_size_gib": 64,
                        "gpu_count": 0,
                    },
                    "backend": "browser",
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
    assert result.output.splitlines()[0].lstrip().startswith("Name")
    assert "launcher" in result.output
    assert "8 CPU, 64 GiB, 0 GPU" in result.output
    assert "HPC Instances" not in result.output
    assert "Total:" not in result.output
    assert "hpc-job-001-launcher-deadbeef" not in result.output
    assert "cpu-node-a" not in result.output
    assert "backend" not in result.output

    json_result = runner.invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "instances",
            "prep",
            "--workspace",
            "cpu-room",
            "--limit",
            "42",
        ],
    )
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["data"] == {
        "name": "prep",
        "items": [
            {
                "name": "launcher",
                "status": "Running",
                "role": "launcher",
                "type": "pod",
                "resource": "8 CPU, 64 GiB, 0 GPU",
                "rank": 0,
            }
        ],
    }


def test_hpc_instances_default_budget_notifies_and_keeps_name_resolution_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    captured: dict[str, Any] = {}
    session = hpc_cmd_module.get_web_session()

    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_jobs",
        lambda **kwargs: (
            captured.update(kwargs)
            or (
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
        ),
    )

    def fake_list_hpc_job_instances(job_id, *, limit, session):  # noqa: ANN001
        captured["instance_limit"] = limit
        return (
            [
                {
                    "name": f"worker-{index}",
                    "status": "Running",
                    "component": "worker",
                }
                for index in range(20)
            ],
            25,
        )

    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_job_instances",
        fake_list_hpc_job_instances,
    )

    result = CliRunner().invoke(
        cli_main,
        ["hpc", "instances", "prep", "--workspace", "cpu-room"],
    )

    assert result.exit_code == 0, result.output
    assert captured["page_size"] == 500
    assert captured["instance_limit"] == 20
    assert "Showing 20 of 25. Use --all for the full list." in result.output
    assert session is not None

    json_result = CliRunner().invoke(
        cli_main,
        ["--json", "hpc", "instances", "prep", "--workspace", "cpu-room"],
    )
    assert json_result.exit_code == 0, json_result.output
    metadata = json.loads(json_result.output)["data"]
    assert metadata["name"] == "prep"
    assert len(metadata["items"]) == 20
    assert metadata["shown"] == 20
    assert metadata["total"] == 25
    assert metadata["truncated"] is True
    assert "limit" not in metadata


def test_hpc_instances_all_expands_and_json_conflict_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_cmd_module

    calls: list[int] = []
    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_jobs",
        lambda **kwargs: (
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
        ),
    )

    def fake_list_hpc_job_instances(job_id, *, limit, session):  # noqa: ANN001
        calls.append(limit)
        count = 25 if limit == 25 else 20
        return (
            [
                {
                    "instance_id": f"hpc-job-001-worker-{index}",
                    "name": f"worker-{index}",
                    "status": "Running",
                }
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(
        hpc_cmd_module.browser_api_module,
        "list_hpc_job_instances",
        fake_list_hpc_job_instances,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "instances",
            "prep",
            "--workspace",
            "cpu-room",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert set(payload) == {"name", "items"}
    assert payload["name"] == "prep"
    assert len(payload["items"]) == 25
    assert all(
        set(item) <= {"name", "status", "role", "type", "resource", "rank"}
        for item in payload["items"]
    )
    assert "hpc-job-001-worker" not in result.output

    conflict = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "instances",
            "prep",
            "--workspace",
            "cpu-room",
            "--all",
            "--limit",
            "3",
        ],
    )

    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output


def test_hpc_stop_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = patch_hpc_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.hpc import hpc_commands as hpc_mod

    monkeypatch.setattr(hpc_mod, "_resolve_hpc_name_in_workspace", lambda *a, **kw: "hpc-job-999")
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--json", "hpc", "stop", "hpc-demo", "--workspace", "cpu-room"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"] == {
        "name": "hpc-demo",
        "status": "stopped",
    }
    assert "stopped" not in payload["data"]
    assert api.calls["stop_hpc_job"] == "hpc-job-999"
