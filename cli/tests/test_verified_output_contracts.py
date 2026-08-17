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

# Captured before conftest's autouse fixture swaps it for a passthrough.
_REAL_RESOLVE_IMAGE_NAME = importlib.import_module(
    "inspire.cli.commands.image.image_commands"
)._resolve_image_name


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


# ---------------------------------------------------------------------------
# Interactive shells: the remote exit has to end the local process
# ---------------------------------------------------------------------------


def test_shell_exit_marker_is_not_echoed_by_the_bootstrap() -> None:
    """The terminal echoes the bootstrap line, so the literal must be split.

    If the contiguous marker rode in on the command line, the watcher would
    trip on that echo and close the shell the instant it opened.
    """
    from inspire.cli.utils.job_shell import SHELL_BOOTSTRAP, SHELL_EXIT_MARKER

    assert SHELL_EXIT_MARKER not in SHELL_BOOTSTRAP
    # …and the shell still prints it contiguously.
    assert SHELL_BOOTSTRAP.rstrip().endswith("'")
    assert "exec bash" not in SHELL_BOOTSTRAP, "exec leaves nobody to announce the exit"


def test_shell_exit_watcher_reports_exit_and_hides_the_marker() -> None:
    from inspire.cli.utils.job_shell import SHELL_EXIT_MARKER, ShellExitWatcher

    watcher = ShellExitWatcher()
    visible, done = watcher.feed(b"tick=1\r\n")
    assert visible == b"tick=1\r\n" and not done

    visible, done = watcher.feed(b"exit\r\n" + SHELL_EXIT_MARKER.encode())
    assert done
    assert visible == b"exit\r\n"
    assert SHELL_EXIT_MARKER.encode() not in visible


def test_shell_exit_watcher_spans_frame_boundaries() -> None:
    """A marker split across two websocket frames still has to be caught."""
    from inspire.cli.utils.job_shell import SHELL_EXIT_MARKER, ShellExitWatcher

    marker = SHELL_EXIT_MARKER.encode()
    watcher = ShellExitWatcher()

    first, done = watcher.feed(b"bye\n" + marker[:9])
    assert not done
    assert first == b"bye\n"  # the partial marker is withheld, not printed

    second, done = watcher.feed(marker[9:])
    assert done
    assert second == b""


def test_shell_exit_watcher_releases_withheld_bytes_that_were_not_a_marker() -> None:
    from inspire.cli.utils.job_shell import ShellExitWatcher

    watcher = ShellExitWatcher()
    first, _ = watcher.feed(b"INSPIRE_SHELL_")
    second, done = watcher.feed(b"NOT_THE_MARKER\n")

    assert not done
    assert first + second + watcher.flush() == b"INSPIRE_SHELL_NOT_THE_MARKER\n"


def test_jupyter_bootstrap_also_announces_its_exit() -> None:
    """`notebook shell` on a restricted machine runs the same loop shape."""
    from inspire.cli.utils.job_shell import SHELL_EXIT_MARKER
    from inspire.platform.web.browser_api.jupyter_terminal import build_shell_bootstrap

    bootstrap = build_shell_bootstrap(cwd="/inspire/hdd/x", env_exports="")

    assert "exec $SHELL" not in bootstrap
    assert "$SHELL -l;" in bootstrap
    assert SHELL_EXIT_MARKER not in bootstrap
    assert bootstrap.startswith("cd ")


