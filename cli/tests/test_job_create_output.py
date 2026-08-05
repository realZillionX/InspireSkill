from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_create
from inspire.cli.main import main as cli_main
from inspire.cli.utils import job_submit
from inspire.cli.utils.quota_resolver import ResolvedQuota
from inspire.config import Config


class _FakeSession:
    workspace_id = "workspace-internal"


def _patch_job_create_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )

    monkeypatch.setattr(
        job_create.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )
    monkeypatch.setattr(job_create, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        job_create,
        "select_workspace_id",
        lambda *args, **kwargs: "workspace-internal",
    )

    quota = ResolvedQuota(
        quota_id="quota-internal",
        logic_compute_group_id="group-internal",
        compute_group_name="CPU Group",
        gpu_count=0,
        cpu_count=4,
        memory_gib=16,
        gpu_type="",
        raw_price={},
    )
    monkeypatch.setattr(job_create, "resolve_quota", lambda **kwargs: quota)
    monkeypatch.setattr(
        job_create.job_submit,
        "select_project_for_workspace",
        lambda *args, **kwargs: (
            SimpleNamespace(
                project_id="project-internal",
                name="Project",
                priority_name=None,
            ),
            None,
        ),
    )
    monkeypatch.setattr(job_create, "is_fair_scheduling_workspace", lambda *args: False)
    monkeypatch.setattr(job_create, "resolve_task_priority", lambda *args, **kwargs: 5)
    monkeypatch.setattr(
        job_create.job_submit,
        "build_training_job_plan",
        lambda **kwargs: job_submit.JobSubmissionPlan(
            create_kwargs={},
            log_path=None,
            wrapped_command="python train.py",
            max_time_ms=None,
            project_name="Project",
            workspace_id="workspace-internal",
            quota=quota,
        ),
    )
    monkeypatch.setattr(
        job_create.job_submit,
        "submit_training_job",
        lambda **kwargs: job_submit.JobSubmission(
            job_id="job-internal",
            data={
                "job_id": "job-internal",
                "name": "backend-name",
                "status": "QUEUING",
                "message": "backend message",
                "request": {"trace_id": "trace-internal"},
            },
            result={"code": 0, "data": {"debug": "backend-debug"}},
            log_path=None,
            wrapped_command="python train.py",
            max_time_ms=None,
        ),
    )
    monkeypatch.setattr(
        job_create,
        "remember_resource_identity",
        lambda **kwargs: None,
    )


def _job_create_args(*, json_output: bool) -> list[str]:
    args = [
        "job",
        "create",
        "--name",
        "train",
        "--quota",
        "0,4,16",
        "--command",
        "python train.py",
        "--workspace",
        "Workspace",
        "--project",
        "Project",
        "--group",
        "CPU Group",
        "--image",
        "python:3.12",
    ]
    return ["--json", *args] if json_output else args


def test_job_create_json_uses_stable_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_job_create_runtime(monkeypatch)

    result = CliRunner().invoke(cli_main, _job_create_args(json_output=True))

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {
            "name": "train",
            "status": "created",
        },
    }
    assert "job-internal" not in result.output
    assert "backend-name" not in result.output
    assert "QUEUING" not in result.output
    assert "backend message" not in result.output
    assert "trace-internal" not in result.output
    assert "backend-debug" not in result.output


def test_job_create_human_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_job_create_runtime(monkeypatch)

    result = CliRunner().invoke(cli_main, _job_create_args(json_output=False))

    assert result.exit_code == 0, result.output
    assert result.output == "OK Job created: train\n"
