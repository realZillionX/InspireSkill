"""Regressions for output defects found against the live platform (2026-08-15).

Each case here was reproduced on qz.sii.edu.cn with a real notebook, job, or
image before the fix landed, so the fixtures use the exact payload shapes the
platform returns rather than idealised ones.
"""

from __future__ import annotations

import datetime as dt
import importlib

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_logs
from inspire.cli.formatters.json_formatter import sanitize_text
from inspire.cli.formatters.table import clip_display
from inspire.cli.utils import events as events_util

# The notebook package re-exports the command under the module's own name, so
# the module itself has to be imported explicitly.
notebook_lifecycle_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_lifecycle"
)


# ---------------------------------------------------------------------------
# Job logs: nanosecond ordering
# ---------------------------------------------------------------------------


def test_job_logs_order_by_sub_millisecond_time() -> None:
    """A burst inside one millisecond keeps the order the job wrote it in.

    `nvidia-smi --format=csv` prints its header then its row 20 microseconds
    later; both round to the same `timestamp_ms`, and the log store handed
    them back row-first.
    """
    header = {
        "message": "name, memory.total [MiB]",
        "timestamp_ms": "1786818724490",
        "time": "2026-08-16T02:32:04.490877458+08:00",
    }
    row = {
        "message": "NVIDIA H100 80GB HBM3, 81559 MiB",
        "timestamp_ms": "1786818724490",
        "time": "2026-08-16T02:32:04.490897958+08:00",
    }

    ordered = sorted([row, header], key=job_logs._web_log_sort_key)

    assert [item["message"] for item in ordered] == [
        "name, memory.total [MiB]",
        "NVIDIA H100 80GB HBM3, 81559 MiB",
    ]


def test_job_log_sort_key_tolerates_missing_time() -> None:
    assert job_logs._web_log_sub_ms({}) == 0
    assert job_logs._web_log_sub_ms({"time": "not-a-timestamp"}) == 0


# ---------------------------------------------------------------------------
# Notebook lifecycle: platform clock rendered in the machine's clock
# ---------------------------------------------------------------------------


