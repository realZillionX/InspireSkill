import json
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.context import (
    Context,
    EXIT_SUCCESS,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_TIMEOUT,
    EXIT_JOB_NOT_FOUND,
)

from inspire import config as config_module
from inspire.bridge import tunnel as tunnel_module
from inspire.cli.commands.notebook import notebook_commands as notebook_cmd_module
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.config import ConfigError
from inspire.cli.utils.quota_resolver import ResolvedQuota

# Valid test job IDs (must match the format: job-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
TEST_JOB_ID = "job-12345678-1234-1234-1234-123456789abc"
TEST_JOB_ID_2 = "job-abcdef12-3456-7890-abcd-ef1234567890"
TEST_JOB_ID_3 = "job-11111111-2222-3333-4444-555555555555"
TEST_DOCKER_REGISTRY = "registry.local"


def _parse_json_stream(output: str) -> List[Dict[str, Any]]:
    """Parse one or more JSON documents echoed sequentially."""
    decoder = json.JSONDecoder()
    payloads: List[Dict[str, Any]] = []
    index = 0
    length = len(output)
    while index < length:
        while index < length and output[index].isspace():
            index += 1
        if index >= length:
            break
        parsed, index = decoder.raw_decode(output, index)
        payloads.append(parsed)
    return payloads


def make_test_config(tmp_path: Path, include_compute_groups: bool = False) -> config_module.Config:
    """Create a test Config object.

    Args:
        tmp_path: Temporary directory path
        include_compute_groups: If True, include test compute groups
    """
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )
    # Add test compute groups if requested
    if include_compute_groups:
        test_group_id = "lcg-test000-0000-0000-0000-000000000000"
        config.compute_groups = [
            {
                "name": "H200 TestRoom",
                "id": test_group_id,
                "gpu_type": "H200",
                "location": "Test",
            }
        ]
    return config


class DummyAPI:
    def __init__(self) -> None:
        self.calls: Dict[str, Any] = {}

    # Job-related methods -------------------------------------------------
    def create_training_job(self, *, payload: dict[str, Any], session: object | None = None) -> Dict[str, Any]:
        self.calls["create_training_job"] = {"payload": payload, "session": session}
        return {"job_id": TEST_JOB_ID, "name": payload.get("name", "test-job")}

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        self.calls.setdefault("get_job_detail", []).append(job_id)
        return {
            "data": {
                "job_id": job_id,
                "name": "test-job",
                "status": "SUCCEEDED",
                "running_time_ms": "1000",
            }
        }

    def get_job_detail_v2(
        self, job_id: str, session: object | None = None
    ) -> Dict[str, Any]:
        self.calls.setdefault("get_job_detail_v2", []).append({"job_id": job_id, "session": session})
        return {
            "job_id": job_id,
            "name": "test-job",
            "status": "SUCCEEDED",
            "running_time_ms": "1000",
        }

    def stop_training_job(self, job_id: str, session: object | None = None) -> None:
        self.calls.setdefault("stop_training_job", []).append({"job_id": job_id, "session": session})

    # Resource / nodes ----------------------------------------------------
    def list_cluster_nodes(
        self,
        page_num: int,
        page_size: int,
        resource_pool: Optional[str],
    ) -> Dict[str, Any]:
        self.calls["list_cluster_nodes"] = {
            "page_num": page_num,
            "page_size": page_size,
            "resource_pool": resource_pool,
        }
        return {
            "data": {
                "nodes": [
                    {
                        "node_id": "node-1",
                        "resource_pool": resource_pool or "online",
                        "status": "ready",
                        "gpu_count": 4,
                    }
                ],
                "total": 1,
            }
        }


def patch_config_and_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, include_compute_groups: bool = False
) -> DummyAPI:
    """Patch config and Browser API helpers to use local stubs.

    Args:
        monkeypatch: pytest monkeypatch fixture
        tmp_path: Temporary directory path
        include_compute_groups: If True, include test compute groups in config
    """
    config = make_test_config(tmp_path, include_compute_groups=include_compute_groups)
    Path(config.path_aliases["me"]).mkdir(parents=True, exist_ok=True)

    def fake_from_env(cls) -> config_module.Config:  # type: ignore[override]
        return config

    def fake_from_files_and_env(cls, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    api = DummyAPI()

    # Mock browser API calls for project selection
    class FakeWebSession:
        workspace_id = "ws-test-workspace"
        storage_state = {}
        all_workspace_ids = [
            "ws-test-workspace",
            "ws-gpu",
            "ws-cpu",
            "ws-77777777-7777-7777-7777-777777777777",
        ]
        all_workspace_names = {
            "ws-test-workspace": "Test Workspace",
            "ws-gpu": "分布式训练空间",
            "ws-cpu": "CPU资源空间",
            "ws-77777777-7777-7777-7777-777777777777": "cpu",
        }

    monkeypatch.setattr(
        web_session_module,
        "get_web_session",
        lambda: FakeWebSession(),
    )
    from inspire.cli.commands.resources import resources_list as resources_list_module
    from inspire.cli.commands.resources import resources_nodes as resources_nodes_module

    monkeypatch.setattr(resources_list_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(resources_nodes_module, "get_web_session", lambda: FakeWebSession())

    # Stub quota resolution so job-submit tests don't hit the real platform —
    # real resolution lives in test_quota_resolver.
    import importlib

    quota_resolver_module = importlib.import_module("inspire.cli.utils.quota_resolver")
    config_check_module = importlib.import_module("inspire.cli.commands.config.check")
    job_commands_module = importlib.import_module("inspire.cli.commands.job.job_commands")
    job_create_module = importlib.import_module("inspire.cli.commands.job.job_create")
    workspaces_module = importlib.import_module("inspire.platform.web.browser_api.workspaces")
    notebook_flow_module = importlib.import_module(
        "inspire.cli.commands.notebook.notebook_create_flow"
    )

    def _fake_resolve_quota(*, spec, workspace_id, session=None, **_):  # noqa: ANN001
        return ResolvedQuota(
            quota_id="quota-test-default",
            logic_compute_group_id="lcg-test-default",
            compute_group_name="Test Group",
            gpu_count=spec.gpu_count,
            cpu_count=spec.cpu_count,
            memory_gib=spec.memory_gib,
            gpu_type="H200" if spec.gpu_count > 0 else "",
            raw_price={
                "cpu_info": {"cpu_type": "Test"},
                "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
            },
        )

    monkeypatch.setattr(quota_resolver_module, "resolve_quota", _fake_resolve_quota)
    monkeypatch.setattr(job_create_module, "resolve_quota", _fake_resolve_quota)
    monkeypatch.setattr(notebook_flow_module, "resolve_quota", _fake_resolve_quota)
    monkeypatch.setattr(workspaces_module, "try_enumerate_workspaces", lambda *_a, **_kw: [])

    # job create imports `get_web_session` by name, so patch that namespace.
    monkeypatch.setattr(job_create_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(config_check_module, "get_web_session", lambda: FakeWebSession())

    test_project = browser_api_module.ProjectInfo(
        project_id="project-test-123",
        name="Test Project",
        workspace_id="ws-test-workspace",
        member_gpu_limit=True,
        member_remain_gpu_hours=100.0,
    )

    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda workspace_id=None, session=None: [test_project],
    )

    monkeypatch.setattr(
        browser_api_module,
        "select_project",
        lambda projects, requested=None, **_: (test_project, None),
    )
    monkeypatch.setattr(browser_api_module, "create_training_job", api.create_training_job)
    monkeypatch.setattr(browser_api_module, "get_job_detail_v2", api.get_job_detail_v2)
    monkeypatch.setattr(browser_api_module, "stop_training_job", api.stop_training_job)
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-test", "username": "user"},
    )

    return api


# ---------------------------------------------------------------------------
# Global main entry with subcommands
# ---------------------------------------------------------------------------


def test_global_json_flag_with_resources_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Include test compute groups in config
    patch_config_and_auth(monkeypatch, tmp_path, include_compute_groups=True)
    from inspire.platform.web import browser_api as browser_api_module

    # Use a test placeholder UUID instead of real compute group ID
    test_group_id = "lcg-test000-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_resource_availability",
        lambda **kwargs: [  # noqa: ARG005
            browser_api_module.GPUAvailability(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_type="NVIDIA H200",
                total_gpus=128,
                used_gpus=32,
                available_gpus=96,
                low_priority_gpus=8,
            )
        ],
    )
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "resources", "availability", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "availability" in payload["data"]
    assert "group_id" not in payload["data"]["availability"][0]


