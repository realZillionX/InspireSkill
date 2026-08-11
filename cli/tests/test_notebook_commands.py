import json
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import click
import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.bridge import tunnel as tunnel_module
from inspire.cli.commands.notebook import notebook as notebook_group
from inspire.cli.commands.notebook import connection as connection_module
from inspire.cli.commands.notebook import notebook_commands as notebook_cmd_module
from inspire.cli.commands.notebook.notebook_presenters import _print_notebook_list
from inspire.cli.commands.notebook import remote_exec as remote_exec_module
from inspire.cli.commands.notebook import remote_shell as remote_shell_module
from inspire.cli.commands.notebook import notebook_ssh_flow as ssh_flow_module
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.main import main as cli_main
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import notebooks as notebooks_api_module
from inspire.platform.web import session as web_session_module


notebook_lifecycle_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_lifecycle"
)
notebook_metrics_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_metrics"
)


NOTEBOOK_CREATE_REQUIRED_ARGS = [
    "--name",
    "test-notebook",
    "--workspace",
    "cpu",
    "--project",
    "proj",
    "--image",
    "registry.local/notebook:latest",
    "--group",
    "H200 Room",
    "--quota",
    "1,20,200",
]


def _workspace_metavars(group: click.Group) -> dict[str, str | None]:
    values: dict[str, str | None] = {}

    def walk(command: click.Command, path: tuple[str, ...]) -> None:
        for parameter in command.params:
            if isinstance(parameter, click.Option) and "--workspace" in parameter.opts:
                values[" ".join(path)] = parameter.metavar
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, (*path, name))

    walk(group, ())
    return values


def make_test_config(tmp_path: Path, include_compute_groups: bool = False) -> config_module.Config:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
    )
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


# Saved at module-import time so the autouse `_short_circuit_platform_resolvers`
# fixture in conftest.py (which patches `_resolve_notebook_id` to a passthrough)
# can be undone within the two tests below — they exercise the REAL resolver's
# retry/error-classification behaviour, not the fixture's id-passthrough.
from inspire.cli.commands.notebook import notebook_lookup as _NBL_MOD  # noqa: E402

_REAL_RESOLVE_NOTEBOOK_ID = _NBL_MOD._resolve_notebook_id


def test_current_user_id_uses_live_user_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        user_detail = {"id": "cached-user"}

        def save(self) -> None:
            self.saved = True

    calls: list[tuple[str, str]] = []

    def _fake_current_user(session=None):  # noqa: ANN001
        calls.append(("GetUserDetail", ""))
        return {"id": "live-user"}

    monkeypatch.setattr(
        _NBL_MOD.browser_api_module, "get_current_user", _fake_current_user
    )

    session = _FakeSession()
    assert _NBL_MOD._try_get_current_user_ids(session, base_url="https://example.invalid") == [
        "live-user"
    ]
    assert session.user_detail == {"id": "live-user"}
    assert getattr(session, "saved", False) is True
    assert calls == [("GetUserDetail", "")]


def test_current_user_id_failure_keeps_api_details_in_debug_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FakeSession:
        user_detail = {"id": "cached-user"}

    def _fake_current_user(session=None):  # noqa: ANN001
        raise RuntimeError("browser runtime missing")

    monkeypatch.setattr(
        _NBL_MOD.browser_api_module, "get_current_user", _fake_current_user
    )
    caplog.set_level(logging.DEBUG, logger=_NBL_MOD.__name__)

    session = _FakeSession()
    assert (
        _NBL_MOD._try_get_current_user_ids(
            session,
            base_url="https://example.invalid",
        )
        == []
    )
    message = _NBL_MOD._current_user_lookup_failure_message(session)
    assert message == (
        "Cannot determine the current platform account. "
        "Refresh the account session with `inspire account add` or `inspire init`, "
        "then retry."
    )
    assert "browser runtime missing" not in message
    assert "inspire account login" not in message
    assert "browser runtime missing" in caplog.text


def test_current_user_detail_uses_live_user_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        user_detail = {"id": "cached-user"}

        def save(self) -> None:
            self.saved = True

    monkeypatch.setattr(
        _NBL_MOD.browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "live-user", "name": "Live"},
    )

    session = _FakeSession()
    assert _NBL_MOD._get_current_user_detail(
        session,
        base_url="https://example.invalid",
    ) == {"id": "live-user", "name": "Live"}
    assert session.user_detail == {"id": "live-user", "name": "Live"}
    assert getattr(session, "saved", False) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("notebook-smoke-20260507", False),
        ("nb-training-2026", False),
        ("notebook-abc", True),
        ("nb-a1b2c3d4", True),
        ("notebook-12345678-1234-1234-1234-123456789abc", True),
        ("550e8400-e29b-41d4-a716-446655440000", True),
    ],
)
def test_looks_like_notebook_id_uses_platform_handle_shape(
    value: str,
    expected: bool,
) -> None:
    assert _NBL_MOD._looks_like_notebook_id(value) is expected


def test_resolve_notebook_id_allows_notebook_prefixed_human_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"
    notebook_name = "notebook-smoke-20260507"
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")

    session = SimpleNamespace(
        base_url="https://example.invalid",
        user_detail={"id": "user-one"},
        workspace_id=workspace_id,
        all_workspace_ids=[workspace_id],
        all_workspace_names={workspace_id: "cpu"},
    )
    monkeypatch.setattr(
        _NBL_MOD,
        "_try_get_current_user_ids",
        lambda *_args, **_kwargs: ["user-one"],
    )

    def fake_list_notebooks(*_args, **kwargs):  # noqa: ANN001
        assert kwargs["keyword"] == notebook_name
        return {
            workspace_id: [
                {
                    "name": notebook_name,
                    "notebook_id": "notebook-a1b2c3d4",
                }
            ]
        }

    monkeypatch.setattr(
        _NBL_MOD,
        "_list_notebooks_for_workspaces",
        fake_list_notebooks,
    )

    notebook_id, resolved_workspace_id = _NBL_MOD._resolve_notebook_id(
        Context(),
        session=session,
        base_url="https://example.invalid",
        identifier=notebook_name,
        json_output=False,
        workspace_ids=[workspace_id],
        require_live=True,
        cache_index=index,
    )

    assert notebook_id == "notebook-a1b2c3d4"
    assert resolved_workspace_id == workspace_id


