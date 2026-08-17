from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.ray import ray_commands
from inspire.cli.main import main as cli_main


class _FakeSession:
    workspace_id = "ws-session"
    all_workspace_names = {"ws-ray": "Ray资源空间"}
    all_workspace_ids = ["ws-ray"]


def _patch_ray_create_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )

    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        ray_commands,
        "_assemble_create_body",
        lambda *args, **kwargs: {
            "name": "ray-demo",
            "workspace_id": "ws-internal",
            "project_id": "project-internal",
            "entrypoint": "python driver.py",
            "head_node": {"quota_id": "quota-internal"},
            "worker_groups": [{"group_name": "workers"}],
        },
    )

    calls: dict[str, Any] = {}

    def fake_create_ray_job(body: dict[str, Any], *, session: object) -> dict[str, Any]:
        del session
        calls["body"] = body
        return {
            "ray_job_id": "ray-job-internal",
            "name": "backend-name",
            "status": "QUEUING",
            "message": "backend message",
            "request": {"trace_id": "trace-internal"},
        }

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "create_ray_job",
        fake_create_ray_job,
    )
    monkeypatch.setattr(
        ray_commands,
        "remember_resource_identity",
        lambda **kwargs: None,
    )
    return calls


def _patch_ray_status_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
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
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        ray_commands,
        "_resolve_ray_name_in_workspace",
        lambda *_args, **_kwargs: "ray-job-internal",
    )

    detail: dict[str, Any] = {
        "ray_job_id": "ray-job-internal",
        "name": "ray-demo",
        "status": "RUNNING",
        "sub_status": "READY",
        "priority": 5,
        "priority_level": "NORMAL",
        "project": {"id": "project-internal", "name": "Project"},
        "workspace_id": "ws-internal",
        "head_node": {
            "logic_compute_group_id": "lcg-head-internal",
            "logic_compute_group_name": "CPU Group",
            "resource_spec_price": {
                "cpu_count": 4,
                "memory_size_gib": 16,
                "gpu_count": 0,
                "quota_id": "quota-head-internal",
            },
        },
        "worker_groups": [
            {
                "group_name": "workers",
                "min_replicas": 1,
                "max_replicas": 3,
                "logic_compute_group_id": "lcg-worker-internal",
                "logic_compute_group_name": "GPU Group",
                "resource_spec_price": {
                    "cpu_count": 8,
                    "memory_size_gib": 32,
                    "gpu_count": 1,
                    "gpu_info": {
                        "gpu_type_display": "NVIDIA H100",
                        "gpu_type": "NVIDIA_H100_INTERNAL",
                    },
                    "quota_id": "quota-worker-internal",
                },
            }
        ],
        "created_at": "1770000000",
        "updated_at": "1770000100",
        "finished_at": None,
        "request": {"request_id": "trace-internal"},
        "internal_path": "/internal/ray-job-internal",
    }
    calls: dict[str, str] = {}

    def fake_get_detail(ray_job_id: str, *, session: object) -> dict[str, Any]:
        del session
        calls["detail"] = ray_job_id
        return detail

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_ray_job_detail",
        fake_get_detail,
    )
    return calls


@pytest.mark.parametrize(
    ("command", "metavar"),
    [
        ("list", "NAME|all"),
        ("create", "NAME"),
        ("quota", "NAME"),
        ("status", "NAME"),
        ("instances", "NAME"),
        ("stop", "NAME"),
        ("delete", "NAME"),
        ("events", "NAME"),
        ("metrics", "NAME"),
    ],
)
def test_ray_workspace_help_uses_name_metavars(
    command: str,
    metavar: str,
) -> None:
    result = CliRunner().invoke(cli_main, ["ray", command, "--help"])

    assert result.exit_code == 0
    assert f"--workspace {metavar}" in result.output
    assert "--workspace TEXT" not in result.output


def test_ray_create_json_uses_stable_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_ray_create_runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "create",
            "--name",
            "ray-demo",
            "--command",
            "python driver.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {
            "name": "ray-demo",
            "status": "created",
        },
    }
    assert calls["body"]["name"] == "ray-demo"
    assert "ray-job-internal" not in result.output
    assert "backend-name" not in result.output
    assert "QUEUING" not in result.output
    assert "backend message" not in result.output
    assert "trace-internal" not in result.output


def test_ray_create_human_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ray_create_runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "create",
            "--name",
            "ray-demo",
            "--command",
            "python driver.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "OK Ray created: ray-demo\n"


def test_ray_list_human_renderer_is_name_first_and_footer_free() -> None:
    output = ray_commands._format_ray_list_rows(
        [
            {
                "name": "ray-demo",
                "status": "RUNNING",
                "created_at": "2026-08-05 12:00:00",
                "created_by_name": "Alice",
            }
        ]
    )

    assert output.splitlines()[0].startswith("Name")
    assert "Ray Jobs" not in output
    assert "Total:" not in output