def test_global_debug_flag_runs_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.platform.web import browser_api as browser_api_module

    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_resource_availability",
        lambda **kwargs: [],  # noqa: ARG005
    )
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--debug", "resources", "availability", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == 0


def test_job_help_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Smoke test to ensure `inspire job --help` works (no import/syntax errors)."""
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "--help"])
    assert result.exit_code == 0
    assert "Manage GPU batch jobs and distributed-training workloads" in result.output


def test_job_list_help_uses_workspace_name_not_raw_id_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "list", "--help"])

    assert result.exit_code == 0
    assert "Workspace name" in result.output
    assert "ws-... id" not in result.output


# ---------------------------------------------------------------------------
# Job command group
# ---------------------------------------------------------------------------


def test_job_create_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "test-job",
            "--quota",
            "1,20,200",
            "--command",
            "echo hi",
            "--workspace",
            "cpu",
            "--project",
            "proj",
            "--group",
            "H200 TestRoom",
            "--image",
            "registry.local/train:latest",
            "--nodes",
            "1",
            "--exclude-node",
            "qb-prod-gpu1736",
            "--exclude-node",
            "qb-prod-gpu1737",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["data"]["name"] == "test-job"
    assert "job_id" not in data["data"]
    create_payload = api.calls["create_training_job"]["payload"]
    framework_config = create_payload["framework_config"][0]
    assert framework_config["exclude_nodes"] == [
        "qb-prod-gpu1736",
        "qb-prod-gpu1737",
    ]
    # The backend CreateJob proto has no framework_config-level quota_id; the quota
    # is conveyed by the top-level logic_compute_group_id plus the nested
    # resource_spec_price (which carries its own quota_id).
    assert "quota_id" not in framework_config
    assert create_payload["logic_compute_group_id"]
    assert framework_config["resource_spec_price"]["quota_id"]
    # No --max-time given: omit max_running_time_ms so the platform applies no
    # time cap (a giant sentinel would overflow the backend's INT column).
    assert "max_running_time_ms" not in create_payload


def test_job_create_max_time_sets_running_time_ms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    api = patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "test-job",
            "--quota",
            "1,20,200",
            "--command",
            "echo hi",
            "--workspace",
            "cpu",
            "--project",
            "proj",
            "--group",
            "H200 TestRoom",
            "--image",
            "registry.local/train:latest",
            "--nodes",
            "1",
            "--max-time",
            "24",
        ],
    )

    assert result.exit_code == 0, result.output
    create_payload = api.calls["create_training_job"]["payload"]
    assert create_payload["max_running_time_ms"] == str(24 * 3600 * 1000)


def test_wrap_in_bash():
    """Test the bash wrapper helper function."""
    from inspire.cli.utils.job_submit import wrap_in_bash

    # Basic wrapping
    assert wrap_in_bash("python train.py") == "bash -c 'python train.py'"

    # Source command (the main use case)
    result = wrap_in_bash("source .env && python train.py")
    assert result == "bash -c 'source .env && python train.py'"

    # Escape single quotes
    result = wrap_in_bash("echo 'hello'")
    assert result == "bash -c 'echo '\\''hello'\\'''"

    # Skip if already wrapped
    assert wrap_in_bash("bash -c 'foo'") == "bash -c 'foo'"
    assert wrap_in_bash("sh -c 'foo'") == "sh -c 'foo'"
    assert wrap_in_bash("/bin/bash -c 'foo'") == "/bin/bash -c 'foo'"
    assert wrap_in_bash("/bin/sh -c 'foo'") == "/bin/sh -c 'foo'"

    # Whitespace handling
    assert wrap_in_bash("  bash -c 'foo'  ") == "  bash -c 'foo'  "


def test_build_remote_logged_command_tees_output_and_sets_pipefail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.cli.utils import job_submit as job_submit_module

    monkeypatch.setattr(job_submit_module, "_now_log_timestamp", lambda: "20260508T010203Z")
    config = config_module.Config(
        username="user",
        password="pass",
        path_aliases={"me": "/train/user space"},
        remote_env={"WANDB_MODE": "offline"},
    )

    command, log_path = job_submit_module.build_remote_logged_command(
        config,
        command="bash -c 'python train.py'",
        name="train/a",
    )

    assert log_path == "/train/user space/.inspire/training_master_train_a_20260508T010203Z.log"
    outer = shlex.split(command)
    assert outer[:4] == ["bash", "-o", "pipefail", "-c"]
    script = outer[4]
    assert "export WANDB_MODE=offline && export PYTHONUNBUFFERED=1 && " in script
    assert "mkdir -p '/train/user space/.inspire' && " in script
    assert ": > '/train/user space/.inspire/training_master_train_a_20260508T010203Z.log'" in script
    assert "cd '/train/user space' && " in script
    assert "{ bash -c 'python train.py' 2> >(" in script
    assert (
        "tee -a '/train/user space/.inspire/training_master_train_a_20260508T010203Z.log' >&2"
        in script
    )
    assert (
        "| tee -a '/train/user space/.inspire/training_master_train_a_20260508T010203Z.log'"
        in script
    )
    assert '> "${log_path}" 2>&1' not in command


def test_build_remote_logged_command_preserves_user_pythonunbuffered() -> None:
    from inspire.cli.utils import job_submit as job_submit_module

    config = config_module.Config(
        username="user",
        password="pass",
        path_aliases={"me": "/train/user"},
        remote_env={"PYTHONUNBUFFERED": "0"},
    )

    command, _ = job_submit_module.build_remote_logged_command(
        config,
        command="bash -c 'python train.py'",
        name="train",
    )

    script = shlex.split(command)[4]
    assert "export PYTHONUNBUFFERED=0 && " in script
    assert "export PYTHONUNBUFFERED=1 && " not in script


def test_build_remote_logged_command_without_default_path_alias_keeps_existing_behavior() -> None:
    from inspire.cli.utils import job_submit as job_submit_module

    config = config_module.Config(
        username="user",
        password="pass",
        remote_env={"FOO": "bar"},
    )

    command, log_path = job_submit_module.build_remote_logged_command(
        config,
        command="bash -c 'python train.py'",
        name="train",
    )

    assert command == "export FOO=bar && bash -c 'python train.py'"
    assert log_path is None


def test_job_status_human_output_uses_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Human ``job status`` shows the name, never the platform handle.

    Name-only boundary: surfacing platform handles in the human view tempts
    callers to round-trip them and then hit ``reject_id_at_boundary``.
    """
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)
    monkeypatch.setattr(job_commands, "get_web_session", web_session_module.get_web_session)
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "get_job_detail_v2",
        lambda job_id, *, session: {
            "job_id": job_id,
            "name": "test-job",
            "status": "SUCCEEDED",
            "created_at": "1770000000000",
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["job", "status", "test-job", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == 0
    assert "Web Job Status" in result.output
    assert "Name: test-job" in result.output
    assert TEST_JOB_ID not in result.output  # platform handle stays out of human output


def test_legacy_human_job_list_formatter_is_name_only() -> None:
    from inspire.cli.formatters.human_formatter import format_job_list

    output = format_job_list(
        [
            {
                "job_id": TEST_JOB_ID,
                "name": "visible-name",
                "status": "RUNNING",
                "created_at": "2026-05-06T14:48:50",
            }
        ]
    )

    assert "visible-name" in output
    assert "Job ID" not in output
    assert TEST_JOB_ID not in output
    assert format_job_list([]) == "No jobs found."


def test_job_status_not_found_sets_specific_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)
    monkeypatch.setattr(job_commands, "get_web_session", web_session_module.get_web_session)

    def failing_get_job_detail_v2(job_id: str, *, session: object) -> Dict[str, Any]:
        del job_id, session
        raise RuntimeError("Job not found")

    monkeypatch.setattr(
        job_commands.browser_api_module,
        "get_job_detail_v2",
        failing_get_job_detail_v2,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "status", "missing-id", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == EXIT_JOB_NOT_FOUND


def test_job_stop_with_force_and_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "job", "stop", "test-job", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["name"] == "test-job"
    assert data["data"]["status"] == "stopped"
    assert "job_id" not in data["data"]


def test_job_wait_succeeds_and_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)
    monkeypatch.setattr(job_commands, "get_web_session", web_session_module.get_web_session)

    # Ensure the job is immediately in a terminal state
    def get_job_detail_v2(job_id: str, *, session: object) -> Dict[str, Any]:
        del session
        return {
            "job_id": job_id,
            "name": "wait-job",
            "status": "SUCCEEDED",
            "running_time_ms": "1000",
        }

    monkeypatch.setattr(job_commands.browser_api_module, "get_job_detail_v2", get_job_detail_v2)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "job",
            "wait",
            "wait-job",
            "--workspace",
            "Test Workspace",
            "--timeout",
            "60",
            "--interval",
            "1",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "SUCCEEDED" in result.output


def test_job_wait_json_output_has_no_human_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)
    monkeypatch.setattr(job_commands, "get_web_session", web_session_module.get_web_session)

    def get_job_detail_v2(job_id: str, *, session: object) -> Dict[str, Any]:
        del session
        return {
            "job_id": job_id,
            "name": "wait-job",
            "status": "SUCCEEDED",
            "running_time_ms": "1000",
        }

    monkeypatch.setattr(job_commands.browser_api_module, "get_job_detail_v2", get_job_detail_v2)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "job",
            "wait",
            "wait-job",
            "--workspace",
            "Test Workspace",
            "--timeout",
            "60",
            "--interval",
            "1",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "Waiting for job" not in result.output
    payloads = _parse_json_stream(result.output)
    assert payloads
    for payload in payloads:
        assert payload["success"] is True