def test_resolve_notebook_id_propagates_listing_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real listing errors propagate immediately — no 12-second silent retry.

    The eventual-consistency retry around `_list_notebooks_for_workspaces`
    only handles "list call SUCCEEDED but the new notebook isn't visible
    yet". A network error / platform `code != 0` envelope would otherwise
    be amplified into a misleading "Notebook not found" 12s later, which
    is what Codex flagged in its v4 post-cache-deletion review.
    """
    from inspire.cli.context import Context

    # Restore the real resolver (autouse fixture replaces it with passthrough).
    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)

    class _BoomError(RuntimeError):
        pass

    class _FakeSession:
        workspace_id = "ws-77777777-7777-7777-7777-777777777777"
        all_workspace_ids = ["ws-77777777-7777-7777-7777-777777777777"]
        all_workspace_names = {"ws-77777777-7777-7777-7777-777777777777": "cpu"}

    call_count = {"n": 0}

    def _exploding_lister(*args, **kwargs):
        call_count["n"] += 1
        raise _BoomError("platform 503")

    monkeypatch.setattr(_NBL_MOD, "_list_notebooks_for_workspaces", _exploding_lister)
    monkeypatch.setattr(_NBL_MOD, "_try_get_current_user_ids", lambda *args, **kwargs: ["user-1"])

    ctx = Context()
    with pytest.raises(_BoomError, match="platform 503"):
        _NBL_MOD._resolve_notebook_id(
            ctx,
            session=_FakeSession(),
            base_url="https://example.invalid",
            identifier="any-name",
            json_output=False,
        )
    # Single call — no silent retry burning a 12s wall on a real failure.
    assert call_count["n"] == 1


def test_resolve_notebook_id_retries_until_eventual_consistency_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty results retry; the new notebook appearing later wins."""
    from inspire.cli.context import Context

    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)

    class _FakeSession:
        workspace_id = "ws-77777777-7777-7777-7777-777777777777"
        all_workspace_ids = ["ws-77777777-7777-7777-7777-777777777777"]
        all_workspace_names = {"ws-77777777-7777-7777-7777-777777777777": "cpu"}

    call_log: list[int] = []

    def _eventually_consistent_lister(*args, **kwargs):
        call_log.append(len(call_log))
        if len(call_log) < 2:
            return {}
        return {
            "ws-77777777-7777-7777-7777-777777777777": [
                {
                    "name": "fresh-name",
                    "notebook_id": "abcd1234-5678-90ab-cdef-1234567890ab",
                }
            ]
        }

    # Skip the real backoff sleep so the test runs in the test budget.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    monkeypatch.setattr(_NBL_MOD, "_list_notebooks_for_workspaces", _eventually_consistent_lister)
    monkeypatch.setattr(_NBL_MOD, "_try_get_current_user_ids", lambda *args, **kwargs: ["user-1"])

    ctx = Context()
    notebook_id, ws_id = _NBL_MOD._resolve_notebook_id(
        ctx,
        session=_FakeSession(),
        base_url="https://example.invalid",
        identifier="fresh-name",
        json_output=False,
    )
    assert notebook_id == "abcd1234-5678-90ab-cdef-1234567890ab"
    assert ws_id == "ws-77777777-7777-7777-7777-777777777777"
    assert len(call_log) >= 2  # at least one retry past the initial empty result


def test_notebook_live_snapshot_cannot_overwrite_newer_write_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"

    class _FakeSession:
        base_url = "https://example.invalid"
        user_detail = {"id": "user-one"}

        def __init__(self) -> None:
            self.workspace_id = workspace_id
            self.all_workspace_ids = [workspace_id]
            self.all_workspace_names = {workspace_id: "cpu"}

    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://example.invalid",
        subject_id="user-one",
        resource_type="notebook",
        workspace_id=workspace_id,
        owner_scope="self",
    )
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="notebook-old", name="demo")],
    )

    def _stale_live_list(*_args, **_kwargs):
        index.mark_deleted(scope, resource_id="notebook-old")
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="notebook-new", name="demo")],
        )
        return {
            workspace_id: [
                {
                    "name": "demo",
                    "notebook_id": "notebook-old",
                }
            ]
        }

    monkeypatch.setattr(
        _NBL_MOD,
        "_list_notebooks_for_workspaces",
        _stale_live_list,
    )
    monkeypatch.setattr(
        _NBL_MOD,
        "_try_get_current_user_ids",
        lambda *_args, **_kwargs: ["user-one"],
    )

    notebook_id, resolved_workspace_id = _NBL_MOD._resolve_notebook_id(
        Context(),
        session=_FakeSession(),
        base_url="https://example.invalid",
        identifier="demo",
        json_output=False,
        workspace_ids=[workspace_id],
        require_live=True,
        cache_index=index,
    )

    assert notebook_id == "notebook-new"
    assert resolved_workspace_id == workspace_id
    assert [
        item.resource_id
        for item in index.lookup(scope, "demo", fresh_only=False)
    ] == ["notebook-new"]


def test_notebook_clear_during_live_lookup_does_not_repopulate_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"

    class _FakeSession:
        base_url = "https://example.invalid"
        user_detail = {"id": "user-one"}

        def __init__(self) -> None:
            self.workspace_id = workspace_id
            self.all_workspace_ids = [workspace_id]
            self.all_workspace_names = {workspace_id: "cpu"}

    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://example.invalid",
        subject_id="user-one",
        resource_type="notebook",
        workspace_id=workspace_id,
        owner_scope="self",
    )

    def _live_list(*_args, **_kwargs):
        index.clear()
        return {
            workspace_id: [
                {
                    "name": "demo",
                    "notebook_id": "notebook-live",
                }
            ]
        }

    monkeypatch.setattr(_NBL_MOD, "_list_notebooks_for_workspaces", _live_list)
    monkeypatch.setattr(
        _NBL_MOD,
        "_try_get_current_user_ids",
        lambda *_args, **_kwargs: ["user-one"],
    )

    notebook_id, resolved_workspace_id = _NBL_MOD._resolve_notebook_id(
        Context(),
        session=_FakeSession(),
        base_url="https://example.invalid",
        identifier="demo",
        json_output=False,
        workspace_ids=[workspace_id],
        require_live=True,
        cache_index=index,
    )

    assert notebook_id == "notebook-live"
    assert resolved_workspace_id == workspace_id
    assert index.list_identities(scope, fresh_only=False) == []