def test_lifecycle_run_times_are_converted_from_platform_local() -> None:
    """`ListRunIndex` answers in the platform's own +08:00 wall clock."""
    converted = notebook_lifecycle_module._to_local("2026-08-16 01:58:03")

    # Same instant as 17:58:03Z, whatever this machine's zone is.
    expected = (
        dt.datetime(2026, 8, 15, 17, 58, 3, tzinfo=dt.timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    assert converted == expected


def test_lifecycle_leaves_unparseable_run_times_alone() -> None:
    assert notebook_lifecycle_module._to_local("") == ""
    assert notebook_lifecycle_module._to_local("later today") == "later today"


# ---------------------------------------------------------------------------
# Events: columns the stream does not carry are dropped
# ---------------------------------------------------------------------------


def test_notebook_events_drop_kubernetes_classification_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Notebook lifecycle events are `{time, message}` and nothing else."""
    events_util.render_events_table(
        [
            {"time": "2026-08-15 13:57:49", "message": "The service is starting up..."},
            {"time": "2026-08-15 13:58:03", "message": "Notebook is ready"},
        ]
    )

    out = capsys.readouterr().out
    assert "Time" in out and "Message" in out
    for dead_column in ("Type", "Reason", "Count"):
        assert dead_column not in out


def test_workload_events_keep_classification_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_util.render_events_table(
        [
            {
                "time": "2026-08-15 14:14:14",
                "type": "Warning",
                "reason": "FailedScheduling",
                "count": 3,
                "message": "0/1483 nodes are unavailable",
            }
        ]
    )

    out = capsys.readouterr().out
    for column in ("Type", "Reason", "Count"):
        assert column in out


# ---------------------------------------------------------------------------
# Path sanitisation: a shared path stays whole
# ---------------------------------------------------------------------------


def test_shared_path_with_placeholder_is_not_split_and_redacted() -> None:
    """The `scp` hint quotes `/inspire/<storage>/...` and must survive it."""
    rendered = sanitize_text(
        "inspire notebook scp <ssh-notebook> <local-path> /inspire/<storage>/...",
        redact_paths=True,
    )

    assert rendered.endswith("/inspire/<storage>/...")
    assert "<redacted>" not in rendered


def test_local_paths_are_still_redacted() -> None:
    rendered = sanitize_text("failed at /Users/someone/.ssh/id_ed25519", redact_paths=True)

    assert "<redacted>" in rendered
    assert "someone" not in rendered


def test_platform_paths_are_redacted_when_asked() -> None:
    rendered = sanitize_text(
        "wrote /inspire/hdd/project/topic/user/out.bin",
        redact_paths=True,
        redact_platform_paths=True,
    )

    assert rendered == "wrote <redacted>"


# ---------------------------------------------------------------------------
# Quota table: the compute-group name is the value `--group` demands verbatim
# ---------------------------------------------------------------------------


def test_quota_table_prints_full_compute_group_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real groups differ only in a suffix past the old 28-column clip."""
    from inspire.cli.commands import workload_quota
    from inspire.cli.main import main as cli_main

    long_names = (
        "开发区-H100-cuda12.8版本-119核",
        "开发区-H100-cuda12.8版本-183核",
    )

    class _FakeSession:
        workspace_id = "ws-1"
        storage_state = {"cookies": [{"name": "session", "value": "ok"}]}

    monkeypatch.setattr(
        workload_quota.Config, "from_files_and_env", lambda **kwargs: (object(), [])
    )
    monkeypatch.setattr(workload_quota, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        workload_quota,
        "resolve_workspace_operation_scope",
        lambda **kwargs: "ws-1",
    )
    monkeypatch.setattr(
        workload_quota, "workspace_name_map", lambda session: {"ws-1": "分布式训练空间"}
    )
    monkeypatch.setattr(
        workload_quota,
        "_query_workspace_quotas",
        lambda **kwargs: [
            {
                "workspace": "分布式训练空间",
                "compute_group": name,
                "gpu_type": "NVIDIA H100 (80GB)",
                "quota": "1,20,200",
                "priority": "any",
                "allowed_priority_levels": [],
            }
            for name in long_names
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["job", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    for name in long_names:
        assert name in result.output
    assert clip_display(long_names[0], 28) not in result.output


# ---------------------------------------------------------------------------
# Batch dry run: the plan its help promises
# ---------------------------------------------------------------------------


def test_batch_dry_run_prints_plans_not_just_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--dry-run` help says it prints plans; it used to print a name list."""
    from inspire.cli.commands import batch as batch_module
    from inspire.cli.context import Context

    ctx = Context()
    batch_module._emit_batch_result(
        ctx,
        outputs=[
            {
                "name": "cli-verify-batch-1",
                "workspace": "分布式训练空间",
                "shared_memory_gib": 8,
                "datasets": [{"name": "pixabay-81k", "version": "v0"}],
                "env": ["BATCH_KEY"],
                "public_path_readonly": True,
            }
        ],
        output_limit=None,
        dry_run=True,
    )

    out = capsys.readouterr().out
    assert "Plan: cli-verify-batch-1" in out
    assert "Shared memory gib: 8" in out
    assert "BATCH_KEY" in out
    assert "Public path readonly: yes" in out


def test_batch_submit_result_stays_a_name_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inspire.cli.commands import batch as batch_module
    from inspire.cli.context import Context

    batch_module._emit_batch_result(
        Context(),
        outputs=[{"name": "cli-verify-batch-1"}],
        output_limit=None,
    )

    assert capsys.readouterr().out == "- cli-verify-batch-1\n"


def test_batch_job_plan_fields_cover_what_the_item_submits() -> None:
    """Every field `_prepare_training_item` reads has a plan column."""
    from inspire.cli.commands import batch as batch_module

    planned = {source for source, _target in batch_module._PUBLIC_FIELDS_BY_KIND["job"]}
    for applied in (
        "description",
        "keep_after_success",
        "keep_after_failure",
        "public_path_readonly",
        "fault_tolerance_retry_interval",
    ):
        assert applied in planned


# ---------------------------------------------------------------------------
# Images: `visibility`, not `source`, answers who can see one
# ---------------------------------------------------------------------------


def test_image_visibility_reads_the_visibility_field() -> None:
    """A notebook-saved image is SOURCE_PUBLIC with VISIBILITY_PRIVATE.

    Reporting `source` as the visibility labelled the whole 个人可见镜像 list
    "public".
    """
    from inspire.cli.commands.image.image_commands import _image_visibility
    from inspire.platform.web.browser_api import CustomImageInfo

    def image(source: str, visibility: str) -> CustomImageInfo:
        return CustomImageInfo(
            image_id="img-1",
            url="registry/x:v1",
            name="x",
            framework="",
            version="v1",
            source=source,
            status="SUCCESS",
            description="",
            created_at="0",
            visibility=visibility,
        )

    assert _image_visibility(image("SOURCE_PUBLIC", "VISIBILITY_PRIVATE")) == "private"
    assert _image_visibility(image("SOURCE_PUBLIC", "VISIBILITY_PUBLIC")) == "public"
    assert _image_visibility(image("SOURCE_PRIVATE", "VISIBILITY_PROJECT")) == "project"
    # Official images carry no visibility of their own.
    assert _image_visibility(image("SOURCE_OFFICIAL", "")) == "official"


def test_project_visible_images_are_reachable() -> None:
    """The web picker's 项目可见镜像 tab had no CLI equivalent."""
    from inspire.cli.commands.image.image_commands import (
        _ALL_SOURCE_KEYS,
        _PUBLIC_SOURCE_CHOICES,
        _parse_visibility_value,
    )

    assert "project" in _PUBLIC_SOURCE_CHOICES
    assert "project" in _ALL_SOURCE_KEYS
    assert _parse_visibility_value("project") == "VISIBILITY_PROJECT"


def test_list_images_by_source_sends_the_project_visibility_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import images as images_api

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        images_api,
        "_get_session_and_workspace_id",
        lambda workspace_id=None, session=None: (object(), "ws-1"),
    )
    monkeypatch.setattr(
        images_api,
        "_image_v2",
        lambda session, action, body: captured.update(body=body) or {"images": []},
    )

    images_api.list_images_by_source(source="project", workspace_id="ws-1")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["filter"]["visibility"] == "VISIBILITY_PROJECT"
    assert body["filter"]["source_list"] == ["SOURCE_PRIVATE", "SOURCE_PUBLIC"]


# ---------------------------------------------------------------------------
# Notebook status: the fields the web detail page shows
# ---------------------------------------------------------------------------


def test_notebook_status_reports_auto_stop_countdown_and_priority() -> None:
    """`--auto-stop-after` had no readback, and priority never rendered.

    The platform puts the countdown in `left_time` and the priority under
    `project`, which is where the web 剩余运行时长 / 优先级 columns read them.
    """
    from inspire.cli.commands.notebook.public_output import public_notebook

    view = public_notebook(
        {
            "name": "cli-verify-cpu",
            "status": "RUNNING",
            "left_time": "14331",
            "live_time": "69",
            "project": {
                "name": "CI-扩散音视频生成",
                "priority_level": "HIGH",
                "priority_name": "10",
            },
        }
    )

    assert view["auto_stop_in_seconds"] == "14331"
    assert view["priority"] == "10"
    assert view["priority_level"] == "HIGH"


def test_public_image_delete_refusal_names_the_one_way_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public image cannot be deleted or un-published by its creator.

    The platform answers `AccessForbidden: 您没有权限删除该镜像。`, which read
    as a transient permission glitch behind the old generic message.
    """
    from inspire.cli.commands.image import image_commands
    from inspire.cli.main import main as cli_main

    monkeypatch.setattr(
        image_commands, "require_web_session", lambda ctx, hint=None: object()
    )
    monkeypatch.setattr(
        image_commands, "_resolve_registry_scope", lambda ctx, **kwargs: "ws-1"
    )
    monkeypatch.setattr(
        image_commands, "_resolve_image_name", lambda ctx, name, **kwargs: "img-1"
    )

    def refuse(**_kwargs: object) -> None:
        raise RuntimeError("API error: AccessForbidden: 您没有权限删除该镜像。")

    monkeypatch.setattr(image_commands.browser_api_module, "delete_image", refuse)

    result = CliRunner().invoke(
        cli_main,
        ["image", "delete", "shared:v1", "--workspace", "CPU资源空间", "--yes"],
    )

    assert result.exit_code != 0
    assert "Cannot delete a public image." in result.output
    assert "neither deleted nor made private again" in result.output
    # The platform's raw refusal carries the request payload.
    assert "AccessForbidden" not in result.output