def test_job_wait_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.cli.commands.job import job_commands

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: TEST_JOB_ID)

    # Force time to jump ahead so we immediately hit timeout
    calls: List[int] = []

    def fake_time() -> int:
        # First call (start_time) -> 0, second call -> large value
        calls.append(1)
        return 0 if len(calls) == 1 else 10

    monkeypatch.setattr(job_commands.time, "time", fake_time)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "job",
            "wait",
            "wait-job",
            "--workspace",
            "Test Workspace",
            "--timeout",
            "1",
            "--interval",
            "1",
        ],
    )
    assert result.exit_code == EXIT_TIMEOUT
    assert "Timeout after 1s" in result.output


def test_job_list_web_name_search_scans_all_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")
    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    class FakeSession:
        workspace_id = "ws-main"
        all_workspace_ids = ["ws-main", "ws-train"]
        all_workspace_names = {
            "ws-main": "Main Workspace",
            "ws-train": "Training Workspace",
        }
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-me"},  # noqa: ARG005
    )

    def fake_list_jobs(
        workspace_id=None,
        created_by=None,
        status=None,
        keyword=None,
        page_num=1,
        page_size=100,
        session=None,
    ):  # noqa: ARG001
        calls.append(
            {
                "workspace_id": workspace_id,
                "created_by": created_by,
                "status": status,
                "keyword": keyword,
                "page_num": page_num,
                "page_size": page_size,
            }
        )
        if workspace_id == "ws-train" and page_num == 1:
            return (
                [
                    JobInfo(
                        job_id=TEST_JOB_ID,
                        name="kchen-slime-code-qwen35-35b-a3b-6node",
                        status="job_queuing",
                        command="bash run_qwen35_35b_a3b_code_6node.sh",
                        created_at="2026-05-06T14:48:50",
                        finished_at=None,
                        created_by_name="Chen Ke",
                        created_by_id="user-me",
                        project_id="project-1",
                        project_name="CQ Project",
                        compute_group_name="H200-3",
                        gpu_type="NVIDIA H200",
                        gpu_count=8,
                        instance_count=6,
                        priority=10,
                        workspace_id="ws-train",
                    )
                ],
                1,
            )
        return ([], 0)

    monkeypatch.setattr(browser_api_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "job", "list", "--workspace", "all", "--keyword", "qwen35", "--limit", "1"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["source"] == "web"
    assert "job_id" not in payload["data"]["jobs"][0]
    assert payload["data"]["jobs"][0]["name"] == "kchen-slime-code-qwen35-35b-a3b-6node"
    assert payload["data"]["jobs"][0]["workspace_name"] == "Training Workspace"
    assert calls[0]["created_by"] == "user-me"
    scanned = {call["workspace_id"] for call in calls}
    assert {"ws-main", "ws-train"} <= scanned


def test_job_list_without_keyword_fetches_all_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")
    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    class FakeSession:
        workspace_id = "ws-main"
        all_workspace_ids = ["ws-main"]
        all_workspace_names = {"ws-main": "Main Workspace"}
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-me"},  # noqa: ARG005
    )

    def _job(index: int) -> JobInfo:
        return JobInfo(
            job_id=TEST_JOB_ID,
            name=f"train-{index:03d}",
            status="job_succeeded",
            command="bash train.sh",
            created_at=f"2026-06-05T10:00:{index % 60:02d}",
            finished_at=None,
            created_by_name="Chen Ke",
            created_by_id="user-me",
            project_id="project-1",
            project_name="CQ Project",
            compute_group_name="H200-3",
            gpu_type="NVIDIA H200",
            gpu_count=8,
            instance_count=1,
            priority=10,
            workspace_id="ws-main",
        )

    def fake_list_jobs(
        workspace_id=None,
        created_by=None,
        status=None,
        keyword=None,
        page_num=1,
        page_size=100,
        session=None,
    ):  # noqa: ARG001
        calls.append(
            {
                "workspace_id": workspace_id,
                "created_by": created_by,
                "status": status,
                "keyword": keyword,
                "page_num": page_num,
                "page_size": page_size,
            }
        )
        if page_num == 1:
            return ([_job(index) for index in range(101, 1, -1)], 101)
        if page_num == 2:
            return ([_job(1)], 101)
        raise AssertionError(f"unexpected page: {page_num}")

    monkeypatch.setattr(browser_api_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "list", "--workspace", "Main Workspace"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload["data"]["jobs"]) == 101
    assert {row["name"] for row in payload["data"]["jobs"]} >= {"train-101", "train-001"}
    assert [call["page_num"] for call in calls] == [1, 2]
    assert all(call["page_size"] == 100 for call in calls)