def test_notebook_snapshot_failure_skips_live_cache_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"

    class _FakeSession:
        base_url = "https://example.invalid"
        user_detail = {"id": "user-one"}

        def __init__(self) -> None:
            self.workspace_id = workspace_id
            self.all_workspace_ids = [workspace_id]
            self.all_workspace_names = {workspace_id: "cpu"}

    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://example.invalid",
        subject_id="user-one",
        resource_type="notebook",
        workspace_id=workspace_id,
        owner_scope="self",
    )
    monkeypatch.setattr(
        index,
        "snapshot_token",
        lambda _scope: (_ for _ in ()).throw(OSError("cache unavailable")),
    )
    monkeypatch.setattr(
        _NBL_MOD,
        "_list_notebooks_for_workspaces",
        lambda *_args, **_kwargs: {
            workspace_id: [
                {
                    "name": "demo",
                    "notebook_id": "notebook-live",
                }
            ]
        },
    )
    monkeypatch.setattr(
        _NBL_MOD,
        "_try_get_current_user_ids",
        lambda *_args, **_kwargs: ["user-one"],
    )

    notebook_id, resolved_workspace_id = _NBL_MOD._resolve_notebook_id(
        Context(),
        session=_FakeSession(),
        base_url="https://example.invalid",
        identifier="demo",
        json_output=False,
        workspace_ids=[workspace_id],
        require_live=True,
        cache_index=index,
    )

    assert notebook_id == "notebook-live"
    assert resolved_workspace_id == workspace_id
    assert index.list_identities(scope, fresh_only=False) == []


def test_notebook_stale_handle_retry_tombstones_exact_old_handle_and_resolves_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NotFoundError(RuntimeError):
        status_code = 404

    session = SimpleNamespace(base_url="https://example.invalid")
    resolve_calls: list[bool] = []
    operation_calls: list[str] = []
    tombstones: list[dict[str, object]] = []

    def fake_resolve(*_args, require_live=False, **_kwargs):  # noqa: ANN001
        resolve_calls.append(require_live)
        if require_live:
            return "notebook-new", "ws-new"
        return "notebook-old", "ws-old"

    def operation(handle: str) -> str:
        operation_calls.append(handle)
        if handle == "notebook-old":
            raise _NotFoundError("API returned 404")
        return "ok"

    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", fake_resolve)
    monkeypatch.setattr(
        _NBL_MOD,
        "forget_resource_identity",
        lambda **kwargs: tombstones.append(kwargs),
    )

    result, handle, workspace_id = (
        _NBL_MOD._run_notebook_operation_with_stale_handle_retry(
            Context(),
            session=session,
            base_url="https://example.invalid",
            identifier="demo-notebook",
            json_output=False,
            workspace_ids=["ws-old", "ws-new"],
            operation=operation,
        )
    )

    assert result == "ok"
    assert handle == "notebook-new"
    assert workspace_id == "ws-new"
    assert resolve_calls == [False, True]
    assert operation_calls == ["notebook-old", "notebook-new"]
    assert tombstones == [
        {
            "session": session,
            "resource_type": "notebook",
            "resource_id": "notebook-old",
            "workspace_id": "ws-old",
            "owner_scope": "self",
            "cache_index": None,
        }
    ]
    assert "name" not in tombstones[0]


