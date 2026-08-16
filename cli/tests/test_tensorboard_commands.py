from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.tensorboard import tensorboard_commands, tensorboard_data
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.tensorboards import TensorboardInfo


class _FakeSession:
    workspace_id = "ws-train"
    all_workspace_names = {"ws-train": "分布式训练空间"}
    all_workspace_ids = ["ws-train"]


def _board(**overrides: Any) -> TensorboardInfo:
    fields: dict[str, Any] = {
        "tb_id": "tb-1",
        "name": "glm-sft",
        "status": "running",
        "job_id": "",
        "job_name": "",
        "summary_path": "/inspire/hdd/project/p/u/runs/glm-sft",
        "url": "https://notebook.example/tensorboard/tb-1/",
        "project_name": "前沿课题探索",
        "compute_group_name": "训练区-H200-1号机房",
        "auto_stop_ms": "86400000",
        "running_time_ms": "1000",
        "created_at": "1769591284000",
    }
    fields.update(overrides)
    return TensorboardInfo(**fields)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, boards: list[TensorboardInfo]) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    for module in (tensorboard_commands, tensorboard_data):
        monkeypatch.setattr(
            module.Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (config, {})),
        )
        monkeypatch.setattr(module, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        tensorboard_commands,
        "select_workspace_id",
        lambda **kwargs: "ws-train",
    )
    monkeypatch.setattr(
        tensorboard_commands.browser_api_module,
        "list_tensorboards",
        lambda **kwargs: (list(boards), len(boards)),
    )
    monkeypatch.setattr(
        tensorboard_commands.browser_api_module,
        "get_tensorboard",
        lambda tb_id, session=None: next(b for b in boards if b.tb_id == tb_id),
    )


def test_list_shows_the_summary_path_and_leaves_job_blank_for_standalone_boards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch, [_board(), _board(tb_id="tb-2", name="run2", job_name="job-a")])
    result = CliRunner().invoke(
        cli_main, ["tensorboard", "list", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "Summary Path" in result.output
    assert "/inspire/hdd/project/p/u/runs/glm-sft" in result.output
    assert "job-a" in result.output


def test_list_job_filter_narrows_to_boards_attached_to_that_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform's own `job_id` filter answers nothing, so this is client-side."""
    _patch_runtime(monkeypatch, [_board(), _board(tb_id="tb-2", name="run2", job_name="job-a")])
    result = CliRunner().invoke(
        cli_main,
        ["--json", "tensorboard", "list", "--workspace", "分布式训练空间", "--job", "job-a"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["name"] for item in items] == ["run2"]


def test_list_reports_an_empty_workspace_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runtime(monkeypatch, [])
    result = CliRunner().invoke(
        cli_main, ["tensorboard", "list", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "No TensorBoards found." in result.output


def test_status_hides_the_board_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Agent has no browser; `tags` / `scalars` read the app for it."""
    _patch_runtime(monkeypatch, [_board()])
    result = CliRunner().invoke(
        cli_main, ["--json", "tensorboard", "status", "glm-sft", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    detail = json.loads(result.output)["data"]
    assert "url" not in detail
    assert detail["auto_stop_hours"] == "24"
    assert detail["summary_path"] == "/inspire/hdd/project/p/u/runs/glm-sft"


def test_commands_reject_a_platform_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runtime(monkeypatch, [_board()])
    result = CliRunner().invoke(
        cli_main,
        [
            "tensorboard",
            "status",
            "tb-0e4c8d02-1f1a-4bb3-9f0d-2b7d1c9a5e11",
            "--workspace",
            "分布式训练空间",
        ],
    )

    assert result.exit_code != 0
    assert "only accept tensorboard names" in result.output


def test_create_refuses_a_compute_group_that_does_not_run_tensorboards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quoting one reaches the platform as `已选择的计算类型组不支持此类型任务`."""
    _patch_runtime(monkeypatch, [])
    monkeypatch.setattr(
        tensorboard_commands.browser_api_module,
        "list_compute_groups",
        lambda **kwargs: [
            {
                "name": "开发区-H200-3号机房",
                "logic_compute_group_id": "lcg-1",
                "support_job_type_list": '["distributed_training"]',
            }
        ],
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "tensorboard",
            "create",
            "-n",
            "glm-sft",
            "--workspace",
            "分布式训练空间",
            "--project",
            "前沿课题探索",
            "--group",
            "开发区-H200-3号机房",
            "--summary-path",
            "/logs",
        ],
    )

    assert result.exit_code != 0
    assert "does not run TensorBoards" in result.output


def test_create_rejects_an_auto_stop_over_the_platform_ceiling() -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "tensorboard",
            "create",
            "-n",
            "glm-sft",
            "--workspace",
            "分布式训练空间",
            "--project",
            "前沿课题探索",
            "--group",
            "训练区-H200-1号机房",
            "--summary-path",
            "/logs",
            "--auto-stop-hours",
            "80",
        ],
    )

    assert result.exit_code != 0
    assert "72" in result.output


def test_reading_a_stopped_board_says_so_instead_of_failing_at_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch, [_board(status="stopped")])
    result = CliRunner().invoke(
        cli_main, ["tensorboard", "tags", "glm-sft", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code != 0
    assert "is stopped; only a running board serves data" in result.output
    assert "inspire tensorboard start glm-sft" in result.output


def test_scalars_summarizes_each_series_by_step_not_by_event_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event files interleave a resumed or multi-worker run; steps are the order."""
    _patch_runtime(monkeypatch, [_board()])
    monkeypatch.setattr(
        tensorboard_data.browser_api_module,
        "read_tensorboard_scalar_tags",
        lambda url, session=None: {".": ["train/loss"]},
    )
    monkeypatch.setattr(
        tensorboard_data.browser_api_module,
        "read_tensorboard_scalar_series",
        lambda url, run, tag, session=None: [
            (100.0, 20, 0.5),
            (99.0, 10, 2.0),
            (101.0, 30, 0.1),
        ],
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "tensorboard",
            "scalars",
            "glm-sft",
            "--workspace",
            "分布式训练空间",
            "--points",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    series = json.loads(result.output)["data"]["series"][0]
    assert series["first_step"] == 10 and series["first_value"] == 2.0
    assert series["last_step"] == 30 and series["last_value"] == 0.1
    assert series["min"] == 0.1 and series["max"] == 2.0
    assert series["points"] == [[20, 0.5], [30, 0.1]]


def test_scalars_reports_a_running_board_with_no_events_as_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch, [_board()])
    monkeypatch.setattr(
        tensorboard_data.browser_api_module,
        "read_tensorboard_scalar_tags",
        lambda url, session=None: {},
    )
    result = CliRunner().invoke(
        cli_main, ["tensorboard", "scalars", "glm-sft", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "No scalar data" in result.output
