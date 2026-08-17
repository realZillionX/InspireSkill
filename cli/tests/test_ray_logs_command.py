"""`inspire ray logs` — instance selection, output budget, and Name-only output."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.ray import ray_commands, ray_logs as ray_logs_module
from inspire.cli.commands.ray.ray_commands import (
    RayInstanceSelectionError,
    ray_instance_views,
    select_ray_instance_views,
)
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.ray_jobs import RayJobInfo


class _FakeSession:
    workspace_id = "ws-session"
    all_workspace_names = {"ws-ray": "Ray资源空间"}
    all_workspace_ids = ["ws-ray"]


_INSTANCES = [
    {
        "instance_id": "rj-abc-head-1",
        "name": "rj-abc-head-9x2kd",
        "instance_type": "head",
        "status": "running",
        "cpu_count": 2,
        "memory_size": 8,
        "gpu_count": 0,
    },
    {
        "instance_id": "rj-abc-worker-1",
        "name": "rj-abc-decode-aaaaa",
        "instance_type": "worker",
        "worker_group_name": "decode",
        "status": "running",
        "rank": 0,
    },
    {
        "instance_id": "rj-abc-worker-2",
        "name": "rj-abc-decode-bbbbb",
        "instance_type": "worker",
        "worker_group_name": "decode",
        "status": "running",
        "rank": 1,
    },
]


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        del cls, require_credentials
        return config, {}

    for module in (ray_commands, ray_logs_module):
        monkeypatch.setattr(
            module.Config,
            "from_files_and_env",
            classmethod(fake_from_files_and_env),
        )


def _patch_resolution(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(ray_logs_module, "get_web_session", lambda: session)
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
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        lambda ray_job_id, *, limit, session: (list(_INSTANCES), len(_INSTANCES)),
    )
    # A live cluster started an hour ago: the default window is its own
    # lifetime, so keep the fixture relative rather than time-bombed.
    created_ms = int(time.time() * 1000) - 60 * 60 * 1000
    monkeypatch.setattr(
        ray_logs_module,
        "get_ray_job_detail",
        lambda ray_job_id, session=None: {
            "status": "RUNNING",
            "created_at": str(created_ms),
            "finished_at": None,
        },
    )


# ---------------------------------------------------------------------------
# instance views — the Name-only identity `--instance` selects on
# ---------------------------------------------------------------------------


def test_ray_instance_views_label_head_and_ranked_workers() -> None:
    views = ray_instance_views(_INSTANCES)

    assert [view.label for view in views] == ["head", "decode-0", "decode-1"]
    # The handle stays the raw pod name — it is what `GetJobLog` scopes on.
    assert [view.handle for view in views] == [
        "rj-abc-head-9x2kd",
        "rj-abc-decode-aaaaa",
        "rj-abc-decode-bbbbb",
    ]


def test_select_ray_instance_views_matches_group_type_and_rank() -> None:
    views = ray_instance_views(_INSTANCES)

    assert [v.label for v in select_ray_instance_views(views, ["head"])] == ["head"]
    assert [v.label for v in select_ray_instance_views(views, ["decode"])] == [
        "decode-0",
        "decode-1",
    ]
    assert [v.label for v in select_ray_instance_views(views, ["worker"])] == [
        "decode-0",
        "decode-1",
    ]
    assert [v.label for v in select_ray_instance_views(views, ["decode-1"])] == [
        "decode-1"
    ]
    # No selector means the whole cluster, not an empty scope.
    assert len(select_ray_instance_views(views, [])) == 3


def test_select_ray_instance_views_rejects_an_unmatched_selector() -> None:
    views = ray_instance_views(_INSTANCES)

    with pytest.raises(RayInstanceSelectionError) as excinfo:
        select_ray_instance_views(views, ["encode"])

    message = str(excinfo.value)
    assert "encode" in message
    assert "decode" in message
    # The hint lists readable identities, never pod handles.
    assert "rj-abc" not in message


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def test_ray_logs_requests_every_pod_and_relabels_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    session = _FakeSession()
    _patch_resolution(monkeypatch, session)
    captured: dict[str, Any] = {}

    def fake_logs(*, pod_names, start_timestamp_ms, end_timestamp_ms, page_size, session):  # noqa: ANN001
        captured["pod_names"] = list(pod_names)
        captured["page_size"] = page_size
        captured["window"] = (start_timestamp_ms, end_timestamp_ms)
        return (
            [
                {
                    "log_id": "log-1",
                    "pod_name": "rj-abc-head-9x2kd",
                    "time": "2026-08-15 10:00:00",
                    "timestamp_ms": 1_770_000_001_000,
                    "message": "driver up",
                },
                {
                    "log_id": "log-2",
                    "pod_name": "rj-abc-decode-aaaaa",
                    "time": "2026-08-15 10:00:01",
                    "timestamp_ms": 1_770_000_002_000,
                    "message": "worker up",
                },
            ],
            2,
        )

    monkeypatch.setattr(ray_logs_module, "list_ray_job_logs", fake_logs)

    result = CliRunner().invoke(
        cli_main,
        ["ray", "logs", "pipeline", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert captured["pod_names"] == [
        "rj-abc-head-9x2kd",
        "rj-abc-decode-aaaaa",
        "rj-abc-decode-bbbbb",
    ]
    assert captured["page_size"] == 100
    start_ms, end_ms = captured["window"]
    assert 0 < start_ms < end_ms
    assert result.output.startswith("Ray Logs")
    assert "driver up" in result.output
    # Pod handles are replaced by the identity `ray instances` prints.
    assert "head" in result.output
    assert "decode-0" in result.output
    assert "9x2kd" not in result.output
    assert "rj-abc" not in result.output


def test_ray_logs_instance_filter_narrows_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    captured: dict[str, Any] = {}

    def fake_logs(*, pod_names, session, **kwargs):  # noqa: ANN001
        captured["pod_names"] = list(pod_names)
        return [], 0

    monkeypatch.setattr(ray_logs_module, "list_ray_job_logs", fake_logs)

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "logs",
            "pipeline",
            "--workspace",
            "Ray资源空间",
            "--instance",
            "head",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["pod_names"] == ["rj-abc-head-9x2kd"]
    assert "No Ray logs found." in result.output


def test_ray_logs_unknown_instance_fails_before_any_log_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    calls: list[Any] = []

    def fake_logs(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return [], 0

    monkeypatch.setattr(ray_logs_module, "list_ray_job_logs", fake_logs)

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "logs",
            "pipeline",
            "--workspace",
            "Ray资源空间",
            "--instance",
            "encode",
        ],
    )

    assert result.exit_code != 0
    assert "No Ray instance matches 'encode'" in result.output
    assert calls == []


def test_ray_logs_json_carries_the_truncation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())

    monkeypatch.setattr(
        ray_logs_module,
        "list_ray_job_logs",
        lambda **kwargs: (
            [
                {
                    "log_id": f"log-{index}",
                    "pod_name": "rj-abc-decode-aaaaa",
                    "time": "2026-08-15 10:00:00",
                    "timestamp_ms": 1_770_000_000_000 + index,
                    "message": f"line {index}",
                }
                for index in range(5)
            ],
            42,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "logs", "pipeline", "--workspace", "Ray资源空间", "--tail", "2"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["shown"] == 2
    assert payload["total"] == 42
    assert payload["truncated"] is True
    assert payload["limit"] == 2
    assert payload["character_limit"] == 16_000
    assert [item["message"] for item in payload["logs"]] == ["line 3", "line 4"]
    assert all(item["pod_name"] == "decode-0" for item in payload["logs"])


def test_ray_logs_reports_no_instances_instead_of_an_empty_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cluster with no pods must not read as "this cluster printed nothing"."""
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        lambda ray_job_id, *, limit, session: ([], 0),
    )
    calls: list[Any] = []
    monkeypatch.setattr(
        ray_logs_module,
        "list_ray_job_logs",
        lambda **kwargs: calls.append(kwargs) or ([], 0),
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "logs", "pipeline", "--workspace", "Ray资源空间"],
    )

    assert result.exit_code != 0
    assert "No instances found for Ray job pipeline" in result.output
    assert calls == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--tail", "5", "--head", "5"], "--tail and --head cannot be used together."),
        (["--all", "--tail", "5"], "--all cannot be combined with --tail"),
        (["--all", "--limit", "5"], "--all cannot be combined with --limit"),
        (["--window", "soon"], "use a window like 30m or 2h"),
    ],
)
def test_ray_logs_usage_errors_are_pre_api(
    monkeypatch: pytest.MonkeyPatch, args: list[str], message: str
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    calls: list[Any] = []
    monkeypatch.setattr(
        ray_logs_module,
        "list_ray_job_logs",
        lambda **kwargs: calls.append(kwargs) or ([], 0),
    )

    result = CliRunner().invoke(
        cli_main,
        ["ray", "logs", "pipeline", "--workspace", "Ray资源空间", *args],
    )

    assert result.exit_code != 0
    assert message in result.output
    assert calls == []


def test_ray_logs_clamps_a_window_wider_than_a_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    captured: dict[str, Any] = {}

    def fake_logs(*, start_timestamp_ms, end_timestamp_ms, **kwargs):  # noqa: ANN001
        captured["window"] = (start_timestamp_ms, end_timestamp_ms)
        return [], 0

    monkeypatch.setattr(ray_logs_module, "list_ray_job_logs", fake_logs)

    result = CliRunner().invoke(
        cli_main,
        ["ray", "logs", "pipeline", "--workspace", "Ray资源空间", "--window", "60d"],
    )

    assert result.exit_code == 0, result.output
    start_ms, end_ms = captured["window"]
    assert end_ms - start_ms == ray_logs_module.RAY_LOG_MAX_WINDOW_MS
    assert "Window shortened to the most recent 30 days" in result.output