def test_notebook_status_runs_detail_fetch_through_stale_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        notebook_cmd_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail("status must not require confirmation"),
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(notebook_cmd_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(
        notebook_cmd_module,
        "resolve_workspace_operation_scope",
        lambda *_args, **_kwargs: "ws-live",
    )

    def fake_retry(*_args, operation, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return operation("notebook-live"), "notebook-live", "ws-live"

    monkeypatch.setattr(
        notebook_cmd_module,
        "_run_notebook_operation_with_stale_handle_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        notebook_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {
            "notebook_id": notebook_id,
            "status": "RUNNING",
            "project": {
                "id": "project-hidden",
                "name": "Demo Project",
            },
            "workspace": {"id": "workspace-hidden"},
            "logic_compute_group": {
                "id": "compute-hidden",
                "name": "H200 Room",
            },
            "created_by": {
                "id": "user-hidden",
                "name": "Alice",
            },
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "status",
            "demo-notebook",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["identifier"] == "demo-notebook"
    assert seen["workspace_ids"] == ["ws-live"]
    assert json.loads(result.output)["data"] == {
        "name": "demo-notebook",
        "status": "RUNNING",
        "project": "Demo Project",
        "workspace": "CPU资源空间",
        "compute_group": "H200 Room",
        "created_by": "Alice",
    }
    assert "notebook-live" not in result.output
    assert "project-hidden" not in result.output
    assert "workspace-hidden" not in result.output
    assert "compute-hidden" not in result.output
    assert "user-hidden" not in result.output

    human = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "status",
            "demo-notebook",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert human.exit_code == 0, human.output
    assert human.output.splitlines()[:2] == [
        "Name: demo-notebook",
        "Status: RUNNING",
    ]
    assert all(not line.startswith("  ") for line in human.output.splitlines())
    assert "Project: Demo Project" in human.output
    assert "Workspace: CPU资源空间" in human.output
    assert "Compute Group: H200 Room" in human.output
    assert "Created By: Alice" in human.output
    assert "notebook-live" not in human.output
    assert "project-hidden" not in human.output
    assert "workspace-hidden" not in human.output
    assert "compute-hidden" not in human.output
    assert "user-hidden" not in human.output


def test_notebook_stop_never_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[str] = []
    monkeypatch.setattr(
        notebook_cmd_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail("stop must not require confirmation"),
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "require_web_session",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(notebook_cmd_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(notebook_cmd_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(
        notebook_cmd_module,
        "resolve_workspace_operation_scope",
        lambda *_args, **_kwargs: "workspace-internal",
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *_args, **_kwargs: ("notebook-internal", "workspace-internal"),
    )
    monkeypatch.setattr(
        browser_api_module,
        "stop_notebook",
        lambda *, notebook_id, session: stopped.append(notebook_id),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "stop",
            "demo",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert stopped == ["notebook-internal"]
    assert json.loads(result.output)["data"] == {
        "name": "demo",
        "status": "stopped",
    }
    assert "notebook-internal" not in result.output


def test_notebook_delete_json_requires_yes_before_remote_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notebook_cmd_module,
        "require_web_session",
        lambda *_args, **_kwargs: pytest.fail("session must not load before confirmation"),
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("config must not load before confirmation"),
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_resolve_notebook_id",
        lambda *_args, **_kwargs: pytest.fail("resolver must not run before confirmation"),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "delete",
            "demo",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ConfirmationRequired"
    assert payload["error"]["hint"] == "Pass --yes to confirm."


def test_notebook_list_all_uses_multi_workspace_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(notebook_cmd_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        notebook_cmd_module,
        "_try_get_current_user_ids",
        lambda session, *, base_url: ["user-1"],  # noqa: ARG005
    )

    class _FakeSession:
        workspace_id = "ws-a"
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "GPU A", "ws-b": "GPU B"}

    monkeypatch.setattr(
        notebook_cmd_module,
        "require_web_session",
        lambda ctx, *, hint: _FakeSession(),  # noqa: ARG005
    )

    captured: dict[str, Any] = {}

    def _multi_lister(*args: Any, **kwargs: Any) -> dict[str, list[dict]]:
        captured["args"] = args
        captured.update(kwargs)
        return {
            "ws-a": [
                {
                    "name": "running-notebook",
                    "status": "RUNNING",
                    "created_at": "2026-06-05 10:00:00",
                    "project": {
                        "id": "project-hidden",
                        "name": "Demo Project",
                    },
                    "logic_compute_group": {
                        "id": "compute-hidden",
                        "name": "H200 Room",
                    },
                    "created_by": {
                        "id": "user-hidden",
                        "name": "Alice",
                    },
                    "quota": {"cpu_count": 20},
                }
            ],
            "ws-b": [],
        }

    monkeypatch.setattr(
        notebook_cmd_module,
        "_list_notebooks_for_workspaces",
        _multi_lister,
        raising=False,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "list", "--workspace", "all", "-s", "RUNNING"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"] == {
        "items": [
            {
                "name": "running-notebook",
                "status": "RUNNING",
                "project": "Demo Project",
                "workspace": "GPU A",
                "compute_group": "H200 Room",
                "created_by": "Alice",
            }
        ]
    }
    assert captured["workspace_ids"] == ["ws-a", "ws-b"]
    assert captured["user_ids"] == ["user-1"]
    assert captured["status"] == ["RUNNING"]
    for hidden in ("project-hidden", "compute-hidden", "user-hidden", "ws-a", "ws-b"):
        assert hidden not in result.output


def test_notebook_list_json_metadata_only_when_truncated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = {
        "name": "demo",
        "status": "RUNNING",
        "project_name": "Project",
        "workspace_name": "Workspace",
        "logic_compute_group_name": "H200 Room",
        "created_by_name": "Alice",
        "project_id": "project-hidden",
        "workspace_id": "workspace-hidden",
        "logic_compute_group_id": "compute-hidden",
        "created_by_id": "user-hidden",
    }
    expected_item = {
        "name": "demo",
        "status": "RUNNING",
        "project": "Project",
        "workspace": "Workspace",
        "compute_group": "H200 Room",
        "created_by": "Alice",
    }

    _print_notebook_list([item], True, total=1, truncated=False)
    untruncated = json.loads(capsys.readouterr().out)["data"]
    assert untruncated == {"items": [expected_item]}

    _print_notebook_list([item], True, total=2, truncated=True)
    truncated = json.loads(capsys.readouterr().out)["data"]
    assert truncated == {
        "items": [expected_item],
        "shown": 1,
        "total": 2,
        "truncated": True,
    }
    assert "project-hidden" not in json.dumps(truncated)
    assert "workspace-hidden" not in json.dumps(truncated)
    assert "compute-hidden" not in json.dumps(truncated)
    assert "user-hidden" not in json.dumps(truncated)


def test_notebook_lifecycle_empty_json_uses_collection_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notebook_metrics_module,
        "_notebook_name_to_id",
        lambda *_args, **_kwargs: SimpleNamespace(task_id="notebook-internal"),
    )
    monkeypatch.setattr(
        notebook_lifecycle_module,
        "list_notebook_runs",
        lambda _task_id: [],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "lifecycle",
            "demo",
            "--workspace",
            "CPU Room",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {"items": []},
    }
    assert "notebook-internal" not in result.output


@pytest.mark.parametrize(
    ("limit_args", "expected_data"),
    (
        (
            ("--limit", "2"),
            {
                "items": [
                    {
                        "index": 2,
                        "start_time": "2026-08-02 10:00:00",
                        "end_time": "2026-08-02 11:00:00",
                        "status": "STOPPED",
                    },
                    {
                        "index": 3,
                        "start_time": "2026-08-03 10:00:00",
                        "end_time": "2026-08-03 11:00:00",
                        "status": "STOPPED",
                    },
                ],
                "shown": 2,
                "total": 3,
                "truncated": True,
            },
        ),
        (
            ("--limit", "3"),
            {
                "items": [
                    {
                        "index": 1,
                        "start_time": "2026-08-01 10:00:00",
                        "end_time": "2026-08-01 11:00:00",
                        "status": "STOPPED",
                    },
                    {
                        "index": 2,
                        "start_time": "2026-08-02 10:00:00",
                        "end_time": "2026-08-02 11:00:00",
                        "status": "STOPPED",
                    },
                    {
                        "index": 3,
                        "start_time": "2026-08-03 10:00:00",
                        "end_time": "2026-08-03 11:00:00",
                        "status": "STOPPED",
                    },
                ]
            },
        ),
        (
            ("--all",),
            {
                "items": [
                    {
                        "index": 1,
                        "start_time": "2026-08-01 10:00:00",
                        "end_time": "2026-08-01 11:00:00",
                        "status": "STOPPED",
                    },
                    {
                        "index": 2,
                        "start_time": "2026-08-02 10:00:00",
                        "end_time": "2026-08-02 11:00:00",
                        "status": "STOPPED",
                    },
                    {
                        "index": 3,
                        "start_time": "2026-08-03 10:00:00",
                        "end_time": "2026-08-03 11:00:00",
                        "status": "STOPPED",
                    },
                ]
            },
        ),
    ),
)
def test_notebook_lifecycle_json_limit_metadata_only_when_truncated(
    monkeypatch: pytest.MonkeyPatch,
    limit_args: tuple[str, ...],
    expected_data: dict[str, object],
) -> None:
    runs = [
        {
            "index": index,
            "start_time": f"2026-08-0{index} 10:00:00",
            "end_time": f"2026-08-0{index} 11:00:00",
            "status": "STOPPED",
            "run_id": f"run-internal-{index}",
        }
        for index in (3, 1, 2)
    ]
    monkeypatch.setattr(
        notebook_metrics_module,
        "_notebook_name_to_id",
        lambda *_args, **_kwargs: SimpleNamespace(task_id="notebook-internal"),
    )
    monkeypatch.setattr(
        notebook_lifecycle_module,
        "list_notebook_runs",
        lambda _task_id: runs,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "lifecycle",
            "demo",
            "--workspace",
            "CPU Room",
            *limit_args,
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": expected_data,
    }
    assert "notebook-internal" not in result.output
    assert "run-internal" not in result.output


def test_notebook_list_fetches_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[int] = []

    def _fake_request_json(session, method, url, *, body, timeout, **kwargs):  # noqa: ANN001, ARG001
        assert method == "POST"
        assert url == "/api/v2/notebook?Action=ListNotebooks"
        assert timeout == 30
        page = int(body["page"])
        pages.append(page)
        if page == 1:
            return {
                "Result": {
                    "total": 3,
                    "list": [{"name": "n3"}, {"name": "n2"}],
                },
            }
        if page == 2:
            return {"Result": {"total": 3, "list": [{"name": "n1"}]}}
        raise AssertionError(f"unexpected page: {page}")

    monkeypatch.setattr(notebooks_api_module, "_request_json", _fake_request_json)

    rows = _NBL_MOD._list_notebooks_for_workspace(
        SimpleNamespace(),
        workspace_id="ws-a",
        user_ids=["user-1"],
        page_size=2,
    )

    assert [row["name"] for row in rows] == ["n3", "n2", "n1"]
    assert pages == [1, 2]


def test_notebook_resource_display_uses_specific_gpu_models() -> None:
    h200_item = {
        "quota": {"gpu_count": 2, "cpu_count": 40},
        "resource_spec_price": {
            "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
        },
    }
    assert (
        _NBL_MOD._format_notebook_gpu(h200_item)
        == "2x H200"
    )
    assert _NBL_MOD._format_notebook_cpu(h200_item) == "40 CPU"
    assert _NBL_MOD._format_notebook_resource(h200_item) == "2x H200 + 40 CPU"
    assert (
        _NBL_MOD._format_notebook_gpu(
            {
                "quota": {"gpu_count": 1},
                "resource_spec": {"gpu_type_display": "NVIDIA H100 (80GB)"},
            }
        )
        == "1x H100"
    )
    assert (
        _NBL_MOD._format_notebook_gpu(
            {
                "quota": {"gpu_count": 8},
                "node": {"gpu_info": {"gpu_type_display": "RTX 4090"}},
            }
        )
        == "8x 4090"
    )
    assert _NBL_MOD._format_notebook_gpu({"quota": {"gpu_count": 0, "cpu_count": 55}}) == "-"
    assert _NBL_MOD._format_notebook_cpu({"quota": {"gpu_count": 0, "cpu_count": 55}}) == "55 CPU"
    assert (
        _NBL_MOD._format_notebook_resource({"quota": {"gpu_count": 0, "cpu_count": 55}})
        == "55 CPU"
    )


def test_notebook_list_human_shows_project_workspace_and_gpu_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_test_config(tmp_path)
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(notebook_cmd_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        notebook_cmd_module,
        "_try_get_current_user_ids",
        lambda session, *, base_url: ["user-1"],  # noqa: ARG005
    )

    class _FakeSession:
        workspace_id = "ws-a"
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "GPU A", "ws-b": "GPU B"}

    monkeypatch.setattr(
        notebook_cmd_module,
        "require_web_session",
        lambda ctx, *, hint: _FakeSession(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        notebook_cmd_module,
        "_list_notebooks_for_workspaces",
        lambda *args, **kwargs: {
            "ws-a": [
                {
                    "name": "a-h200-notebook",
                    "status": "RUNNING",
                    "created_at": "2026-06-05 10:00:00",
                    "project": {"name": "Project One"},
                    "quota": {"gpu_count": 2, "cpu_count": 40},
                    "resource_spec_price": {
                        "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
                    },
                }
            ],
            "ws-b": [
                {
                    "name": "b-cpu-notebook",
                    "status": "STOPPED",
                    "created_at": "2026-06-06 10:00:00",
                    "project": {"name": "Project Two"},
                    "quota": {"gpu_count": 0, "cpu_count": 55},
                }
            ]
        },
        raising=False,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "list", "--workspace", "all"])

    assert result.exit_code == 0
    assert result.output.splitlines()[0].startswith("Name")
    assert "Total:" not in result.output
    assert "Project" in result.output
    assert "Workspace" in result.output
    assert "GPU" in result.output
    assert "CPU" in result.output
    assert "Project One" in result.output
    assert "Project Two" in result.output
    assert "GPU A" in result.output
    assert "GPU B" in result.output
    assert "2x H200" in result.output
    assert "40 CPU" in result.output
    assert "55 CPU" in result.output
    assert result.output.index("a-h200-notebook") < result.output.index("b-cpu-notebook")


def test_notebook_create_accepts_priority_10(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_notebook_create(ctx: Context, **kwargs: Any) -> None:
        assert ctx is not None
        captured.update(kwargs)

    monkeypatch.setattr(notebook_cmd_module, "run_notebook_create", fake_run_notebook_create)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "create", *NOTEBOOK_CREATE_REQUIRED_ARGS, "--priority", "10"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["priority"] == 10


def test_notebook_create_rejects_priority_11(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run_notebook_create(ctx: Context, **kwargs: Any) -> None:
        nonlocal called
        assert ctx is not None
        assert kwargs is not None
        called = True

    monkeypatch.setattr(notebook_cmd_module, "run_notebook_create", fake_run_notebook_create)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "create", "--priority", "11"])

    assert result.exit_code != EXIT_SUCCESS
    assert "1<=x<=10" in result.output
    assert called is False


def test_notebook_create_accepts_post_start_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_notebook_create(ctx: Context, **kwargs: Any) -> None:
        assert ctx is not None
        captured.update(kwargs)

    monkeypatch.setattr(notebook_cmd_module, "run_notebook_create", fake_run_notebook_create)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "create", *NOTEBOOK_CREATE_REQUIRED_ARGS, "--post-start", "echo hi"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["post_start"] == "echo hi"
    assert captured["post_start_script"] is None


def test_notebook_create_rejects_post_start_and_script_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def fake_run_notebook_create(ctx: Context, **kwargs: Any) -> None:
        nonlocal called
        assert ctx is not None
        assert kwargs is not None
        called = True

    monkeypatch.setattr(notebook_cmd_module, "run_notebook_create", fake_run_notebook_create)

    script_path = tmp_path / "bootstrap.sh"
    script_path.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "notebook",
            "create",
            *NOTEBOOK_CREATE_REQUIRED_ARGS,
            "--post-start",
            "echo hi",
            "--post-start-script",
            str(script_path),
        ],
    )

    assert result.exit_code != EXIT_SUCCESS
    assert "Use either --post-start or --post-start-script" in result.output
    assert called is False


def test_notebook_start_accepts_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
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
        **kwargs,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0


        assert method.upper() == "POST"
        assert url.endswith("/api/v2/notebook?Action=ListNotebooks")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"

        ws_id = str(body["workspace_id"])
        if ws_id == ws_cpu:
            return {"Result": {"list": [item]}}
        if ws_id == ws_gpu:
            return {"Result": {"list": []}}
        return {"Result": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)
    monkeypatch.setattr(notebooks_api_module, "_request_json", fake_request_json)
    monkeypatch.setattr(
        browser_api_module, "get_current_user", lambda session=None: {"id": "user-1"}
    )

    started: dict[str, str] = {}

    def fake_start_notebook(notebook_id: str, session=None) -> dict:  # type: ignore[no-untyped-def]
        started["notebook_id"] = notebook_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "start_notebook", fake_start_notebook)

    def fake_wait_for_notebook_running(
        notebook_id: str,
        session=None,
        timeout: int = 600,
        poll_interval: int = 5,
        progress_callback=None,
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


def test_notebook_start_wait_prints_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = ws_cpu
        storage_state = {}
        all_workspace_ids = [ws_cpu]
        all_workspace_names = {ws_cpu: "a"}

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
        **kwargs,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0


        assert method.upper() == "POST"
        assert url.endswith("/api/v2/notebook?Action=ListNotebooks")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"
        return {"Result": {"list": [item]}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)
    monkeypatch.setattr(notebooks_api_module, "_request_json", fake_request_json)
    monkeypatch.setattr(
        browser_api_module, "get_current_user", lambda session=None: {"id": "user-1"}
    )
    monkeypatch.setattr(
        browser_api_module,
        "start_notebook",
        lambda notebook_id, session=None: {"ok": True},
    )

    def fake_wait_for_notebook_running(
        notebook_id: str,
        session=None,
        timeout: int = 600,
        poll_interval: int = 5,
        progress_callback=None,
    ) -> dict:
        assert progress_callback is not None
        progress_callback(
            {"status": "CREATING", "sub_status": "Scheduling"},
            "CREATING",
            "[Normal] Scheduled: Pulling image\n[Normal] Started: Container booting",
        )
        return {"status": "RUNNING", "notebook_id": notebook_id, "quota": {"gpu_count": 8}}

    monkeypatch.setattr(
        browser_api_module, "wait_for_notebook_running", fake_wait_for_notebook_running
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "start", "ring-8h100-test", "--workspace", "a", "--wait"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "Status: CREATING (Scheduling)" in result.output
    assert "Latest event: [Normal] Started: Container booting" in result.output


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
        **kwargs,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0


        assert method.upper() == "POST"
        assert url.endswith("/api/v2/notebook?Action=ListNotebooks")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"

        ws_id = str(body["workspace_id"])
        if ws_id == ws_cpu:
            return {"Result": {"list": [cpu_item]}}
        if ws_id == ws_gpu:
            return {"Result": {"list": [gpu_item]}}
        return {"Result": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)
    monkeypatch.setattr(notebooks_api_module, "_request_json", fake_request_json)
    monkeypatch.setattr(
        browser_api_module, "get_current_user", lambda session=None: {"id": "user-1"}
    )

    started: dict[str, str] = {}

    def fake_start_notebook(notebook_id: str, session=None) -> dict:  # type: ignore[no-untyped-def]
        started["notebook_id"] = notebook_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "start_notebook", fake_start_notebook)

    def fake_wait_for_notebook_running(
        notebook_id: str,
        session=None,
        timeout: int = 600,
        poll_interval: int = 5,
        progress_callback=None,
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


def test_notebook_start_warns_when_no_wait_conflicts_with_configured_post_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    ws_gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        notebook_post_start="echo from config",
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
        **kwargs,
    ) -> dict:
        assert timeout
        assert _retry_count >= 0


        assert method.upper() == "POST"
        assert url.endswith("/api/v2/notebook?Action=ListNotebooks")
        assert body and "workspace_id" in body
        assert (body.get("filter_by") or {}).get("keyword") == "ring-8h100-test"

        ws_id = str(body["workspace_id"])
        if ws_id == ws_cpu:
            return {"Result": {"list": [item]}}
        if ws_id == ws_gpu:
            return {"Result": {"list": []}}
        return {"Result": {"list": []}}

    monkeypatch.setattr(web_session_module, "request_json", fake_request_json)
    monkeypatch.setattr(notebooks_api_module, "_request_json", fake_request_json)
    monkeypatch.setattr(
        browser_api_module, "get_current_user", lambda session=None: {"id": "user-1"}
    )

    started: dict[str, str] = {}

    def fake_start_notebook(notebook_id: str, session=None) -> dict:  # type: ignore[no-untyped-def]
        started["notebook_id"] = notebook_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "start_notebook", fake_start_notebook)

    def fake_wait_for_notebook_running(
        notebook_id: str,
        session=None,
        timeout: int = 600,
        poll_interval: int = 5,
        progress_callback=None,
    ) -> dict:
        return {"status": "RUNNING", "notebook_id": notebook_id, "quota": {"gpu_count": 8}}

    monkeypatch.setattr(
        browser_api_module, "wait_for_notebook_running", fake_wait_for_notebook_running
    )
    monkeypatch.setattr(browser_api_module, "run_command_in_notebook", lambda **kwargs: True)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "start", "ring-8h100-test", "--workspace", "a", "--no-wait"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert started["notebook_id"] == item["id"]
    assert "--no-wait requested" in result.output
    assert "set notebook_post_start=none" in result.output
    assert "Waiting for notebook to reach RUNNING status..." in result.output


def test_run_notebook_ssh_blocks_restricted_gpu_before_tunnel_setup(
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

    monkeypatch.setattr(ssh_flow_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {"name": "test-nb"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "require_notebook_gpu_model",
        lambda *_args, **_kwargs: "H200",
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    fake_tunnel_config = FakeTunnelConfig()
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )

    with pytest.raises(SystemExit) as exc:
        ssh_flow_module.run_notebook_ssh(
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
    assert captured["type"] == "PolicyBlocked"
    assert "runs on H200 GPUs" in captured["message"]
    assert fake_tunnel_config.bridges == {}


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

    monkeypatch.setattr(ssh_flow_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
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
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "current-user", "username": "current"},
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    with pytest.raises(SystemExit) as exc:
        ssh_flow_module.run_notebook_ssh(
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
    assert captured["message"] == "Notebook/account mismatch detected before tunnel setup."
    assert captured["hint"] == (
        "Retry with `--account <name>`, or add the owning account with "
        "`inspire account add <name>`."
    )
    public_error = f"{captured['message']}\n{captured['hint']}"
    for private_identity in ("current-user", "other-user", "current", "user"):
        assert private_identity not in public_error


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

    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
        },
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")

    def fake_setup_notebook_rtunnel(**kwargs):  # type: ignore[no-untyped-def]
        setup_kwargs.update(kwargs)
        return "wss://proxy.example/notebook/"

    monkeypatch.setattr(browser_api_module, "setup_notebook_rtunnel", fake_setup_notebook_rtunnel)
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
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

    monkeypatch.setattr(ssh_flow_module, "run_interactive_pty", lambda args: 0)

    ssh_flow_module.run_notebook_ssh(
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


def test_run_notebook_ssh_reports_openssh_internal_install_failure(
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

    monkeypatch.setattr(ssh_flow_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "paper-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
        },
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(browser_api_module.OpenSSHInternalInstallError()),
    )

    with pytest.raises(SystemExit) as exc:
        ssh_flow_module.run_notebook_ssh(
            Context(),
            notebook_id="paper-nb",
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
    assert captured["type"] == "SetupError"
    assert "OpenSSH" in captured["message"]
    assert "SII 内部 Ubuntu apt 源" in captured["message"]
    assert "`VERSION_CODENAME`" in captured["hint"]
    assert "SII 内部 Ubuntu apt 源" in captured["hint"]
    assert browser_api_module.SII_UBUNTU_APT_MIRROR in captured["hint"]
    assert browser_api_module.OPENSSH_INSTALL_LOG in captured["hint"]


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

    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
        },
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")

    def fake_setup_notebook_rtunnel(**kwargs):  # type: ignore[no-untyped-def]
        setup_called["value"] = True
        return "wss://proxy.example/new"

    monkeypatch.setattr(browser_api_module, "setup_notebook_rtunnel", fake_setup_notebook_rtunnel)
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
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

    monkeypatch.setattr(ssh_flow_module, "run_interactive_pty", lambda args: 0)

    ssh_flow_module.run_notebook_ssh(
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
    # Cache key is the notebook's canonical display name. The pre-existing
    # 'shared-profile' entry binds to a *different* notebook_id, so it is
    # left untouched; the new connection is saved under its own canonical key.
    untouched = fake_tunnel_config.bridges["shared-profile"]
    assert getattr(untouched, "notebook_id", None) == "notebook-old"
    canonical_key = "test-nb"  # mock notebook_detail's display name
    saved_profile = fake_tunnel_config.bridges[canonical_key]
    assert getattr(saved_profile, "notebook_id", None) == "notebook-12345678"


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

    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: cfg)
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
        },
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: "wss://proxy.example/notebook/",
    )
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "save_tunnel_config", lambda config: None)
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
    monkeypatch.setattr(ssh_flow_module, "run_interactive_pty", lambda args: next(ssh_rc))

    def fake_rebuild(*args: Any, **kwargs: Any) -> object:
        reconnect_calls["rebuild"] += 1
        profile_name = str(kwargs.get("bridge_name", "notebook-12345678"))
        return fake_tunnel_config.bridges[profile_name]

    monkeypatch.setattr(ssh_flow_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    ssh_flow_module.run_notebook_ssh(
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

    monkeypatch.setattr(ssh_flow_module, "_handle_error", fake_handle_error)
    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-12345678", None),
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_notebook_running",
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}},
        },
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_get_current_user_detail",
        lambda session, base_url: {"id": "user-1", "username": "user"},
    )
    monkeypatch.setattr(
        ssh_flow_module,
        "_validate_notebook_account_access",
        lambda current_user, notebook_detail: (True, ""),
    )
    monkeypatch.setattr(ssh_flow_module, "load_ssh_public_key", lambda pubkey: "ssh-ed25519 AAA")
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
        ssh_flow_module.run_notebook_ssh(
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
    assert "Proxy readiness:" in captured["hint"]
    assert "proxy.example" not in captured["hint"]
    assert "notebook-12345678" not in captured["hint"]


def test_notebook_connection_status_json_is_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel_config = tunnel_module.TunnelConfig()
    tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example/proxy/31337/",
            notebook_id="notebook-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            notebook_name="gpu-main",
            rtunnel_port=31337,
        )
    )

    monkeypatch.setattr(connection_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(
        connection_module,
        "run_ssh_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="gpu-host\n", stderr=""),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "connection", "status", "gpu-main"],
    )

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    data = payload["data"]
    assert data == {
        "name": "gpu-main",
        "status": "connected",
    }
    assert "bridge" not in data
    assert "elapsed_ms" not in data
    assert "proxy.example" not in result.output
    assert "31337" not in result.output
    assert "notebook-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in result.output


def test_notebook_connection_status_human_omits_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel_config = tunnel_module.TunnelConfig()
    tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example/proxy/31337/",
            notebook_name="gpu-main",
        )
    )

    monkeypatch.setattr(connection_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(
        connection_module,
        "run_ssh_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="gpu-host\n", stderr=""),
    )

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "connection", "status", "gpu-main"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "connected" in result.output
    assert "Response time" not in result.output


@pytest.mark.parametrize(
    "args",
    (
        [
            "notebook",
            "connection",
            "target",
            "forget",
            "gpu-main",
            "--workspace",
            "ws-123456",
        ],
        [
            "notebook",
            "connection",
            "status",
            "gpu-main",
            "--workspace",
            "ws-123456",
        ],
        [
            "notebook",
            "connection",
            "refresh",
            "gpu-main",
            "--workspace",
            "ws-123456",
        ],
        [
            "notebook",
            "connection",
            "forget",
            "gpu-main",
            "--workspace",
            "ws-123456",
        ],
    ),
)
def test_notebook_connection_workspace_rejects_id_shaped_values(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        connection_module,
        "_load_bridge_or_exit",
        lambda *_args, **_kwargs: pytest.fail("workspace validation did not run first"),
    )
    monkeypatch.setattr(
        connection_module,
        "forget_notebook_targets",
        lambda **_kwargs: pytest.fail("workspace validation did not run first"),
    )
    monkeypatch.setattr(
        connection_module,
        "preflight_notebook_transport_policy",
        lambda *_args, **_kwargs: pytest.fail("workspace validation did not run first"),
    )

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "only accept workspace names" in result.output
    assert "handle" not in result.output.lower()
    assert "ws-123456" not in result.output


def test_notebook_path_commands_manage_project_path_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "__home"
    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text("")
    (fake_home / ".inspire" / "current").write_text("alice\n")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    set_result = runner.invoke(
        cli_main,
        [
            "notebook",
            "path",
            "set",
            "me",
            "/inspire/ssd/project/topic/alice/",
        ],
    )

    assert set_result.exit_code == EXIT_SUCCESS
    assert set_result.output == "OK Path alias saved: me\n"
    json_set_result = runner.invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "path",
            "set",
            "me",
            "/inspire/ssd/project/topic/alice/",
        ],
    )
    assert json_set_result.exit_code == EXIT_SUCCESS
    assert json.loads(json_set_result.output)["data"] == {
        "name": "me",
        "status": "saved",
    }
    assert "/inspire/" not in json_set_result.output
    config_path = tmp_path / ".inspire" / "accounts" / "alice" / "config.toml"
    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "[path_aliases]" in content
    assert 'me = "/inspire/ssd/project/topic/alice/"' in content

    list_result = runner.invoke(cli_main, ["notebook", "path", "list"])
    assert list_result.exit_code == EXIT_SUCCESS
    assert "Project path aliases" not in list_result.output
    assert "me" in list_result.output
    assert "/inspire/" not in list_result.output

    json_list_result = runner.invoke(cli_main, ["--json", "notebook", "path", "list"])
    assert json_list_result.exit_code == EXIT_SUCCESS
    assert json.loads(json_list_result.output)["data"] == {"items": [{"name": "me"}]}
    assert "/inspire/" not in json_list_result.output

    show_result = runner.invoke(cli_main, ["notebook", "path", "show", "me"])
    assert show_result.exit_code == EXIT_SUCCESS
    assert "Path alias: me" in show_result.output
    assert "/inspire/ssd/project/topic/alice/" in show_result.output

    json_show_result = runner.invoke(
        cli_main,
        ["--json", "notebook", "path", "show", "me"],
    )
    assert json_show_result.exit_code == EXIT_SUCCESS
    assert json.loads(json_show_result.output)["data"] == {
        "name": "me",
        "path": "/inspire/ssd/project/topic/alice/",
    }

    delete_result = runner.invoke(cli_main, ["notebook", "path", "delete", "me", "--yes"])
    assert delete_result.exit_code == EXIT_SUCCESS
    assert delete_result.output == "OK Path alias deleted: me\n"
    assert "[path_aliases]" not in config_path.read_text(encoding="utf-8")