class _ScriptedWebSocket:
    """A websocket that replays frames and never closes, like the gateway.

    Selectable through a real socketpair so the shell loops' `select` works.
    """

    def __init__(self, frames: list[bytes]) -> None:
        import socket as _socket

        self._frames = list(frames)
        self._reader, self._writer = _socket.socketpair()
        for _ in self._frames:
            self._writer.send(b"x")
        self.sent: list[str] = []

    # The loops construct this as `cls(url, headers)`.
    @classmethod
    def factory(cls, frames: list[bytes]):
        made: dict[str, _ScriptedWebSocket] = {}

        def build(_url, _headers, **_kwargs):
            made["ws"] = cls(frames)
            return made["ws"]

        build.made = made  # type: ignore[attr-defined]
        return build

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._reader.close()
        self._writer.close()
        return False

    def fileno(self) -> int:
        return self._reader.fileno()

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def recv_frame(self):
        if not self._frames:
            raise EOFError
        self._reader.recv(1)
        return 0x2, self._frames.pop(0)


def test_job_shell_returns_when_the_remote_shell_announces_its_exit() -> None:
    """The gateway holds the socket open, so the marker is the only signal."""
    import io

    from inspire.cli.utils.job_shell import SHELL_EXIT_MARKER, run_remote_shell

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    # A closed stdin keeps it out of the select set; this exercises the read
    # side, which is where the marker has to be noticed.
    stdin = _ClosedStdin()
    build = _ScriptedWebSocket.factory(
        [b"tick=1\r\n", b"exit\r\n" + SHELL_EXIT_MARKER.encode()]
    )

    code = run_remote_shell(
        job_id="job-1",
        instance_name="worker-0",
        session=_ShellSession(),
        stdin=stdin,
        stdout=stdout,
        websocket_cls=build,  # type: ignore[arg-type]
    )

    assert code == 0
    stdout.flush()
    written = stdout.buffer.getvalue()  # type: ignore[attr-defined]
    assert b"tick=1" in written
    assert SHELL_EXIT_MARKER.encode() not in written


class _ShellSession:
    base_url = "https://qz.sii.edu.cn"
    storage_state = {"cookies": [{"name": "inspire-session", "value": "ok"}]}
    cookies: dict[str, str] = {}
    workspace_id = "ws-1"


class _ClosedStdin:
    closed = True

    def isatty(self) -> bool:
        return False


