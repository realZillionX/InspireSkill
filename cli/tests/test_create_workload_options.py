"""Create-option coverage for `notebook create`, `job create` and `hpc create`.

Two things are locked down here. First, every option added on top of the
original create surface must stay out of the request unless it is asked for:
the `*_unchanged_by_default` tests compare the whole body against what the
command sent before those options existed, so a new field can never quietly
change an existing create. Second, when an option *is* used, the payload has
to match the shapes the platform accepts — `envs` entries are `{name, value}`,
the reserve/max-time fields are string milliseconds, and the HPC runtime cap
lives inside `sbatch_script`, not at the top level.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.hpc import hpc_commands
from inspire.cli.main import main as cli_main
from inspire.cli.utils import dataset_mounts, job_submit
from inspire.cli.utils.quota_resolver import ResolvedQuota
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import datasets as datasets_module
from inspire.platform.web.browser_api.datasets import DatasetMount, DatasetValidation


# ---------------------------------------------------------------------------
# spec parsing
# ---------------------------------------------------------------------------


def test_dataset_spec_parses_name_and_version() -> None:
    assert dataset_mounts.parse_dataset_specs(["pixabay-81k:v0", "videoufo:v1"]) == [
        DatasetMount("pixabay-81k", "v0"),
        DatasetMount("videoufo", "v1"),
    ]


@pytest.mark.parametrize("value", ["pixabay-81k", "", ":v0", "pixabay-81k:"])
def test_dataset_spec_requires_both_halves(value: str) -> None:
    with pytest.raises(dataset_mounts.DatasetSpecError):
        dataset_mounts.parse_dataset_specs([value])


def test_dataset_spec_rejects_a_repeated_mount() -> None:
    with pytest.raises(dataset_mounts.DatasetSpecError, match="more than once"):
        dataset_mounts.parse_dataset_specs(["pixabay-81k:v0", "pixabay-81k:v0"])


def test_dataset_spec_error_becomes_a_usage_error() -> None:
    # A malformed spec is a command-line typo, so it is reported before any
    # workspace, project or quota resolution happens.
    with pytest.raises(click.UsageError):
        dataset_mounts.parse_dataset_specs_or_usage_error(["pixabay-81k"])


def test_dataset_mount_views_expose_only_names_and_container_path() -> None:
    assert dataset_mounts.dataset_mount_views([DatasetMount("pixabay-81k", "v0")]) == [
        {
            "name": "pixabay-81k",
            "version": "v0",
            "path": "/inspire/dataset/pixabay-81k/v0",
        }
    ]


def test_resolve_dataset_info_fills_the_platform_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # `path` is the storage path the platform resolves, not the container path;
    # the create Actions want it pre-filled, exactly as 校验数据 leaves it.
    monkeypatch.setattr(
        dataset_mounts,
        "validate_dataset_mounts",
        lambda mounts, *, workspace_id, session=None: [
            DatasetValidation(
                dataset="pixabay-81k",
                version="v0",
                ok=True,
                path="sftpgo/pixabay-81k/v0",
            )
        ],
    )

    assert dataset_mounts.resolve_dataset_info(
        [DatasetMount("pixabay-81k", "v0")],
        workspace_id="ws-1",
    ) == [{"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}]


def test_resolve_dataset_info_reports_the_platform_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dataset_mounts,
        "validate_dataset_mounts",
        lambda mounts, *, workspace_id, session=None: [
            DatasetValidation(
                dataset="pexels-245k",
                version="v1",
                ok=False,
                error="无访问权限",
            )
        ],
    )

    with pytest.raises(dataset_mounts.DatasetSpecError, match="无访问权限"):
        dataset_mounts.resolve_dataset_info(
            [DatasetMount("pexels-245k", "v1")],
            workspace_id="ws-1",
        )


def test_env_assignments_use_name_and_value() -> None:
    # `{"key": ...}` is rejected by the create Action with unknown field "key".
    assert job_submit.parse_env_assignments(["A=1", "B=", "C=x=y"]) == [
        {"name": "A", "value": "1"},
        {"name": "B", "value": ""},
        {"name": "C", "value": "x=y"},
    ]


@pytest.mark.parametrize("value", ["A", "=1", ""])
def test_env_assignment_requires_key_and_equals(value: str) -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        job_submit.parse_env_assignments([value])


def test_env_assignment_rejects_a_repeated_key() -> None:
    with pytest.raises(ValueError, match="more than once"):
        job_submit.parse_env_assignments(["A=1", "A=2"])


def test_hours_become_string_milliseconds() -> None:
    # The three time fields are declared as strings; a number is rejected.
    assert job_submit.hours_to_ms_string(1.5) == "5400000"
    assert job_submit.hours_to_ms_string(None) is None


# ---------------------------------------------------------------------------
# notebook payload
# ---------------------------------------------------------------------------


def _capture_notebook_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    import inspire.platform.web.browser_api.notebooks as notebooks_module

    monkeypatch.setattr(
        notebooks_module,
        "_get_session_and_workspace_id",
        lambda *, workspace_id, session: (session, workspace_id),
    )

    def fake_v2(session: Any, action: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["action"] = action
        captured["body"] = body
        return {"notebook_id": "nb-1"}

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_v2)
    return captured


def _create_notebook(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> dict[str, Any]:
    captured = _capture_notebook_body(monkeypatch)
    browser_api_module.create_notebook(
        name="demo",
        project_id="project-1",
        project_name="Project",
        image_id="image-1",
        image_url="registry/image:tag",
        logic_compute_group_id="lcg-1",
        quota_id="quota-1",
        gpu_count=0,
        cpu_count=4,
        memory_size=32,
        shared_memory_size=8,
        auto_stop=False,
        workspace_id="ws-1",
        session=object(),
        **extra,
    )
    body = captured["body"]
    assert isinstance(body, dict)
    return body


def test_notebook_create_body_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _create_notebook(monkeypatch) == {
        "workspace_id": "ws-1",
        "name": "demo",
        "project_id": "project-1",
        "project_name": "Project",
        "auto_stop": False,
        "allow_ssh": True,
        "mirror_id": "image-1",
        "mirror_url": "registry/image:tag",
        "logic_compute_group_id": "lcg-1",
        "quota_id": "quota-1",
        "cpu_count": 4,
        "gpu_count": 0,
        "memory_size": 32,
        "shared_memory_size": 8,
    }


def test_notebook_create_body_carries_the_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _create_notebook(
        monkeypatch,
        dataset_info=[
            {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
        ],
        enable_notification=True,
        stop_hour=1,
        stop_minute=30,
        is_publicpath_readonly=True,
        is_projectuserspath_readonly=False,
    )

    assert body["dataset_info"] == [
        {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
    ]
    assert body["enable_notification"] is True
    assert body["stop_hour"] == 1
    assert body["stop_minute"] == 30
    # False is a value the caller chose; only `None` means "do not send".
    assert body["is_publicpath_readonly"] is True
    assert body["is_projectuserspath_readonly"] is False


def test_notebook_read_only_guards_stay_off_the_wire_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Read-only is the safer value, but turning it on by default would change
    # every existing create; the platform keeps deciding.
    body = _create_notebook(monkeypatch, dataset_info=[])
    assert "is_publicpath_readonly" not in body
    assert "is_projectuserspath_readonly" not in body
    assert "dataset_info" not in body


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(None, (None, None)), (2, (0, 2)), (45, (0, 45)), (90, (1, 30)), (1500, (25, 0))],
)
def test_auto_stop_after_splits_into_hours_and_minutes(
    minutes: int | None, expected: tuple[int | None, int | None]
) -> None:
    from inspire.cli.commands.notebook import notebook_create_flow

    assert notebook_create_flow._split_auto_stop_after(minutes) == expected


# ---------------------------------------------------------------------------
# training job payload
# ---------------------------------------------------------------------------


class _FakeJobConfig:
    path_aliases: dict[str, str] = {}
    remote_env: dict[str, str] = {}
    shm_size = None
    job_auto_fault_tolerance = False
    job_fault_tolerance_max_retry = None


def _training_plan(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> dict[str, Any]:
    monkeypatch.setattr(
        job_submit,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False: "registry/train:v1",
    )
    plan = job_submit.build_training_job_plan(
        config=_FakeJobConfig(),
        name="demo",
        command="python train.py",
        quota=ResolvedQuota(
            quota_id="quota-1",
            logic_compute_group_id="lcg-1",
            compute_group_name="Group",
            gpu_count=0,
            cpu_count=10,
            memory_gib=200,
            gpu_type="",
            raw_price={"cpu_count": 10, "memory_size_gib": 200, "gpu_count": 0},
        ),
        framework="pytorch",
        project_id="project-1",
        workspace_id="ws-1",
        image="train:v1",
        priority=5,
        nodes=1,
        max_time_hours=None,
        session=object(),
        **extra,
    )
    return plan.create_kwargs


def test_job_create_payload_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _training_plan(monkeypatch) == {
        "name": "demo",
        "command": "bash -c 'python train.py'",
        "framework": "pytorch",
        "project_id": "project-1",
        "workspace_id": "ws-1",
        "logic_compute_group_id": "lcg-1",
        "task_priority": 5,
        "enable_notification": False,
        "framework_config": [
            {
                "image_type": "SOURCE_PRIVATE",
                "image": "registry/train:v1",
                "instance_count": 1,
                "resource_spec_price": {
                    "cpu_type": "",
                    "cpu_count": 10,
                    "gpu_count": 0,
                    "memory_size_gib": 200,
                    "logic_compute_group_id": "lcg-1",
                    "quota_id": "quota-1",
                },
                "cpu": 10,
                "gpu_count": 0,
                "mem_gi": 200,
            }
        ],
    }


def test_job_create_payload_carries_the_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _training_plan(
        monkeypatch,
        dataset_info=[
            {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
        ],
        envs=[{"name": "PROBE", "value": "1"}],
        description="probe",
        keep_after_success_hours=0.5,
        keep_after_failure_hours=2,
        public_path_readonly=True,
    )

    assert payload["dataset_info"] == [
        {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
    ]
    assert payload["envs"] == [{"name": "PROBE", "value": "1"}]
    assert payload["description"] == "probe"
    # String milliseconds: a number is rejected with "invalid value for string
    # field reserveOnSuccessMs".
    assert payload["reserve_on_success_ms"] == "1800000"
    assert payload["reserve_on_fail_ms"] == "7200000"
    assert payload["is_publicpath_readonly"] is True


def test_job_fault_tolerance_interval_needs_fault_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _training_plan(
        monkeypatch,
        auto_fault_tolerance=True,
        fault_tolerance_max_retry=3,
        fault_tolerance_retry_interval_sec=60,
    )
    assert payload["fault_tolerance_retry_interval_sec"] == 60

    with pytest.raises(ValueError, match="--auto-fault-tolerance"):
        _training_plan(monkeypatch, fault_tolerance_retry_interval_sec=60)


# ---------------------------------------------------------------------------
# HPC payload
# ---------------------------------------------------------------------------


def _hpc_payload(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> dict[str, Any]:
    monkeypatch.setattr(
        hpc_commands,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False: "registry/hpc:v1",
    )
    return hpc_commands.build_hpc_create_payload(
        name="demo",
        logic_compute_group_id="lcg-1",
        project_id="project-1",
        workspace_id="ws-1",
        image="hpc:v1",
        image_type="SOURCE_PUBLIC",
        entrypoint="srun hostname",
        quota_id="quota-1",
        instance_count=1,
        task_priority=10,
        number_of_tasks=1,
        cpus_per_task=4,
        memory_per_cpu=4,
        enable_hyper_threading=False,
        resource_spec_price={"cpu_count": 4, "memory_size_gib": 16},
        session=object(),
        **extra,
    )


def test_hpc_create_payload_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _hpc_payload(monkeypatch) == {
        "job_name": "demo",
        "logic_compute_group_id": "lcg-1",
        "project_id": "project-1",
        "workspace_id": "ws-1",
        "enable_notification": False,
        "sbatch_script": {
            "number_of_tasks": 1,
            "cpus_per_task": 4,
            "memory_per_cpu": "4G",
            "enable_hyper_threading": False,
            "entrypoint": "srun hostname",
        },
        "slurm_cluster_spec": {
            "predef_quota_id": "quota-1",
            "cpu": 4,
            "mem_gi": 16,
            "image": "registry/hpc:v1",
            "image_type": "SOURCE_PUBLIC",
            "instance_count": 1,
            "spec_price": {"cpu_count": 4, "memory_size_gib": 16},
        },
        "priority": 10,
    }


def test_hpc_create_payload_carries_the_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _hpc_payload(
        monkeypatch,
        enable_notification=True,
        max_time_hours=25.5,
        dataset_info=[
            {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
        ],
        description="probe",
        keep_after_finish_hours=0.25,
        public_path_readonly=True,
    )

    assert payload["enable_notification"] is True
    assert payload["dataset_info"][0]["dataset_id"] == "pixabay-81k"
    assert payload["description"] == "probe"
    # Seconds here, unlike the millisecond fields on the training job.
    assert payload["ttl_after_job_finish_seconds"] == 900
    assert payload["is_publicpath_readonly"] is True


def test_hpc_runtime_cap_lives_inside_the_sbatch_script(monkeypatch: pytest.MonkeyPatch) -> None:
    # `max_running_time_ms` / `max_running_time_minutes` are unknown fields at
    # the top level; the cap is the Slurm `--time` string plus its breakdown,
    # both inside `sbatch_script`.
    payload = _hpc_payload(monkeypatch, max_time_hours=25.5)
    sbatch = payload["sbatch_script"]

    assert sbatch["job_max_time"] == "1-01:30:00"
    assert sbatch["max_running_time_days"] == 1
    assert sbatch["max_running_time_hours"] == 1
    assert sbatch["max_running_time_minutes"] == 30
    assert "max_running_time_ms" not in payload
    assert "job_max_time" not in payload


def test_hpc_notification_is_no_longer_hard_coded(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _hpc_payload(monkeypatch, enable_notification=True)["enable_notification"] is True
    assert _hpc_payload(monkeypatch)["enable_notification"] is False


# ---------------------------------------------------------------------------
# command surface: dry-run rendering and error mapping
# ---------------------------------------------------------------------------


class _FakeWebSession:
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"
    storage_state: dict[str, Any] = {}
    all_workspace_names = {"ws-77777777-7777-7777-7777-777777777777": "cpu"}
    all_workspace_ids = [workspace_id]


def _patch_create_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    verdicts: list[DatasetValidation],
) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    config.projects = {}
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )

    project = browser_api_module.ProjectInfo(
        project_id="project-12345678-1234-1234-1234-123456789abc",
        name="Project One",
        workspace_id=_FakeWebSession.workspace_id,
        priority_name="10",
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda workspace_id=None, session=None: [project],
    )
    monkeypatch.setattr(
        browser_api_module,
        "check_scheduling_health",
        lambda workspace_id=None, project_ids=None, session=None: {},
    )
    monkeypatch.setattr(
        browser_api_module,
        "select_project",
        lambda projects, requested=None, **_: (project, None),
    )

    def fake_resolve_quota(*, spec, workspace_id, session=None, **kwargs):  # noqa: ANN001
        return ResolvedQuota(
            quota_id="quota-12345678-1234-1234-1234-123456789abc",
            logic_compute_group_id="lcg-12345678-1234-1234-1234-123456789abc",
            compute_group_name="CPU Room",
            gpu_count=spec.gpu_count,
            cpu_count=spec.cpu_count,
            memory_gib=spec.memory_gib,
            gpu_type="",
            raw_price={"cpu_info": {"cpu_type": "Test"}},
        )

    from inspire.cli.commands.job import job_create as job_create_module
    from inspire.cli.utils import quota_resolver as quota_module
    from inspire.platform.web.browser_api import projects as projects_module

    monkeypatch.setattr(projects_module, "list_projects", lambda **_kwargs: [project])
    monkeypatch.setattr(job_create_module, "get_web_session", lambda: _FakeWebSession())
    monkeypatch.setattr(job_create_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        job_create_module, "select_workspace_id", lambda **_kwargs: _FakeWebSession.workspace_id
    )
    monkeypatch.setattr(job_create_module, "is_fair_scheduling_workspace", lambda *args: False)
    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: _FakeWebSession())
    monkeypatch.setattr(
        hpc_commands, "select_workspace_id", lambda **_kwargs: _FakeWebSession.workspace_id
    )
    monkeypatch.setattr(
        hpc_commands, "resolve_workspace_task_priority", lambda *args, **kwargs: 10
    )
    monkeypatch.setattr(quota_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        job_submit.web_session_module,
        "get_web_session",
        lambda: _FakeWebSession(),
    )
    monkeypatch.setattr(
        job_submit,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False: "registry/train:v1",
    )
    monkeypatch.setattr(
        hpc_commands,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False: "registry/hpc:v1",
    )

    monkeypatch.setattr(
        datasets_module,
        "validate_dataset_mounts",
        lambda mounts, *, workspace_id, session=None: verdicts,
    )
    monkeypatch.setattr(
        dataset_mounts,
        "validate_dataset_mounts",
        lambda mounts, *, workspace_id, session=None: verdicts,
    )
    del tmp_path


_OK_VERDICT = [
    DatasetValidation(
        dataset="pixabay-81k",
        version="v0",
        ok=True,
        path="sftpgo/pixabay-81k/v0",
    )
]
_REJECTED_VERDICT = [
    DatasetValidation(
        dataset="pexels-245k",
        version="v1",
        ok=False,
        error="无访问权限",
    )
]


def _job_create_args(*extra: str) -> list[str]:
    return [
        "job",
        "create",
        "--name",
        "probe",
        "--command",
        "python train.py",
        "--workspace",
        "cpu",
        "--project",
        "Project One",
        "--group",
        "CPU Room",
        "--quota",
        "0,4,16",
        "--image",
        "registry.local/train:latest",
        *extra,
    ]


def _hpc_create_args(*extra: str) -> list[str]:
    return [
        "hpc",
        "create",
        "--name",
        "probe",
        "--entrypoint",
        "srun hostname",
        "--workspace",
        "cpu",
        "--project",
        "Project One",
        "--group",
        "CPU Room",
        "--quota",
        "0,4,16",
        "--image",
        "registry.local/hpc:latest",
        *extra,
    ]


def test_job_dry_run_shows_the_resolved_mount_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_create_runtime(monkeypatch, tmp_path, verdicts=_OK_VERDICT)

    result = CliRunner().invoke(
        cli_main,
        _job_create_args("--dataset", "pixabay-81k:v0", "--dry-run"),
    )

    assert result.exit_code == 0, result.output
    assert "Dataset: pixabay-81k:v0 -> /inspire/dataset/pixabay-81k/v0" in result.output


def test_job_dry_run_json_lists_datasets_and_env_names_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_create_runtime(monkeypatch, tmp_path, verdicts=_OK_VERDICT)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            *_job_create_args(
                "--dataset",
                "pixabay-81k:v0",
                "--env",
                "PROBE_TOKEN=secret-value",
                "--dry-run",
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["datasets"] == [
        {"name": "pixabay-81k", "version": "v0", "path": "/inspire/dataset/pixabay-81k/v0"}
    ]
    # A value can be a token, and a plan gets printed and logged.
    assert data["env"] == ["PROBE_TOKEN"]
    assert "secret-value" not in result.output


def test_job_create_maps_a_rejected_mount_to_a_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_create_runtime(monkeypatch, tmp_path, verdicts=_REJECTED_VERDICT)

    result = CliRunner().invoke(
        cli_main,
        _job_create_args("--dataset", "pexels-245k:v1", "--dry-run"),
    )

    assert result.exit_code != 0
    assert "无访问权限" in result.output


def test_job_create_rejects_a_malformed_spec_before_resolving_anything() -> None:
    result = CliRunner().invoke(cli_main, _job_create_args("--dataset", "pixabay-81k"))

    assert result.exit_code == 2
    assert "'<dataset>:<version>'" in result.output


def test_hpc_dry_run_shows_datasets_time_and_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_create_runtime(monkeypatch, tmp_path, verdicts=_OK_VERDICT)

    result = CliRunner().invoke(
        cli_main,
        _hpc_create_args(
            "--dataset",
            "pixabay-81k:v0",
            "--max-time",
            "2",
            "--keep-after-finish",
            "0.5",
            "--enable-notification",
            "--dry-run",
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Dataset: pixabay-81k:v0 -> /inspire/dataset/pixabay-81k/v0" in result.output
    assert "Max time: 0-02:00:00 (day-hh:mm:ss)" in result.output
    assert "Keep after finish: 0.5 h" in result.output
    assert "Notifications: enabled" in result.output


def test_hpc_create_maps_a_rejected_mount_to_a_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_create_runtime(monkeypatch, tmp_path, verdicts=_REJECTED_VERDICT)

    result = CliRunner().invoke(
        cli_main,
        _hpc_create_args("--dataset", "pexels-245k:v1", "--dry-run"),
    )

    assert result.exit_code != 0
    assert "无访问权限" in result.output


def test_notebook_create_reports_where_the_dataset_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.cli.commands.notebook import notebook_create_flow as flow_module
    from inspire.cli.context import Context

    captured: dict[str, Any] = {}

    def fake_create_notebook(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"notebook_id": "nb-1111"}

    monkeypatch.setattr(flow_module.browser_api_module, "create_notebook", fake_create_notebook)
    monkeypatch.setattr(flow_module, "remember_resource_identity", lambda **kwargs: None)

    notebook_id = flow_module.create_notebook_and_report(
        Context(),
        name="probe",
        diagnostics=flow_module.NotebookCreateDiagnostics(
            name="probe",
            workspace="cpu",
            project="Project One",
            image="Image",
            resource="4CPU + 32GiB",
            compute_group="CPU Room",
        ),
        selected_project=SimpleNamespace(project_id="project-1", name="Project One"),
        selected_image=SimpleNamespace(image_id="image-1", url="registry/image", name="Image"),
        quota=ResolvedQuota(
            quota_id="quota-1",
            logic_compute_group_id="lcg-1",
            compute_group_name="CPU Room",
            gpu_count=0,
            cpu_count=4,
            memory_gib=32,
            gpu_type="",
            raw_price={},
        ),
        shm_size=8,
        auto_stop=False,
        workspace_id="ws-1",
        session=SimpleNamespace(all_workspace_names={"ws-1": "cpu"}),
        json_output=False,
        dataset_mounts=[DatasetMount("pixabay-81k", "v0")],
        dataset_info=[
            {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/pixabay-81k/v0"}
        ],
    )

    assert notebook_id == "nb-1111"
    assert captured["dataset_info"][0]["path"] == "sftpgo/pixabay-81k/v0"