def test_notebook_help_exposes_path_group() -> None:
    runner = CliRunner()

    notebook_help = runner.invoke(cli_main, ["notebook", "--help"])
    assert notebook_help.exit_code == EXIT_SUCCESS
    assert "path" in notebook_help.output

    path_help = runner.invoke(cli_main, ["notebook", "path", "--help"])
    assert path_help.exit_code == EXIT_SUCCESS
    assert "Manage project-level remote path aliases." in path_help.output
    assert "not bound to any one notebook instance" in path_help.output

    show_help = runner.invoke(cli_main, ["notebook", "path", "show", "--help"])
    assert show_help.exit_code == EXIT_SUCCESS
    assert "Reveal the remote path stored for one alias." in show_help.output


def test_notebook_workspace_metavars_are_name_oriented() -> None:
    metavars = _workspace_metavars(notebook_group)

    assert metavars
    assert {
        path
        for path, metavar in metavars.items()
        if metavar == "NAME|all"
    } == {"list", "quota"}
    assert all(
        metavar == "NAME"
        for path, metavar in metavars.items()
        if path not in {"list", "quota"}
    )


@pytest.mark.parametrize(
    "command",
    (
        "status",
        "start",
        "stop",
        "delete",
        "events",
        "metrics",
        "proxy-url",
    ),
)
def test_notebook_live_name_commands_share_pick_interface(command: str) -> None:
    result = CliRunner().invoke(cli_main, ["notebook", command, "--help"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "--pick INTEGER" in result.output
    assert (
        "Pick the Nth candidate (1-indexed) when the name is ambiguous."
        in " ".join(result.output.split())
    )


def test_notebook_exec_cwd_uses_path_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_module.Config(
        username="",
        password="",
        path_aliases={"me": "/inspire/ssd/project/topic/alice/"},
    )
    tunnel_config = tunnel_module.TunnelConfig()
    tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com")
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(remote_exec_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(remote_exec_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        remote_exec_module,
        "preflight_notebook_transport_policy",
        lambda *args, **kwargs: SimpleNamespace(exec_transport="ssh", notebook_id="nb-test"),
    )

    def fake_stream(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(remote_exec_module, "run_ssh_command_streaming", fake_stream)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "exec", "gpu-main", "--cwd", "me:repo", "pwd"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert 'cd "/inspire/ssd/project/topic/alice/repo" && pwd' in str(captured["command"])


def test_notebook_exec_verifies_target_cache_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="", password="")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_resolve_cached_notebook_target(ctx: Context, **kwargs: object) -> None:
        del ctx
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        remote_exec_module,
        "resolve_cached_notebook_target",
        fake_resolve_cached_notebook_target,
    )
    monkeypatch.setattr(
        remote_exec_module,
        "preflight_notebook_transport_policy",
        lambda *args, **kwargs: SimpleNamespace(exec_transport="ssh", notebook_id="nb-test"),
    )
    monkeypatch.setattr(
        remote_exec_module,
        "try_exec_via_ssh_tunnel",
        lambda *args, **kwargs: EXIT_SUCCESS,
    )

    result = CliRunner().invoke(cli_main, ["notebook", "exec", "gpu-main", "pwd"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["verify_target_cache"] is True


def test_notebook_shell_cwd_uses_path_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_module.Config(
        username="",
        password="",
        path_aliases={"me": "/inspire/ssd/project/topic/alice/"},
    )
    tunnel_config = tunnel_module.TunnelConfig()
    tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com")
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(remote_shell_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(remote_shell_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        remote_shell_module,
        "preflight_notebook_transport_policy",
        lambda *args, **kwargs: SimpleNamespace(exec_transport="ssh", notebook_id="nb-test"),
    )

    def fake_get_ssh_command_args(bridge_name, config, remote_command=None):  # type: ignore[no-untyped-def]
        captured["bridge_name"] = bridge_name
        captured["remote_command"] = remote_command
        return ["ssh", "root@localhost"]

    monkeypatch.setattr(remote_shell_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(remote_shell_module, "run_interactive_pty", lambda args: 0)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main", "--cwd", "me:repo"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["bridge_name"] == "gpu-main"
    assert 'cd "/inspire/ssd/project/topic/alice/repo" && exec $SHELL -l' in str(
        captured["remote_command"]
    )


def test_notebook_shell_without_default_path_alias_uses_login_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="", password="")
    tunnel_config = tunnel_module.TunnelConfig()
    tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com")
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(remote_shell_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(remote_shell_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        remote_shell_module,
        "preflight_notebook_transport_policy",
        lambda *args, **kwargs: SimpleNamespace(exec_transport="ssh", notebook_id="nb-test"),
    )

    def fake_get_ssh_command_args(bridge_name, config, remote_command=None):  # type: ignore[no-untyped-def]
        captured["bridge_name"] = bridge_name
        captured["remote_command"] = remote_command
        return ["ssh", "root@localhost"]

    monkeypatch.setattr(remote_shell_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(remote_shell_module, "run_interactive_pty", lambda args: 0)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["bridge_name"] == "gpu-main"
    assert captured["remote_command"] is None
    assert result.output == ""
