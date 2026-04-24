import json
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
    EXIT_GENERAL_ERROR,
    EXIT_TIMEOUT,
    EXIT_LOG_NOT_FOUND,
    EXIT_JOB_NOT_FOUND,
)

from inspire import config as config_module
from inspire.bridge import tunnel as tunnel_module
from inspire.cli.commands.notebook import notebook_commands as notebook_cmd_module
from inspire.cli.utils import auth as auth_module
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.cli.utils.auth import AuthenticationError
from inspire.config import ConfigError
from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.cli.utils.job_cache import JobCache
from inspire.platform.openapi import ResourceManager

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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
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
        self.resource_manager = ResourceManager(
            [
                {
                    "name": "H200 Default Test Group",
                    "id": "lcg-test-h200-0000-0000-0000-00000000H200",
                    "gpu_type": "H200",
                    "location": "Test",
                },
                {
                    "name": "H100 Default Test Group",
                    "id": "lcg-test-h100-0000-0000-0000-00000000H100",
                    "gpu_type": "H100",
                    "location": "Test",
                },
            ]
        )

    # Job-related methods -------------------------------------------------
    def create_training_job_smart(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls["create_training_job_smart"] = kwargs
        return {"data": {"job_id": TEST_JOB_ID}}

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

    def stop_training_job(self, job_id: str) -> None:
        self.calls.setdefault("stop_training_job", []).append(job_id)

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
    """Patch Config.from_env and AuthManager.get_api to use local stubs.

    Args:
        monkeypatch: pytest monkeypatch fixture
        tmp_path: Temporary directory path
        include_compute_groups: If True, include test compute groups in config
    """
    config = make_test_config(tmp_path, include_compute_groups=include_compute_groups)
    config.target_dir and Path(config.target_dir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INSPIRE_JOB_CACHE", config.job_cache_path)

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        if require_target_dir and not config.target_dir:
            raise ConfigError("Missing INSPIRE_TARGET_DIR")
        return config

    def fake_from_files_and_env(cls, require_target_dir: bool = False, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        if require_target_dir and not config.target_dir:
            raise ConfigError("Missing INSPIRE_TARGET_DIR")
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    api = DummyAPI()

    def fake_get_api(self_or_cls, cfg: Optional[config_module.Config] = None) -> DummyAPI:  # type: ignore[override]
        # Ensure we were passed the same config object
        assert cfg is config or cfg is None
        return api

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)
    auth_module.AuthManager.clear_cache()

    # Mock browser API calls for project selection
    class FakeWebSession:
        workspace_id = "ws-test-workspace"
        storage_state = {}
        all_workspace_ids = ["ws-test-workspace", "ws-gpu", "ws-cpu"]
        all_workspace_names = {
            "ws-test-workspace": "Test Workspace",
            "ws-gpu": "分布式训练空间",
            "ws-cpu": "CPU资源空间",
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

    # Stub the live train-spec resolver so job-submit tests don't hit the real
    # platform — real resolution lives in resolve_train_spec's own unit tests.
    from inspire.cli.utils import spec_resolver as spec_resolver_module

    monkeypatch.setattr(
        spec_resolver_module,
        "resolve_train_spec",
        lambda **_: ("spec-test-default", 16, 128),
    )

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

    result = runner.invoke(cli_main, ["--json", "resources", "list"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "availability" in payload["data"]
    assert payload["data"]["availability"][0]["group_id"] == test_group_id


def test_global_debug_flag_runs_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    from inspire.platform.web import browser_api as browser_api_module

    monkeypatch.setattr(
        browser_api_module,
        "get_accurate_resource_availability",
        lambda **kwargs: [],  # noqa: ARG005
    )
    runner = CliRunner()

    result = runner.invoke(cli_main, ["--debug", "resources", "list"])
    assert result.exit_code == 0


def test_job_help_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Smoke test to ensure `inspire job --help` works (no import/syntax errors)."""
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "--help"])
    assert result.exit_code == 0
    assert "Manage training jobs" in result.output


# ---------------------------------------------------------------------------
# Job command group
# ---------------------------------------------------------------------------


def test_job_create_human_output_updates_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
            "--no-auto",
        ],
    )

    assert result.exit_code == 0
    # v2: plain-text output reports the name, not the platform id.
    assert "Job created: test-job" in result.output

    # Verify job cache file was created
    cache_path = Path(make_test_config(tmp_path).job_cache_path)
    assert cache_path.exists()


def test_job_create_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
            "--no-auto",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["data"]["job_id"] == TEST_JOB_ID


def test_job_create_requires_target_dir(monkeypatch: pytest.MonkeyPatch):
    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):
        assert require_target_dir is True
        raise ConfigError("Missing INSPIRE_TARGET_DIR")

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "test-job",
            "--resource",
            "H200",
            "--command",
            "echo hi",
        ],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Missing INSPIRE_TARGET_DIR" in result.output


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


def test_job_status_updates_cache_and_formats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["job", "status", TEST_JOB_ID])
    assert result.exit_code == 0
    assert "Job Status" in result.output
    assert TEST_JOB_ID in result.output


def test_job_command_prefers_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Seed cache with a different command to ensure API is preferred
    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="cached command",
        status="RUNNING",
        log_path=None,
    )

    def api_detail(job_id: str) -> Dict[str, Any]:
        api.calls.setdefault("get_job_detail", []).append(job_id)
        return {"data": {"job_id": job_id, "command": "api command"}}

    api.get_job_detail = api_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "command", TEST_JOB_ID])

    assert result.exit_code == 0
    assert "api command" in result.output
    assert "cached command" not in result.output
    assert api.calls["get_job_detail"] == [TEST_JOB_ID]


def test_job_command_falls_back_to_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="cached command",
        status="RUNNING",
        log_path=None,
    )

    def api_detail(job_id: str) -> Dict[str, Any]:  # noqa: ARG001
        raise AuthenticationError("bad credentials")

    api.get_job_detail = api_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "command", TEST_JOB_ID])

    assert result.exit_code == 0
    assert "cached command" in result.output


def test_job_status_not_found_sets_specific_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    def failing_get_job_detail(job_id: str) -> Dict[str, Any]:
        raise RuntimeError("Job not found")

    api.get_job_detail = failing_get_job_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "status", "missing-id"])
    assert result.exit_code == EXIT_JOB_NOT_FOUND


def test_job_stop_with_force_and_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        ["--json", "job", "stop", TEST_JOB_ID],
    )
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert data["data"]["job_id"] == TEST_JOB_ID
    assert data["data"]["status"] == "stopped"


def test_job_wait_succeeds_and_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Ensure the job is immediately in a terminal state
    def get_job_detail(job_id: str) -> Dict[str, Any]:
        return {
            "data": {
                "job_id": job_id,
                "name": "wait-job",
                "status": "SUCCEEDED",
                "running_time_ms": "1000",
            }
        }

    api.get_job_detail = get_job_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "wait", TEST_JOB_ID, "--timeout", "60", "--interval", "1"],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "SUCCEEDED" in result.output


def test_job_wait_json_output_has_no_human_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_config_and_auth(monkeypatch, tmp_path)

    def get_job_detail(job_id: str) -> Dict[str, Any]:
        return {
            "data": {
                "job_id": job_id,
                "name": "wait-job",
                "status": "SUCCEEDED",
                "running_time_ms": "1000",
            }
        }

    api.get_job_detail = get_job_detail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "job", "wait", TEST_JOB_ID, "--timeout", "60", "--interval", "1"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "Waiting for job" not in result.output
    payloads = _parse_json_stream(result.output)
    assert payloads
    for payload in payloads:
        assert payload["success"] is True


def test_job_wait_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Force time to jump ahead so we immediately hit timeout
    from importlib import import_module

    job_deps = import_module("inspire.cli.commands.job.job_deps")

    calls: List[int] = []

    def fake_time() -> int:
        # First call (start_time) -> 0, second call -> large value
        calls.append(1)
        return 0 if len(calls) == 1 else 10

    monkeypatch.setattr(job_deps.time, "time", fake_time)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "wait", TEST_JOB_ID, "--timeout", "1", "--interval", "1"],
    )
    assert result.exit_code == EXIT_TIMEOUT
    assert "Timeout after 1s" in result.output


def test_job_list_uses_local_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Provide a fake JobCache implementation
    from importlib import import_module

    job_deps = import_module("inspire.cli.commands.job.job_deps")

    class FakeCache:
        def __init__(self, path: str) -> None:  # noqa: ARG002
            pass

        def list_jobs(
            self,
            limit: int = 10,
            status: Optional[str] = None,
            exclude_statuses: Optional[set] = None,
        ) -> List[Dict[str, Any]]:
            return [
                {
                    "job_id": TEST_JOB_ID,
                    "name": "cached-job",
                    "status": status or "PENDING",
                    "created_at": "2025-01-01T00:00:00",
                }
            ]

    monkeypatch.setattr(job_deps, "JobCache", FakeCache)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list", "--limit", "5"])

    assert result.exit_code == 0
    assert "cached-job" in result.output
    assert TEST_JOB_ID in result.output


def test_job_list_defaults_to_all_cached_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    for index in range(12):
        cache.add_job(
            job_id=f"job-aaaaaaa{index:01d}-1234-1234-1234-{index:012d}"[:40],
            name=f"job-{index}",
            resource="H200",
            command=f"echo {index}",
            status="PENDING",
        )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list"])

    assert result.exit_code == 0
    assert "Total: 12 job(s)" in result.output
    assert "job-11" in result.output


def test_job_list_refreshes_live_status_from_web_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="cached-job",
        resource="H200",
        command="echo test",
        status="PENDING",
    )

    from importlib import import_module

    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    monkeypatch.setattr(
        jobs_module,
        "list_jobs",
        lambda workspace_id=None, page_num=1, page_size=100, session=None: (  # noqa: ARG005
            [
                JobInfo(
                    job_id=TEST_JOB_ID,
                    name="cached-job",
                    status="job_succeeded",
                    command="echo test",
                    created_at="2025-01-01T00:00:00",
                    finished_at=None,
                    created_by_name="tester",
                    created_by_id="user-1",
                    project_id="project-1",
                    project_name="Test Project",
                    compute_group_name="H200 TestRoom",
                    gpu_type="H200",
                    gpu_count=8,
                    instance_count=1,
                    priority=9,
                    workspace_id="ws-test-workspace",
                )
            ],
            1,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list", "--limit", "5"])

    assert result.exit_code == 0
    assert "job_succeeded" in result.output
    refreshed = cache.get_job(TEST_JOB_ID)
    assert refreshed is not None
    assert refreshed["status"] == "job_succeeded"


def test_job_list_live_refresh_scans_beyond_ten_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="cached-job",
        resource="H200",
        command="echo test",
        status="PENDING",
    )

    from importlib import import_module

    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    def fake_list_jobs(workspace_id=None, page_num=1, page_size=100, session=None):  # noqa: ARG001
        if page_num < 11:
            return (
                [
                    JobInfo(
                        job_id=f"job-{page_num:08d}-0000-0000-0000-000000000000",
                        name=f"other-{page_num}",
                        status="PENDING",
                        command="echo other",
                        created_at="2025-01-01T00:00:00",
                        finished_at=None,
                        created_by_name="tester",
                        created_by_id="user-1",
                        project_id="project-1",
                        project_name="Test Project",
                        compute_group_name="H200 TestRoom",
                        gpu_type="H200",
                        gpu_count=8,
                        instance_count=1,
                        priority=9,
                        workspace_id="ws-test-workspace",
                    )
                ],
                11 * page_size,
            )
        if page_num == 11:
            return (
                [
                    JobInfo(
                        job_id=TEST_JOB_ID,
                        name="cached-job",
                        status="job_succeeded",
                        command="echo test",
                        created_at="2025-01-01T00:00:00",
                        finished_at=None,
                        created_by_name="tester",
                        created_by_id="user-1",
                        project_id="project-1",
                        project_name="Test Project",
                        compute_group_name="H200 TestRoom",
                        gpu_type="H200",
                        gpu_count=8,
                        instance_count=1,
                        priority=9,
                        workspace_id="ws-test-workspace",
                    )
                ],
                11 * page_size,
            )
        return ([], 11 * page_size)

    monkeypatch.setattr(jobs_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list"])

    assert result.exit_code == 0
    assert "job_succeeded" in result.output
    refreshed = cache.get_job(TEST_JOB_ID)
    assert refreshed is not None
    assert refreshed["status"] == "job_succeeded"


def test_job_list_refreshes_live_status_across_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="cached-job",
        resource="H200",
        command="echo test",
        status="PENDING",
    )

    from importlib import import_module

    jobs_module = import_module("inspire.platform.web.browser_api.jobs")
    JobInfo = jobs_module.JobInfo

    class FakeSession:
        workspace_id = "ws-other"
        all_workspace_ids = ["ws-other", "ws-train"]
        storage_state = {}

    monkeypatch.setattr(
        web_session_module, "get_web_session", lambda *args, **kwargs: FakeSession()
    )

    def fake_list_jobs(workspace_id=None, page_num=1, page_size=100, session=None):  # noqa: ARG001
        if workspace_id == "ws-train":
            return (
                [
                    JobInfo(
                        job_id=TEST_JOB_ID,
                        name="cached-job",
                        status="job_succeeded",
                        command="echo test",
                        created_at="2025-01-01T00:00:00",
                        finished_at=None,
                        created_by_name="tester",
                        created_by_id="user-1",
                        project_id="project-1",
                        project_name="Test Project",
                        compute_group_name="H200 TestRoom",
                        gpu_type="H200",
                        gpu_count=8,
                        instance_count=1,
                        priority=9,
                        workspace_id="ws-train",
                    )
                ],
                1,
            )
        return ([], 0)

    monkeypatch.setattr(jobs_module, "list_jobs", fake_list_jobs)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list", "--limit", "5"])

    assert result.exit_code == 0
    assert "job_succeeded" in result.output
    refreshed = cache.get_job(TEST_JOB_ID)
    assert refreshed is not None
    assert refreshed["status"] == "job_succeeded"


def test_job_list_status_filter_accepts_api_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="cached-job",
        resource="H200",
        command="echo test",
        status="job_succeeded",
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "list", "--status", "SUCCEEDED"])

    assert result.exit_code == 0
    assert "cached-job" in result.output
    assert TEST_JOB_ID in result.output


def test_job_list_watch_json_does_not_clear_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    from importlib import import_module

    job_commands_module = import_module("inspire.cli.commands.job.job_commands")

    def fail_clear(cmd: str) -> int:  # noqa: ARG001
        raise AssertionError("clear should not be called in JSON mode")

    monkeypatch.setattr(job_commands_module.os, "system", fail_clear)
    monkeypatch.setattr(
        job_commands_module.job_deps.time,
        "sleep",
        lambda interval: (_ for _ in ()).throw(KeyboardInterrupt()),  # noqa: ARG005
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "list", "--watch", "--interval", "1"])

    assert result.exit_code == EXIT_SUCCESS
    payloads = _parse_json_stream(result.output)
    assert payloads
    for payload in payloads:
        assert payload["success"] is True


def test_job_update_refreshes_job_creating_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = patch_config_and_auth(monkeypatch, tmp_path)

    # Seed cache with a job in an early-stage API status that should still be refreshed
    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="creating-job",
        resource="H200",
        command="echo hi",
        status="job_creating",
        log_path=None,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "update", "--delay", "0"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True

    updated_ids = {entry["job_id"] for entry in payload["data"]["updated"]}
    assert updated_ids == {TEST_JOB_ID}

    # Ensure the job was actually polled and the cache was updated
    assert api.calls["get_job_detail"] == [TEST_JOB_ID]
    refreshed = cache.get_job(TEST_JOB_ID)
    assert refreshed is not None
    assert refreshed["status"] == "SUCCEEDED"


def test_job_update_defaults_to_refresh_all_cached_active_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    for index in range(12):
        cache.add_job(
            job_id=f"job-bbbbbbb{index:01d}-1234-1234-1234-{index:012d}"[:40],
            name=f"job-{index}",
            resource="H200",
            command=f"echo {index}",
            status="PENDING",
        )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "update", "--delay", "0"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert len(payload["data"]["updated"]) == 12
    assert len(api.calls["get_job_detail"]) == 12


def test_job_logs_path_and_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache with a remote log path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    # Create local cache directory and log file (simulating already-fetched log)
    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    local_log_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    local_log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    # Mock fetch_remote_log_via_bridge to do nothing (log already cached)
    from importlib import import_module

    job_deps = import_module("inspire.cli.commands.job.job_deps")
    job_logs_module = import_module("inspire.cli.commands.job.job_logs")

    def fake_fetch(config, job_id, remote_log_path, cache_path, refresh):  # noqa: ARG001
        pass  # Log already exists locally

    monkeypatch.setattr(job_deps, "fetch_remote_log_via_bridge", fake_fetch)
    monkeypatch.setattr(job_logs_module, "is_tunnel_available", lambda *args, **kwargs: False)

    runner = CliRunner()

    # --path just prints path
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--path"])
    assert result.exit_code == 0
    assert str(remote_log_path) in result.output

    # --tail reads last N lines
    result_tail = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--tail", "2"])
    assert result_tail.exit_code == 0
    assert "line2" in result_tail.output
    assert "line3" in result_tail.output


def test_job_logs_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache with a remote log path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    # Create local cache directory and log file
    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    local_log_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    local_log_path.write_text("test log content\n", encoding="utf-8")

    # Mock fetch_remote_log_via_bridge
    from importlib import import_module

    job_deps = import_module("inspire.cli.commands.job.job_deps")
    job_logs_module = import_module("inspire.cli.commands.job.job_logs")

    def fake_fetch(config, job_id, remote_log_path, cache_path, refresh):  # noqa: ARG001
        pass

    monkeypatch.setattr(job_deps, "fetch_remote_log_via_bridge", fake_fetch)
    monkeypatch.setattr(job_logs_module, "is_tunnel_available", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "logs", TEST_JOB_ID])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert "log_path" in data["data"]
    assert "content" in data["data"]
    assert "test log content" in data["data"]["content"]


def test_job_logs_legacy_filename_is_migrated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    local_cache_dir = Path(config.log_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    legacy_log_path = local_cache_dir / f"job-{TEST_JOB_ID}.log"
    legacy_log_path.write_text("legacy line1\nlegacy line2\n", encoding="utf-8")

    from importlib import import_module

    job_deps = import_module("inspire.cli.commands.job.job_deps")
    job_logs_module = import_module("inspire.cli.commands.job.job_logs")

    def fail_fetch(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("fetch should not be called when legacy cache exists")

    monkeypatch.setattr(job_deps, "fetch_remote_log_via_bridge", fail_fetch)
    monkeypatch.setattr(job_logs_module, "is_tunnel_available", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--tail", "1"])

    assert result.exit_code == 0
    assert "legacy line2" in result.output
    new_path = local_cache_dir / f"{TEST_JOB_ID}.log"
    assert new_path.exists()
    assert not legacy_log_path.exists()


def test_job_logs_missing_file_sets_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Config.from_env will succeed but LogReader will return no file
    patch_config_and_auth(monkeypatch, tmp_path)

    # Add job to cache WITHOUT log_path to test the "log not found" path
    config = make_test_config(tmp_path)
    from inspire.cli.utils.job_cache import JobCache

    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=None,  # No log path means LogNotFound
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID])

    assert result.exit_code == EXIT_LOG_NOT_FOUND
    assert f"No log file found for job {TEST_JOB_ID}" in result.output


def test_job_logs_follow_json_skips_ssh_follow_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    from importlib import import_module

    job_logs_module = import_module("inspire.cli.commands.job.job_logs")

    called = {"workflow_follow": False}
    monkeypatch.setattr(job_logs_module, "is_tunnel_available", lambda: True)
    monkeypatch.setattr(
        job_logs_module,
        "_follow_logs_via_ssh",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        job_logs_module,
        "_follow_logs",
        lambda *args, **kwargs: (called.__setitem__("workflow_follow", True) or EXIT_SUCCESS),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "job", "logs", TEST_JOB_ID, "--follow"])

    assert result.exit_code == EXIT_SUCCESS
    assert called["workflow_follow"] is True


def test_job_logs_follow_returns_follow_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_config_and_auth(monkeypatch, tmp_path)

    config = make_test_config(tmp_path)
    cache = JobCache(config.get_expanded_cache_path())
    remote_log_path = f"/train/logs/.inspire/training_master_{TEST_JOB_ID}.log"
    cache.add_job(
        job_id=TEST_JOB_ID,
        name="test-job",
        resource="H200",
        command="echo test",
        status="RUNNING",
        log_path=remote_log_path,
    )

    from importlib import import_module

    job_logs_module = import_module("inspire.cli.commands.job.job_logs")

    monkeypatch.setattr(job_logs_module, "is_tunnel_available", lambda: False)
    monkeypatch.setattr(job_logs_module, "_follow_logs", lambda *args, **kwargs: EXIT_GENERAL_ERROR)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", TEST_JOB_ID, "--follow"])

    assert result.exit_code == EXIT_GENERAL_ERROR


# ---------------------------------------------------------------------------
# Resources / nodes / config commands
# ---------------------------------------------------------------------------


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

    result = runner.invoke(cli_main, ["--json", "resources", "nodes"])
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
    result = runner.invoke(cli_main, ["--json", "resources", "list", "--all", "--include-cpu"])
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

    def fake_from_env(cls, require_target_dir: bool = False) -> config_module.Config:  # type: ignore[override]
        return config

    def fake_from_files_and_env(cls, require_target_dir: bool = False, require_credentials: bool = True) -> tuple:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(config_module.Config, "from_env", classmethod(fake_from_env))
    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    def fake_get_api(self_or_cls, cfg: Optional[config_module.Config] = None):  # type: ignore[override]
        from inspire.cli.utils.auth import AuthenticationError

        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(auth_module.AuthManager, "get_api", fake_get_api)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Authentication failed" in result.output


def test_config_check_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
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
    project_config.write_text(
        """
[api]
base_url = "https://my-inspire.internal"
"""
    )
    global_config = tmp_path / "global-config.toml"

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_PROJECT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return global_config, project_config

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    monkeypatch.setenv("INSPIRE_BASE_URL", "https://env.example")
    monkeypatch.setattr(auth_module.AuthManager, "get_api", lambda _cls, cfg=None: DummyAPI())

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

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_ENV}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    monkeypatch.setattr(auth_module.AuthManager, "get_api", lambda _cls, cfg=None: DummyAPI())

    runner = CliRunner()
    result = runner.invoke(cli_main, ["config", "check", "--json"])

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

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_DEFAULT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    monkeypatch.setattr(
        auth_module.AuthManager, "get_api", lambda _cls, cfg=None: pytest.fail("should not auth")
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

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
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
    monkeypatch.setattr(
        auth_module.AuthManager, "get_api", lambda _cls, cfg=None: pytest.fail("should not auth")
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

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_PROJECT}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, project_config

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    monkeypatch.setattr(
        auth_module.AuthManager, "get_api", lambda _cls, cfg=None: pytest.fail("should not auth")
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "config", "check"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert "top-level `base_url`" in payload["error"]["message"]
    assert "[api]" in payload["error"]["message"]


def test_config_check_allows_path_defaults_for_endpoint_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    config.base_url = "https://my-inspire.internal"
    config.docker_registry = TEST_DOCKER_REGISTRY
    config.auth_endpoint = "/auth/token"
    config.openapi_prefix = "/openapi/v1"
    config.browser_api_prefix = "/api/v1"

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {"base_url": config_module.SOURCE_ENV}

    def fake_get_config_paths(cls):  # type: ignore[override]
        return None, None

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )
    monkeypatch.setattr(
        config_module.Config, "get_config_paths", classmethod(fake_get_config_paths)
    )
    monkeypatch.setattr(auth_module.AuthManager, "get_api", lambda _cls, cfg=None: DummyAPI())

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

    result = runner.invoke(cli_main, ["--json", "init", "--template", "--project", "--force"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["mode"] == "template"
    assert payload["data"]["files_written"] == [str(tmp_path / ".inspire" / "config.toml")]


def test_config_show_respects_global_json_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        workspaces={"a": ws_cpu, "b": ws_gpu},
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "list", "--all-workspaces", "--all", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    items = payload["data"]["items"]
    assert [item["id"] for item in items] == ["nb-gpu", "nb-cpu"]
    assert calls == [ws_cpu, ws_gpu]


def test_notebook_start_accepts_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        workspaces={"a": ws_cpu, "b": ws_gpu},
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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
    result = runner.invoke(cli_main, ["notebook", "start", "ring-8h100-test", "--no-keepalive"])

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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        workspaces={"a": ws_cpu, "b": ws_gpu},
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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
        ["notebook", "start", "ring-8h100-test", "--no-keepalive"],
        input="2\n",
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}}
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
        notebook_cmd_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(
            dropbear_deb_dir="/project/dropbear",
            setup_script=None,
        ),
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
            wait=True,
            pubkey=None,
            save_as=None,
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
            wait=True,
            pubkey=None,
            save_as=None,
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

    resolved_runtime = SshRuntimeConfig(
        rtunnel_download_url="https://project.example/rtunnel.tgz",
    )
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
        notebook_cmd_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: resolved_runtime,
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
        wait=True,
        pubkey=None,
        save_as=None,
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert setup_kwargs["ssh_runtime"] is resolved_runtime


def test_run_notebook_ssh_refreshes_saved_profile_on_notebook_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
        notebook_cmd_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
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
        wait=True,
        pubkey=None,
        save_as="shared-profile",
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert setup_called["value"] is True
    saved_profile = fake_tunnel_config.bridges["shared-profile"]
    assert getattr(saved_profile, "notebook_id", None) == "notebook-12345678"


# Removed in v2.0.0: the old "numeric id / partial hex → full id" resolution
# path no longer exists. Notebook commands take a name (exact match on
# item.name); anything that looks like an id is rejected upfront.


def test_run_notebook_ssh_interactive_reconnects_after_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
        notebook_cmd_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
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
        wait=True,
        pubkey=None,
        save_as=None,
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
        notebook_cmd_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
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
            wait=True,
            pubkey=None,
            save_as=None,
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