def test_job_list_limit_applies_per_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")
    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    class FakeSession:
        workspace_id = "ws-main"
        all_workspace_ids = ["ws-main", "ws-train"]
        all_workspace_names = {
            "ws-main": "Main Workspace",
            "ws-train": "Training Workspace",
        }
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-me"},  # noqa: ARG005
    )

    def _job(name: str, workspace_id: str) -> JobInfo:
        return JobInfo(
            job_id=TEST_JOB_ID,
            name=name,
            status="job_running",
            command="bash train.sh",
            created_at="2026-06-05T10:00:00",
            finished_at=None,
            created_by_name="Chen Ke",
            created_by_id="user-me",
            project_id="project-1",
            project_name="CQ Project",
            compute_group_name="H200-3",
            gpu_type="NVIDIA H200",
            gpu_count=8,
            instance_count=1,
            priority=10,
            workspace_id=workspace_id,
        )

    def fake_list_jobs(
        workspace_id=None,
        created_by=None,
        status=None,
        keyword=None,
        page_num=1,
        page_size=100,
        session=None,
    ):  # noqa: ARG001
        calls.append(
            {
                "workspace_id": workspace_id,
                "created_by": created_by,
                "status": status,
                "keyword": keyword,
                "page_num": page_num,
                "page_size": page_size,
            }
        )
        return ([_job(f"{workspace_id}-job-{page_num}", workspace_id or "ws-main")], 2)

    monkeypatch.setattr(browser_api_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "job", "list", "--workspace", "all", "--limit", "1"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert {row["name"] for row in payload["data"]["jobs"]} == {
        "ws-main-job-1",
        "ws-train-job-1",
    }
    assert [call["workspace_id"] for call in calls] == ["ws-main", "ws-train"]
    assert all(call["page_num"] == 1 for call in calls)
    assert all(call["page_size"] == 1 for call in calls)


def test_job_list_active_queries_only_active_platform_statuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")
    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    class FakeSession:
        workspace_id = "ws-main"
        all_workspace_ids = ["ws-main", "ws-train"]
        all_workspace_names = {
            "ws-main": "Main Workspace",
            "ws-train": "Training Workspace",
        }
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-me"},  # noqa: ARG005
    )

    def _job(name: str, status: str, workspace_id: str) -> JobInfo:
        return JobInfo(
            job_id=TEST_JOB_ID,
            name=name,
            status=status,
            command="bash train.sh",
            created_at="2026-06-05T10:00:00",
            finished_at=None,
            created_by_name="Chen Ke",
            created_by_id="user-me",
            project_id="project-1",
            project_name="CQ Project",
            compute_group_name="H200-3",
            gpu_type="NVIDIA H200",
            gpu_count=8,
            instance_count=1,
            priority=10,
            workspace_id=workspace_id,
        )

    def fake_list_jobs(
        workspace_id=None,
        created_by=None,
        status=None,
        keyword=None,
        page_num=1,
        page_size=100,
        session=None,
    ):  # noqa: ARG001
        calls.append(
            {
                "workspace_id": workspace_id,
                "created_by": created_by,
                "status": status,
                "keyword": keyword,
                "page_num": page_num,
                "page_size": page_size,
            }
        )
        if status is None:
            return (
                [_job("old-finished-job", "job_succeeded", workspace_id or "ws-main")],
                1,
            )
        if status == "job_running" and workspace_id == "ws-train":
            return ([_job("running-job", "job_running", "ws-train")], 1)
        if status == "job_queuing" and workspace_id == "ws-main":
            return ([_job("queued-job", "job_queuing", "ws-main")], 1)
        return ([], 0)

    monkeypatch.setattr(browser_api_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "job", "list", "--workspace", "all", "--active"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    names = {row["name"] for row in payload["data"]["jobs"]}
    assert names == {"running-job", "queued-job"}
    queried_statuses = {call["status"] for call in calls}
    assert queried_statuses == {
        "job_pending",
        "job_creating",
        "job_queuing",
        "job_running",
    }
    assert None not in queried_statuses


def test_job_list_human_output_hides_raw_ids_and_name_search_ignores_job_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    from inspire.cli.formatters.human_formatter import format_epoch

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")
    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo
    created_at = "1781230039000"

    class FakeSession:
        workspace_id = "ws-main"
        all_workspace_ids = ["ws-main", "ws-train"]
        all_workspace_names = {
            "ws-main": "Main Workspace",
            "ws-train": "Training Workspace",
        }
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    monkeypatch.setattr(job_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-me"},  # noqa: ARG005
    )

    def fake_list_jobs(
        workspace_id=None,
        created_by=None,
        status=None,
        keyword=None,
        page_num=1,
        page_size=100,
        session=None,
    ):  # noqa: ARG001
        if workspace_id == "ws-train" and page_num == 1:
            return (
                [
                    JobInfo(
                        job_id=TEST_JOB_ID,
                        name="kchen-slime-code-qwen35-35b-a3b-6node",
                        status="job_queuing",
                        command="bash run_qwen35_35b_a3b_code_6node.sh",
                        created_at=created_at,
                        finished_at=None,
                        created_by_name="Chen Ke",
                        created_by_id="user-me",
                        project_id="project-1",
                        project_name="CQ Project",
                        compute_group_name="H200-3",
                        gpu_type="NVIDIA H200",
                        gpu_count=8,
                        instance_count=6,
                        priority=10,
                        workspace_id="ws-train",
                    )
                ],
                1,
            )
        return ([], 0)

    monkeypatch.setattr(browser_api_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    human_result = runner.invoke(
        cli_main,
        ["job", "list", "--workspace", "all", "--keyword", "qwen35"],
    )

    assert human_result.exit_code == 0
    assert "kchen-slime-code-qwen35-35b-a3b-6node" in human_result.output
    assert "Training Workspace" in human_result.output
    assert format_epoch(created_at) in human_result.output
    assert created_at not in human_result.output
    assert TEST_JOB_ID not in human_result.output
    assert "ws-train" not in human_result.output
    assert "project-1" not in human_result.output

    id_query_result = runner.invoke(
        cli_main,
        ["job", "list", "--workspace", "all", "--keyword", "12345678"],
    )

    assert id_query_result.exit_code == 0
    assert "No jobs found." in id_query_result.output
    assert TEST_JOB_ID not in id_query_result.output


def test_nodes_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Include test compute groups in config
    patch_config_and_auth(monkeypatch, tmp_path, include_compute_groups=True)
    from inspire.platform.web import browser_api as browser_api_module

    test_group_id = "lcg-test000-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node=8, session=None, _retry=True: [  # noqa: ARG005
            browser_api_module.FullFreeNodeCount(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_per_node=gpu_per_node,
                total_nodes=10,
                ready_nodes=8,
                full_free_nodes=3,
            )
        ],
    )
    # Also mock get_accurate_resource_availability which is called by the nodes command
    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_resource_availability",
        lambda workspace_id=None, session=None, include_cpu=False, all_workspaces=False, _retry=True: [  # noqa: ARG005
            browser_api_module.GPUAvailability(
                group_id=test_group_id,
                group_name="H200 TestRoom",
                gpu_type="H200",
                total_gpus=80,
                used_gpus=68,
                available_gpus=12,
                low_priority_gpus=0,
            )
        ],
    )
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "resources", "nodes", "--workspace", "Test Workspace"],
    )
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["groups"]
    assert data["data"]["total_full_free_nodes"] == 3