def test_ray_list_filters_status_and_readable_keyword_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )
    session = _FakeSession()
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-1"},
    )

    def fake_list_ray_jobs(**kwargs):  # noqa: ANN001
        jobs = [
            {
                "ray_job_id": "ray-hidden-1",
                "name": "unrelated",
                "status": "RUNNING",
                "workspace_id": kwargs["workspace_id"],
                "project_id": "project-1",
                "project_name": "Inference",
                "created_at": "1770000002",
                "created_by_id": "user-1",
                "created_by_name": "Alice",
            },
            {
                "ray_job_id": "ray-hidden-2",
                "name": "other",
                "status": "FAILED",
                "workspace_id": kwargs["workspace_id"],
                "project_id": "project-1",
                "project_name": "Inference",
                "created_at": "1770000001",
                "created_by_id": "user-1",
                "created_by_name": "Alice",
            },
        ]
        return (
            [
                ray_commands.browser_api_module.RayJobInfo.from_api_response(item)
                for item in jobs[: kwargs["page_size"]]
            ],
            len(jobs),
        )

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_jobs",
        fake_list_ray_jobs,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "list",
            "--workspace",
            "Ray资源空间",
            "--status",
            "running",
            "--keyword",
            "inference",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert [item["name"] for item in payload["items"]] == ["unrelated"]
    assert "ray-hidden-1" not in result.output
    assert "ray-hidden-2" not in result.output


def test_ray_stop_json_uses_stable_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_ray_status_runtime(monkeypatch)

    def fake_stop_ray_job(ray_job_id: str, *, session: object) -> None:
        del session
        calls["stop"] = ray_job_id

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "stop_ray_job",
        fake_stop_ray_job,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "stop", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {
            "name": "ray-demo",
            "status": "stopped",
        },
    }
    assert "stopped" not in json.loads(result.output)["data"]
    assert calls["stop"] == "ray-job-internal"


def test_ray_stop_human_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_ray_status_runtime(monkeypatch)

    def fake_stop_ray_job(ray_job_id: str, *, session: object) -> None:
        del session
        calls["stop"] = ray_job_id

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "stop_ray_job",
        fake_stop_ray_job,
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "stop", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "OK Ray stopped: ray-demo\n"
    assert calls["stop"] == "ray-job-internal"


def test_ray_status_human_output_is_compact_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_ray_status_runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["ray", "status", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert "Ray Job Status" not in result.output
    assert "Name: ray-demo" in result.output
    assert "Status: RUNNING" in result.output
    assert "Project: Project" in result.output
    assert "Resource: head: 4 CPU, 16 GiB, 0 GPU, CPU Group" in result.output
    assert "workers: workers (1-3 replicas): 8 CPU, 32 GiB, 1 GPU, NVIDIA H100, GPU Group" in result.output
    assert "Priority: 5" in result.output
    assert "Priority Level: NORMAL" in result.output
    assert "Sub-status: READY" in result.output
    assert "Created: 1770000000" in result.output
    assert "Updated: 1770000100" in result.output
    assert "ray-job-internal" not in result.output
    assert "project-internal" not in result.output
    assert "lcg-worker-internal" not in result.output
    assert "trace-internal" not in result.output
    assert "/internal/ray-job-internal" not in result.output
    assert calls["detail"] == "ray-job-internal"


def test_ray_status_json_uses_stable_public_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ray_status_runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "status", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"] == {
        "name": "ray-demo",
        "status": "RUNNING",
        "project": "Project",
        "resource": {
            "head": {
                "cpu": 4,
                "memory_gib": 16,
                "gpu": 0,
                "compute_group": "CPU Group",
            },
            "workers": [
                {
                    "name": "workers",
                    "min": 1,
                    "max": 3,
                    "cpu": 8,
                    "memory_gib": 32,
                    "gpu": 1,
                    "gpu_type": "NVIDIA H100",
                    "compute_group": "GPU Group",
                }
            ],
        },
        "priority": 5,
        "priority_level": "NORMAL",
        "sub_status": "READY",
        "created_at": "1770000000",
        "updated_at": "1770000100",
    }
    assert "ray_job_id" not in result.output
    assert "workspace_id" not in result.output
    assert "quota-worker-internal" not in result.output
    assert "trace-internal" not in result.output


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _patch_ray_start(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: list[str],
) -> dict[str, Any]:
    """Patch start plus the status reads the command confirms with."""
    calls = _patch_ray_status_runtime(monkeypatch)
    remaining = list(statuses)

    def fake_start_ray_job(ray_job_id: str, *, session: object) -> dict[str, Any]:
        del session
        calls["start"] = ray_job_id
        return {"ray_job": {"ray_job_id": ray_job_id}}

    def fake_detail(ray_job_id: str, *, session: object) -> dict[str, Any]:
        del session
        calls.setdefault("detail_reads", 0)
        calls["detail_reads"] += 1
        return {"status": remaining.pop(0) if remaining else "STOPPED"}

    monkeypatch.setattr(
        ray_commands.browser_api_module, "start_ray_job", fake_start_ray_job
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module, "get_ray_job_detail", fake_detail
    )
    # The command sleeps between confirmation reads; tests must not.
    monkeypatch.setattr(ray_commands, "_RAY_START_CONFIRM_INTERVAL_SECONDS", 0)
    return calls