def test_jupyter_terminal_shell_returns_on_the_same_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`notebook shell` on a restricted machine shares the hang and the fix."""
    import io
    import json as _json

    from inspire.cli.utils import job_shell
    from inspire.platform.web.browser_api import jupyter_terminal

    frames = [
        _json.dumps(["stdout", "tick=1\r\n"]).encode(),
        _json.dumps(
            ["stdout", "exit\r\n" + job_shell.SHELL_EXIT_MARKER]
        ).encode(),
    ]
    build = _ScriptedWebSocket.factory(frames)
    monkeypatch.setattr(job_shell, "_WebSocketClient", build)

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    code = jupyter_terminal._run_jupyter_terminal_shell(
        ws_url="wss://qz.sii.edu.cn/terminals/1",
        session=_ShellSession(),  # type: ignore[arg-type]
        bootstrap="$SHELL -l\r",
        stdin=_ClosedStdin(),
        stdout=stdout,
    )

    assert code == 0
    stdout.flush()
    written = stdout.buffer.getvalue()  # type: ignore[attr-defined]
    assert b"tick=1" in written
    assert job_shell.SHELL_EXIT_MARKER.encode() not in written


# ---------------------------------------------------------------------------
# image register: only one add_method the CLI can actually drive
# ---------------------------------------------------------------------------


def test_register_uses_the_add_method_that_works() -> None:
    """`add_method=0` answers `InvalidParameter: no image uploaded`.

    That is the console's 文件上传 route and needs a file upload this CLI does
    not implement; 2 is 本地推送, which reserves the slot and returns the
    address to push to. The CLI used to default to 0.
    """
    from inspire.cli.commands.image.image_commands import IMAGE_ADD_METHOD_LOCAL_PUSH

    assert IMAGE_ADD_METHOD_LOCAL_PUSH == 2


def test_platform_reason_is_repeated_without_the_payload() -> None:
    from inspire.cli.commands.image.image_commands import _platform_reason

    conflict = RuntimeError(
        "request payload {'name': 'x'} failed: "
        "API error: Conflict: Duplicated image name and version: x:v1"
    )
    assert _platform_reason(conflict) == (
        ": Conflict: Duplicated image name and version: x:v1"
    )
    # Nothing recognisable: say nothing rather than echo the request body.
    assert _platform_reason(RuntimeError("socket hang up")) == "."


# ---------------------------------------------------------------------------
# image name resolution: version is part of the identity
# ---------------------------------------------------------------------------


def _patch_image_catalog(monkeypatch: pytest.MonkeyPatch, names: list[tuple[str, str]]):
    from inspire.cli.commands.image import image_commands
    from inspire.platform.web.browser_api import CustomImageInfo

    def fake_list(source="official", session=None, workspace_id=None):
        if source != "private":
            return []
        return [
            CustomImageInfo(
                image_id=f"img-{n}-{v}",
                url=f"registry/{n}:{v}",
                name=n,
                framework="",
                version=v,
                source="SOURCE_PUBLIC",
                status="SUCCESS",
                description="",
                created_at="0",
                visibility="VISIBILITY_PRIVATE",
            )
            for n, v in names
        ]

    monkeypatch.setattr(
        image_commands.browser_api_module, "list_images_by_source", fake_list
    )
    monkeypatch.setattr(
        image_commands, "require_web_session", lambda ctx, hint=None: object()
    )
    monkeypatch.setattr(
        image_commands, "_resolve_registry_scope", lambda ctx, **kwargs: "ws-1"
    )


def _resolve_bare(
    monkeypatch: pytest.MonkeyPatch, name: str, catalog: list[tuple[str, str]]
) -> tuple[int, str]:
    """Call the real resolver and return (exit code, stderr).

    conftest replaces `_resolve_image_name` with a passthrough for the whole
    suite, so the resolver is exercised through the reference captured at
    import time rather than through the CLI.
    """
    import io as _io
    from contextlib import redirect_stderr

    from inspire.cli.context import Context

    _patch_image_catalog(monkeypatch, catalog)

    captured = _io.StringIO()
    try:
        with redirect_stderr(captured):
            _REAL_RESOLVE_IMAGE_NAME(
                Context(), name, session=object(), workspace_id="ws-1"
            )
    except SystemExit as exit_info:
        return int(exit_info.code or 0), captured.getvalue()
    return 0, captured.getvalue()


def test_bare_image_name_lists_the_versions_that_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare name used to answer a flat "not found"."""
    code, err = _resolve_bare(
        monkeypatch, "runtime", [("runtime", "v1"), ("runtime", "v2"), ("other", "v1")]
    )

    assert code != 0
    assert "needs a version" in err
    assert "runtime:v1" in err and "runtime:v2" in err
    # One mistake, one error block.
    assert err.count("Error:") == 1
    assert "No image with name" not in err


def test_unknown_image_name_still_says_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, err = _resolve_bare(monkeypatch, "nope", [("runtime", "v1")])

    assert code != 0
    assert "No image with name" in err
    assert "needs a version" not in err


def test_exact_name_version_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.cli.context import Context

    _patch_image_catalog(monkeypatch, [("runtime", "v1"), ("runtime", "v2")])

    assert (
        _REAL_RESOLVE_IMAGE_NAME(
            Context(), "runtime:v2", session=object(), workspace_id="ws-1"
        )
        == "img-runtime-v2"
    )


def test_image_list_keyword_filters_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry holds thousands of images; the console has a name search."""
    from inspire.cli.main import main as cli_main

    _patch_image_catalog(
        monkeypatch, [("runtime-a", "v1"), ("runtime-b", "v1"), ("other", "v1")]
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "image",
            "list",
            "--workspace",
            "CPU资源空间",
            "--source",
            "private",
            "--keyword",
            "RUNTIME-A",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "runtime-a:v1" in result.output
    assert "runtime-b" not in result.output
    assert "other" not in result.output