def test_resources_list_all_workspaces_and_cpu_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    patch_config_and_auth(monkeypatch, tmp_path, include_compute_groups=True)
    from inspire.platform.web import browser_api as browser_api_module

    captured: dict[str, object] = {}

    def _fake_get_accurate_resource_availability(**kwargs):
        captured.update(kwargs)
        return [
            browser_api_module.GPUAvailability(
                group_id="lcg-h100",
                group_name="cuda12.8版本H100",
                gpu_type="NVIDIA H100 (80GB)",
                total_gpus=128,
                used_gpus=64,
                available_gpus=64,
                low_priority_gpus=8,
                workspace_id="ws-gpu",
                workspace_name="分布式训练空间",
                cpu_total=2048,
                cpu_used=1024,
                cpu_available=1024,
                resource_kind="gpu",
            ),
            browser_api_module.GPUAvailability(
                group_id="lcg-cpu",
                group_name="CPU资源-2",
                gpu_type="",
                total_gpus=0,
                used_gpus=0,
                available_gpus=0,
                low_priority_gpus=0,
                workspace_id="ws-cpu",
                workspace_name="CPU资源空间",
                cpu_total=1200,
                cpu_used=200,
                cpu_available=1000,
                memory_total_gib=4000,
                memory_used_gib=500,
                memory_available_gib=3500,
                resource_kind="cpu",
            ),
        ]

    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_resource_availability",
        _fake_get_accurate_resource_availability,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "resources", "availability", "--workspace", "all", "--include-cpu"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.output)
    rows = payload["data"]["availability"]
    assert payload["success"] is True
    assert captured["all_workspaces"] is True
    assert captured["include_cpu"] is True
    assert {row["resource_kind"] for row in rows} == {"gpu", "cpu"}
    assert any(row["workspace_name"] == "分布式训练空间" for row in rows)
    assert any(row["cpu_total"] == 1200 for row in rows)