def test_ray_start_json_uses_stable_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `ray stop` used to be a one-way door: `ray.StartJob` exists, so a stopped
    # cluster can come back without re-specifying anything.
    calls = _patch_ray_start(monkeypatch, statuses=["PENDING"])

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "start", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {"name": "ray-demo", "status": "started", "job_status": "PENDING"},
    }
    assert calls["start"] == "ray-job-internal"
    # The refreshed ray_job the Action returns carries platform handles; none
    # of it reaches the output.
    assert "ray-job-internal" not in result.output


def test_ray_start_human_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ray_start(monkeypatch, statuses=["RUNNING"])

    result = CliRunner().invoke(
        cli_main,
        ["ray", "start", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "OK Ray started: ray-demo\n"


def test_ray_start_fails_when_the_job_never_leaves_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `StartJob` answers a success envelope; success is judged by the status
    # actually moving. Reporting OK off the envelope alone would be the same
    # class of lie as the old `API error: None`.
    calls = _patch_ray_start(monkeypatch, statuses=[])

    result = CliRunner().invoke(
        cli_main,
        ["ray", "start", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code != 0
    assert "still stopped" in result.output
    # The hint points at the cluster's own events, the only place that says why
    # a restart did not take. It must not claim the job is unrestartable: a
    # controlled live run restarted a job that had never reached RUNNING.
    assert "inspire ray events ray-demo" in result.output
    assert "cannot be restarted" not in result.output
    # Every attempt was spent before giving up.
    assert calls["detail_reads"] == ray_commands._RAY_START_CONFIRM_ATTEMPTS


def test_ray_start_stops_polling_as_soon_as_the_job_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_ray_start(monkeypatch, statuses=["STOPPED", "PENDING"])

    result = CliRunner().invoke(
        cli_main,
        ["ray", "start", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert calls["detail_reads"] == 2


def test_ray_start_treats_an_unreadable_status_as_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ray_start(monkeypatch, statuses=[""])

    result = CliRunner().invoke(
        cli_main,
        ["ray", "start", "ray-demo", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code != 0


def test_ray_start_rejects_a_platform_handle_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ray_status_runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "start",
            "ray-12345678-1234-1234-1234-123456789abc",
            "--workspace",
            "Ray资源空间",
        ],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# create: read-only public path
# ---------------------------------------------------------------------------


class _FakeResolvedQuota:
    quota_id = "quota-internal"
    logic_compute_group_id = "lcg-internal"
    compute_group_name = "CPU Room"
    gpu_count = 0
    cpu_count = 4
    memory_gib = 16
    gpu_type = ""
    raw_price: dict[str, Any] = {}


def _ray_create_body(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    from inspire.cli.utils import quota_resolver as quota_module

    monkeypatch.setattr(
        ray_commands, "select_workspace_id", lambda **_kwargs: "ws-internal"
    )
    monkeypatch.setattr(
        ray_commands, "_resolve_project_id", lambda *args, **kw: "project-internal"
    )
    monkeypatch.setattr(
        ray_commands, "_resolve_image_id", lambda raw, **kw: "image-internal"
    )
    monkeypatch.setattr(
        ray_commands, "resolve_workspace_task_priority", lambda *args, **kw: 4
    )
    monkeypatch.setattr(
        quota_module, "resolve_quota", lambda **_kwargs: _FakeResolvedQuota()
    )

    return ray_commands._assemble_create_body(
        None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        session=_FakeSession(),
        name="ray-demo",
        command="python driver.py",
        description="",
        project="Project",
        workspace="Ray资源空间",
        priority=None,
        image="ray-base:v1",
        image_type="SOURCE_PUBLIC",
        group="CPU Room",
        quota="0,4,16",
        shm_size=None,
        workers=("name=w;image=ray-base:v1;group=CPU Room;quota=0,4,16;min=1;max=2",),
        **kwargs,
    )


def test_ray_create_body_unchanged_when_read_only_guard_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Read-only is the safer value, but turning it on by default would change
    # every existing create; the platform keeps deciding.
    body = _ray_create_body(monkeypatch)

    assert "is_publicpath_readonly" not in body


@pytest.mark.parametrize("requested", [True, False])
def test_ray_create_body_carries_an_explicit_read_only_guard(
    monkeypatch: pytest.MonkeyPatch, requested: bool
) -> None:
    body = _ray_create_body(monkeypatch, public_path_readonly=requested)

    # `False` is a value the caller chose; only `None` means "do not send".
    assert body["is_publicpath_readonly"] is requested
