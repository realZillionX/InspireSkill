"""`inspire ray scaling` — elastic replica history projection and budget."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.ray import ray_commands, ray_scaling as ray_scaling_module
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

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        del cls, require_credentials
        return config, {}

    for module in (ray_commands, ray_scaling_module):
        monkeypatch.setattr(
            module.Config,
            "from_files_and_env",
            classmethod(fake_from_files_and_env),
        )


def _patch_resolution(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(ray_scaling_module, "get_web_session", lambda: session)
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
                    name="pipeline",
                    status="RUNNING",
                    workspace_id=kwargs["workspace_id"],
                    project_id="project-1",
                    project_name="Project 1",
                    created_at="1770000000000",
                    finished_at=None,
                    created_by_id="user-1",
                    created_by_name="tester",
                    priority=1,
                    raw={},
                )
            ],
            1,
        ),
    )


def _history(count: int) -> list[dict[str, Any]]:
    return [
        {
            "event_time": str(1_770_000_000_000 + index * 60_000),
            "event_type": "scale_up" if index % 2 else "scale_down",
            "worker_group_name": "decode",
            "replicas_before": index,
            "replicas_after": index + 1,
            "ray_job_id": "rj-abc",
        }
        for index in range(count)
    ]


def test_ray_scaling_renders_replica_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    captured: dict[str, Any] = {}

    def fake_histories(ray_job_id, *, worker_group_name, page_num, page_size, session):  # noqa: ANN001
        captured.update(
            ray_job_id=ray_job_id,
            worker_group_name=worker_group_name,
            page_size=page_size,
        )
        return (
            [
                {
                    "event_time": "1770000000000",
                    "event_type": "initialized",
                    "worker_group_name": "decode",
                    "replicas_before": 0,
                    "replicas_after": 1,
                },
                {
                    "event_time": "1770000060000",
                    "event_type": "scale_up",
                    "worker_group_name": "decode",
                    "replicas_before": 1,
                    "replicas_after": 4,
                },
            ],
            2,
        )

    monkeypatch.setattr(
        ray_scaling_module, "list_ray_job_scaling_histories", fake_histories
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "scaling", "pipeline", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert captured["ray_job_id"] == "rj-abc"
    assert captured["worker_group_name"] == ""
    # Ordering is done here, so the whole result set has to come back.
    assert captured["page_size"] == -1
    header = result.output.splitlines()[0]
    assert "Time" in header and "Group" in header and "Replicas" in header
    assert "initialized" in result.output
    assert "1 -> 4" in result.output
    assert "decode" in result.output
    assert "rj-abc" not in result.output


def test_ray_scaling_group_filter_goes_to_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    captured: dict[str, Any] = {}

    def fake_histories(ray_job_id, *, worker_group_name, **kwargs):  # noqa: ANN001
        captured["worker_group_name"] = worker_group_name
        return [], 0

    monkeypatch.setattr(
        ray_scaling_module, "list_ray_job_scaling_histories", fake_histories
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "scaling",
            "pipeline",
            "--workspace",
            "Ray资源空间",
            "--group",
            "decode",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["worker_group_name"] == "decode"
    assert "No Ray scaling history found." in result.output


def test_ray_scaling_keeps_the_most_recent_changes_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    rows = _history(25)
    # Hand them back newest-first: the Action declares no sorter, so the
    # command may not assume the platform's order.
    monkeypatch.setattr(
        ray_scaling_module,
        "list_ray_job_scaling_histories",
        lambda ray_job_id, **kwargs: (list(reversed(rows)), 25),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "scaling", "pipeline", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["shown"] == 20
    assert payload["total"] == 25
    assert payload["truncated"] is True
    # Oldest-first inside the page, and the page is the newest 20 of 25.
    assert [item["replicas_before"] for item in payload["items"]] == list(range(5, 25))


def test_ray_scaling_all_shows_the_complete_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    rows = _history(25)
    monkeypatch.setattr(
        ray_scaling_module,
        "list_ray_job_scaling_histories",
        lambda ray_job_id, **kwargs: (list(rows), 25),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "scaling", "pipeline", "--workspace", "Ray资源空间", "--all"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert len(payload["items"]) == 25
    assert "truncated" not in payload
    assert set(payload["items"][0]) == {
        "time",
        "event",
        "group",
        "replicas_before",
        "replicas_after",
    }


def test_ray_scaling_limit_and_all_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    calls: list[Any] = []
    monkeypatch.setattr(
        ray_scaling_module,
        "list_ray_job_scaling_histories",
        lambda *args, **kwargs: calls.append(kwargs) or ([], 0),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "scaling",
            "pipeline",
            "--workspace",
            "Ray资源空间",
            "--all",
            "--limit",
            "5",
        ],
    )

    assert result.exit_code != 0
    assert "Use either --limit or --all, not both." in result.output
    assert calls == []


def test_ray_scaling_surfaces_platform_failure_instead_of_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())

    def _boom(*args, **kwargs):  # noqa: ANN001
        raise ValueError("Ray Job scaling_histories failed: API error: Throttling")

    monkeypatch.setattr(
        ray_scaling_module, "list_ray_job_scaling_histories", _boom
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "scaling", "pipeline", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code != 0
    assert "No Ray scaling history found." not in result.output
    assert "Throttling" in result.output