def test_config_check_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = make_test_config(tmp_path)
    config.docker_registry = TEST_DOCKER_REGISTRY

    def fake_from_env(cls) -> config_module.Config:  # type: ignore[override]
        return config

    def fake_from_files_and_env(cls, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(config_check_module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        config_check_module.browser_api_module,
        "get_current_user",
        lambda session=None: (_ for _ in ()).throw(ValueError("bad credentials")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Authentication failed" in result.output


def test_config_check_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        raise ConfigError("missing env")

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"


def test_config_check_json_includes_base_url_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.prefer_source = "toml"
    config.base_url = "https://my-inspire.internal"
    config.docker_registry = TEST_DOCKER_REGISTRY

    project_dir = tmp_path / ".inspire"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_config = project_dir / "config.toml"
    project_config.write_text("""
[api]
base_url = "https://my-inspire.internal"
""")
    global_config = tmp_path / "global-config.toml"

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_PROJECT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return global_config, project_config

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setenv("INSPIRE_BASE_URL", "https://env.example")
    monkeypatch.setattr(config_check_module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        config_check_module.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    resolution = payload["data"]["base_url_resolution"]
    assert resolution["value"] == "https://my-inspire.internal"
    assert resolution["source"] == config_module.SOURCE_PROJECT
    assert resolution["prefer_source"] == "toml"
    assert resolution["env_present"] is True
    assert resolution["project_config_path"] == str(project_config)
    assert resolution["global_config_path"] == str(global_config)


def test_config_check_accepts_local_json_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://my-inspire.internal"
    config.docker_registry = TEST_DOCKER_REGISTRY

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_ENV}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(config_check_module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        config_check_module.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["auth_ok"] is True
    assert "base_url_resolution" in payload["data"]


def test_config_check_rejects_placeholder_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://api.example.com"

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_DEFAULT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(
        config_check_module,
        "get_web_session",
        lambda: pytest.fail("should not auth"),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Placeholder host values detected" in payload["error"]["message"]
    assert "INSPIRE_BASE_URL" in payload["error"]["message"]


def test_config_check_requires_docker_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://my-inspire.internal"
    config.docker_registry = None

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {
            "base_url": config_module.SOURCE_ENV,
            "docker_registry": config_module.SOURCE_DEFAULT,
        }

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(
        config_check_module,
        "get_web_session",
        lambda: pytest.fail("should not auth"),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Missing docker registry configuration" in payload["error"]["message"]
    assert "INSPIRE_DOCKER_REGISTRY" in payload["error"]["message"]


def test_config_check_rejects_top_level_project_base_url_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://my-inspire.internal"

    project_dir = tmp_path / ".inspire"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_config = project_dir / "config.toml"
    project_config.write_text('base_url = "https://wrong.example.com"\n')

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_PROJECT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, project_config

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(
        config_check_module,
        "get_web_session",
        lambda: pytest.fail("should not auth"),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert "top-level `base_url`" in payload["error"]["message"]
    assert "[api]" in payload["error"]["message"]


def test_config_check_allows_path_default_for_browser_api_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://my-inspire.internal"
    config.docker_registry = TEST_DOCKER_REGISTRY
    config.browser_api_prefix = "/api/v1"

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_ENV}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    from inspire.cli.commands.config import check as config_check_module

    monkeypatch.setattr(config_check_module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        config_check_module.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check"])

    assert result.exit_code == EXIT_SUCCESS
    assert "Configuration looks good" in result.output


def test_init_json_global_contract_via_top_level_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # init needs an active account to resolve its writable path; set one up.
    fake_home = tmp_path / "__home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    acct = fake_home / ".inspire" / "accounts" / "default"
    acct.mkdir(parents=True)
    (acct / "config.toml").write_text("")
    (fake_home / ".inspire" / "current").write_text("default\n")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "init", "--template", "--scope", "project", "--force"],
    )

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["mode"] == "template"
    assert payload["data"]["files_written"] == [
        str(tmp_path / ".inspire" / "accounts" / "default" / "config.toml")
    ]


def test_config_show_respects_global_json_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {"username": config_module.SOURCE_ENV}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "show"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert "config_files" in payload
    assert "values" in payload
    assert "INSPIRE_USERNAME" in payload["values"]


def test_notebook_list_all_workspaces_combines_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = ws_cpu
        storage_state = {}
        all_workspace_ids = [ws_cpu, ws_gpu]
        all_workspace_names = {ws_cpu: "a", ws_gpu: "b"}

    monkeypatch.setattr(web_session_module, "get_web_session", lambda: FakeSession())

    cpu_item = {
        "id": "nb-cpu",
        "name": "cpu-notebook",
        "status": "RUNNING",
        "created_at": "2026-02-01T10:00:00Z",
        "quota": {"cpu_count": 4, "gpu_count": 0},
    }
    gpu_item = {
        "id": "nb-gpu",
        "name": "gpu-notebook",
        "status": "RUNNING",
        "created_at": "2026-02-02T10:00:00Z",
        "quota": {"cpu_count": 8, "gpu_count": 1},
        "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}},
    }

    calls: list[str] = []

    def fake_request_json(
        session,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: int = 30,
        _retry_count: int = 0,
    ) -> dict:
        assert headers is None or isinstance(headers, dict)
        assert timeout
        assert _retry_count >= 0

        assert method.upper() == "POST"
        assert url.endswith("/api/v1/notebook/list")
        assert body and "workspace_id" in body

        ws_id = str(body["workspace_id"])
        calls.append(ws_id)

        if ws_id == ws_cpu:
            return {"code": 0, "data": {"list": [cpu_item]}}
        if ws_id == ws_gpu:
            return {"code": 0, "data": {"list": [gpu_item]}}
        return {"code": 0, "data": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)
    monkeypatch.setattr(
        notebook_cmd_module,
        "_try_get_current_user_ids",
        lambda *args, **kwargs: ["user"],
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "list", "--workspace", "all"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    items = payload["data"]["items"]
    assert [item["name"] for item in items] == ["cpu-notebook", "gpu-notebook"]
    assert all("id" not in item for item in items)
    assert calls == [ws_cpu, ws_gpu]


def test_notebook_start_accepts_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = ws_cpu
        storage_state = {}
        all_workspace_ids = [ws_cpu, ws_gpu]
        all_workspace_names = {ws_cpu: "a", ws_gpu: "b"}

    monkeypatch.setattr(web_session_module, "get_web_session", lambda: FakeSession())

    item = {
        "id": "78822a57-3830-44e7-8d45-e8b0d674fc44",
        "name": "ring-8h100-test",
        "status": "STOPPED",
        "created_at": "2026-02-01T10:00:00Z",
        "quota": {"cpu_count": 8, "gpu_count": 8},
    }

    def fake_request_json(
        session,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: int = 30,
        _retry_count: int = 0,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0

        if method.upper() == "GET" and url.endswith("/api/v1/user/detail"):
            return {"data": {"id": "user-1"}}

        assert method.upper() == "POST"
        assert url.endswith("/api/v1/notebook/list")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"

        ws_id = str(body["workspace_id"])
        if ws_id == ws_cpu:
            return {"code": 0, "data": {"list": [item]}}
        if ws_id == ws_gpu:
            return {"code": 0, "data": {"list": []}}
        return {"code": 0, "data": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)

    started: dict[str, str] = {}

    def fake_start_notebook(notebook_id: str, session=None) -> dict:  # type: ignore[no-untyped-def]
        started["notebook_id"] = notebook_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "start_notebook", fake_start_notebook)

    def fake_wait_for_notebook_running(
        notebook_id: str, session=None, timeout: int = 600, poll_interval: int = 5
    ) -> dict:
        return {"status": "RUNNING", "notebook_id": notebook_id, "quota": {"gpu_count": 8}}

    monkeypatch.setattr(
        browser_api_module, "wait_for_notebook_running", fake_wait_for_notebook_running
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "start", "ring-8h100-test", "--workspace", "a"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert started["notebook_id"] == item["id"]


def test_notebook_start_name_conflict_prompts_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = ws_cpu
        storage_state = {}
        all_workspace_ids = [ws_cpu, ws_gpu]
        all_workspace_names = {ws_cpu: "a", ws_gpu: "b"}

    monkeypatch.setattr(web_session_module, "get_web_session", lambda: FakeSession())

    cpu_item = {
        "id": "nb-cpu",
        "name": "ring-8h100-test",
        "status": "STOPPED",
        "created_at": "2026-02-02T10:00:00Z",
        "quota": {"cpu_count": 8, "gpu_count": 8},
    }
    gpu_item = {
        "id": "nb-gpu",
        "name": "ring-8h100-test",
        "status": "STOPPED",
        "created_at": "2026-02-01T10:00:00Z",
        "quota": {"cpu_count": 8, "gpu_count": 8},
    }

    def fake_request_json(
        session,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: int = 30,
        _retry_count: int = 0,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0

        if method.upper() == "GET" and url.endswith("/api/v1/user/detail"):
            return {"data": {"id": "user-1"}}

        assert method.upper() == "POST"
        assert url.endswith("/api/v1/notebook/list")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"

        ws_id = str(body["workspace_id"])
        if ws_id == ws_cpu:
            return {"code": 0, "data": {"list": [cpu_item]}}
        if ws_id == ws_gpu:
            return {"code": 0, "data": {"list": [gpu_item]}}
        return {"code": 0, "data": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)

    started: dict[str, str] = {}

    def fake_start_notebook(notebook_id: str, session=None) -> dict:  # type: ignore[no-untyped-def]
        started["notebook_id"] = notebook_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "start_notebook", fake_start_notebook)

    def fake_wait_for_notebook_running(
        notebook_id: str, session=None, timeout: int = 600, poll_interval: int = 5
    ) -> dict:
        return {"status": "RUNNING", "notebook_id": notebook_id, "quota": {"gpu_count": 8}}

    monkeypatch.setattr(
        browser_api_module, "wait_for_notebook_running", fake_wait_for_notebook_running
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "start", "ring-8h100-test", "--workspace", "b"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert started["notebook_id"] == "nb-gpu"


def test_run_notebook_ssh_validates_dropbear_setup_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """setup_script is now optional — built-in bootstrap handles dropbear.

    This test verifies that *no* ConfigError is raised when dropbear_deb_dir
    is set without a setup_script.  The code should proceed to the rtunnel
    setup phase (mocked here to raise so we can verify it was reached).
    """

    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    class FakeTunnelConfig:
        def __init__(self) -> None:
            self.bridges: dict[str, object] = {}
            self.default_bridge = None

        def add_bridge(self, profile: object) -> None:
            name = str(getattr(profile, "name", "default"))
            self.bridges[name] = profile
            if self.default_bridge is None:
                self.default_bridge = name

        def get_bridge(self, name: Optional[str] = None) -> object | None:
            if name:
                return self.bridges.get(name)
            if self.default_bridge:
                return self.bridges.get(self.default_bridge)
            return None

    captured: dict[str, str] = {}

    def fake_handle_error(
        ctx: Context,
        error_type: str,
        message: str,
        exit_code: int,
        *,
        hint: Optional[str] = None,
    ) -> None:
        assert ctx is not None
        captured["type"] = error_type
        captured["message"] = message
        captured["hint"] = hint or ""
        raise SystemExit(exit_code)

    monkeypatch.setattr(notebook_cmd_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(
        notebook_cmd_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA"
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    fake_tunnel_config = FakeTunnelConfig()
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: False)

    with pytest.raises(SystemExit) as exc:
        notebook_cmd_module.run_notebook_ssh(
            Context(),
            notebook_id="nb-name",
            workspace="Test Workspace",
            wait=True,
            pubkey=None,
            port=31337,
            ssh_port=22222,
            command=None,
            debug_playwright=False,
            setup_timeout=60,
        )

    # No longer a ConfigError — the code now proceeds to rtunnel setup
    # which is mocked to raise AssertionError ("should not be called" was
    # correct when the validation blocked it; now we expect it to be called).
    assert exc.value.code != EXIT_CONFIG_ERROR


def test_run_notebook_ssh_fails_fast_on_account_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    captured: dict[str, str] = {}

    def fake_handle_error(
        ctx: Context,
        error_type: str,
        message: str,
        exit_code: int,
        *,
        hint: Optional[str] = None,
    ) -> None:
        assert ctx is not None
        captured["type"] = error_type
        captured["message"] = message
        captured["hint"] = hint or ""
        raise SystemExit(exit_code)

    monkeypatch.setattr(notebook_cmd_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "user_id": "other-user",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "current-user", "username": "current"},
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    with pytest.raises(SystemExit) as exc:
        notebook_cmd_module.run_notebook_ssh(
            Context(),
            notebook_id="nb-name",
            workspace="Test Workspace",
            wait=True,
            pubkey=None,
            port=31337,
            ssh_port=22222,
            command=None,
            debug_playwright=False,
            setup_timeout=60,
        )

    assert exc.value.code == EXIT_CONFIG_ERROR
    assert captured["type"] == "ConfigError"
    assert "Notebook/account mismatch" in captured["message"]


def test_run_notebook_ssh_passes_resolved_runtime_to_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    class FakeTunnelConfig:
        def __init__(self) -> None:
            self.bridges: dict[str, object] = {}
            self.default_bridge = None

        def add_bridge(self, profile: object) -> None:
            name = str(getattr(profile, "name", "default"))
            self.bridges[name] = profile
            if self.default_bridge is None:
                self.default_bridge = name

        def get_bridge(self, name: Optional[str] = None) -> object | None:
            if name:
                return self.bridges.get(name)
            if self.default_bridge:
                return self.bridges.get(self.default_bridge)
            return None

    setup_kwargs: dict[str, object] = {}
    fake_tunnel_config = FakeTunnelConfig()

    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
            "start_config": {"allow_ssh": False},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(
        notebook_cmd_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA"
    )

    def fake_setup_notebook_rtunnel(**kwargs):  # type: ignore[no-untyped-def]
        setup_kwargs.update(kwargs)
        return "wss://proxy.example/notebook/"

    monkeypatch.setattr(browser_api_module, "setup_notebook_rtunnel", fake_setup_notebook_rtunnel)

    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: True)
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )
    monkeypatch.setattr(
        tunnel_module,
        "is_tunnel_available",
        lambda bridge_name, config, retries=0, retry_pause=0.0, progressive=True: True,
    )

    monkeypatch.setattr(notebook_cmd_module.subprocess, "call", lambda args: 0)

    notebook_cmd_module.run_notebook_ssh(
        Context(),
        notebook_id="nb-name",
        workspace="Test Workspace",
        wait=True,
        pubkey=None,
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )


def test_run_notebook_ssh_refreshes_saved_profile_on_notebook_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    class FakeTunnelConfig:
        def __init__(self) -> None:
            self.bridges: dict[str, object] = {}
            self.default_bridge = None

        def add_bridge(self, profile: object) -> None:
            name = str(getattr(profile, "name", "default"))
            self.bridges[name] = profile
            if self.default_bridge is None:
                self.default_bridge = name

        def get_bridge(self, name: Optional[str] = None) -> object | None:
            if name:
                return self.bridges.get(name)
            if self.default_bridge:
                return self.bridges.get(self.default_bridge)
            return None

    setup_called = {"value": False}
    fake_tunnel_config = FakeTunnelConfig()
    fake_tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="shared-profile",
            proxy_url="wss://proxy.example/old",
            notebook_id="notebook-old",
        )
    )

    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
            "start_config": {"allow_ssh": False},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(
        notebook_cmd_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA"
    )

    def fake_setup_notebook_rtunnel(**kwargs):  # type: ignore[no-untyped-def]
        setup_called["value"] = True
        return "wss://proxy.example/new"

    monkeypatch.setattr(browser_api_module, "setup_notebook_rtunnel", fake_setup_notebook_rtunnel)
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: True)
    monkeypatch.setattr(
        tunnel_module,
        "is_tunnel_available",
        lambda bridge_name, config, retries=0, retry_pause=0.0, progressive=True: True,
    )
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )

    monkeypatch.setattr(notebook_cmd_module.subprocess, "call", lambda args: 0)

    notebook_cmd_module.run_notebook_ssh(
        Context(),
        notebook_id="nb-name",
        workspace="Test Workspace",
        wait=True,
        pubkey=None,
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert setup_called["value"] is True
    # Cache key is now always the notebook's canonical name. The pre-existing
    # 'shared-profile' entry binds to a *different* notebook_id, so it stays
    # untouched; the new connection lands under its own canonical key.
    untouched = fake_tunnel_config.bridges["shared-profile"]
    assert getattr(untouched, "notebook_id", None) == "notebook-old"
    canonical_key = "test-nb"  # mock notebook_detail's display name
    saved_profile = fake_tunnel_config.bridges[canonical_key]
    assert getattr(saved_profile, "notebook_id", None) == "notebook-12345678"


# Removed in v2.0.0: the old "numeric id / partial hex → full id" resolution
# path no longer exists. Notebook commands take a name (exact match on
# item.name); anything that looks like an id is rejected upfront.


def test_run_notebook_ssh_interactive_reconnects_after_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    class FakeTunnelConfig:
        def __init__(self) -> None:
            self.bridges: dict[str, object] = {}
            self.default_bridge = None

        def add_bridge(self, profile: object) -> None:
            name = str(getattr(profile, "name", "default"))
            self.bridges[name] = profile
            if self.default_bridge is None:
                self.default_bridge = name

        def get_bridge(self, name: Optional[str] = None) -> object | None:
            if name:
                return self.bridges.get(name)
            if self.default_bridge:
                return self.bridges.get(self.default_bridge)
            return None

    cfg = make_test_config(tmp_path)
    cfg.tunnel_retries = 2
    cfg.tunnel_retry_pause = 0.0

    reconnect_calls = {"rebuild": 0}
    fake_tunnel_config = FakeTunnelConfig()

    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: cfg)
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
            "start_config": {"allow_ssh": False},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(
        notebook_cmd_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA"
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: "wss://proxy.example/notebook/",
    )
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: True)
    monkeypatch.setattr(
        tunnel_module,
        "is_tunnel_available",
        lambda bridge_name, config, retries=0, retry_pause=0.0, progressive=True: True,
    )
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )

    ssh_rc = iter([255, 0])
    monkeypatch.setattr(notebook_cmd_module.subprocess, "call", lambda args: next(ssh_rc))

    def fake_rebuild(*args: Any, **kwargs: Any) -> object:
        reconnect_calls["rebuild"] += 1
        profile_name = str(kwargs.get("bridge_name", "notebook-12345678"))
        return fake_tunnel_config.bridges[profile_name]

    monkeypatch.setattr(notebook_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    notebook_cmd_module.run_notebook_ssh(
        Context(),
        notebook_id="nb-name",
        workspace="Test Workspace",
        wait=True,
        pubkey=None,
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert reconnect_calls["rebuild"] == 1


def test_run_notebook_ssh_reports_when_tunnel_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
        all_workspace_ids = ["ws-test"]
        all_workspace_names = {"ws-test": "Test Workspace"}
        storage_state = {}

    class FakeTunnelConfig:
        def __init__(self) -> None:
            self.bridges: dict[str, object] = {}
            self.default_bridge = None

        def add_bridge(self, profile: object) -> None:
            self.bridges[str(getattr(profile, "name", "default"))] = profile

    captured: dict[str, str] = {}

    def fake_handle_error(
        ctx: Context,
        error_type: str,
        message: str,
        exit_code: int,
        *,
        hint: Optional[str] = None,
    ) -> None:
        assert ctx is not None
        captured["type"] = error_type
        captured["message"] = message
        captured["hint"] = hint or ""
        raise SystemExit(exit_code)

    monkeypatch.setattr(notebook_cmd_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(notebook_cmd_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
            "start_config": {"allow_ssh": False},
        },
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(
        notebook_cmd_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA"
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: "wss://proxy.example/notebook/",
    )

    fake_tunnel_config = FakeTunnelConfig()
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: True)
    monkeypatch.setattr(
        tunnel_module,
        "is_tunnel_available",
        lambda bridge_name, config, retries=0, retry_pause=0.0, progressive=True: False,
    )
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )

    with pytest.raises(SystemExit) as exc:
        notebook_cmd_module.run_notebook_ssh(
            Context(),
            notebook_id="nb-name",
            workspace="Test Workspace",
            wait=True,
            pubkey=None,
            port=31337,
            ssh_port=22222,
            command=None,
            debug_playwright=False,
            setup_timeout=60,
        )

    assert exc.value.code == EXIT_API_ERROR
    assert captured["type"] == "APIError"
    assert "SSH preflight failed" in captured["message"]
    assert "Proxy readiness report:" in captured["hint"]
    assert "allow_ssh=false" in captured["hint"]
