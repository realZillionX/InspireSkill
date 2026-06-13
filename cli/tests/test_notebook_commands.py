import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.bridge import tunnel as tunnel_module
from inspire.cli.commands.notebook import connection as connection_module
from inspire.cli.commands.notebook import notebook_commands as notebook_cmd_module
from inspire.cli.commands.notebook import remote_exec as remote_exec_module
from inspire.cli.commands.notebook import remote_shell as remote_shell_module
from inspire.cli.commands.notebook import notebook_ssh_flow as ssh_flow_module
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
)
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module


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


def make_test_config(tmp_path: Path, include_compute_groups: bool = False) -> config_module.Config:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
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

    def _fake_request_json(session, method, url, **kwargs):  # noqa: ANN001
        calls.append((method, url))
        return {"data": {"id": "live-user"}}

    monkeypatch.setattr(_NBL_MOD.web_session_module, "request_json", _fake_request_json)

    session = _FakeSession()
    assert _NBL_MOD._try_get_current_user_ids(session, base_url="https://example.invalid") == [
        "live-user"
    ]
    assert session.user_detail == {"id": "live-user"}
    assert getattr(session, "saved", False) is True
    assert calls == [("GET", "https://example.invalid/api/v1/user/detail")]


def test_current_user_id_failure_records_live_user_detail_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        user_detail = {"id": "cached-user"}

    def _fake_request_json(session, method, url, **kwargs):  # noqa: ANN001
        raise RuntimeError("browser runtime missing")

    monkeypatch.setattr(_NBL_MOD.web_session_module, "request_json", _fake_request_json)

    session = _FakeSession()
    assert (
        _NBL_MOD._try_get_current_user_ids(
            session,
            base_url="https://example.invalid",
        )
        == []
    )
    message = _NBL_MOD._current_user_lookup_failure_message(session)
    assert "/api/v1/user/detail" in message
    assert "browser runtime missing" in message
    assert "inspire account login" in message
    assert "inspire update --cli-only" in message


def test_current_user_detail_uses_live_user_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        user_detail = {"id": "cached-user"}

        def save(self) -> None:
            self.saved = True

    def _fake_request_json(session, method, url, **kwargs):  # noqa: ANN001
        return {"data": {"id": "live-user", "name": "Live"}}

    monkeypatch.setattr(_NBL_MOD.web_session_module, "request_json", _fake_request_json)

    session = _FakeSession()
    assert _NBL_MOD._get_current_user_detail(
        session,
        base_url="https://example.invalid",
    ) == {"id": "live-user", "name": "Live"}
    assert session.user_detail == {"id": "live-user", "name": "Live"}
    assert getattr(session, "saved", False) is True


def test_resolve_notebook_id_propagates_listing_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    config = make_test_config(tmp_path)

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
            config=config,
            base_url="https://example.invalid",
            identifier="any-name",
            json_output=False,
        )
    # Single call — no silent retry burning a 12s wall on a real failure.
    assert call_count["n"] == 1


def test_resolve_notebook_id_retries_until_eventual_consistency_settles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty results retry; the new notebook appearing later wins."""
    from inspire.cli.context import Context

    monkeypatch.setattr(_NBL_MOD, "_resolve_notebook_id", _REAL_RESOLVE_NOTEBOOK_ID)

    config = make_test_config(tmp_path)

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
        config=config,
        base_url="https://example.invalid",
        identifier="fresh-name",
        json_output=False,
    )
    assert notebook_id == "abcd1234-5678-90ab-cdef-1234567890ab"
    assert ws_id == "ws-77777777-7777-7777-7777-777777777777"
    assert len(call_log) >= 2  # at least one retry past the initial empty result


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
                    "quota": {"cpu_count": 20},
                }
            ],
            "ws-b": [],
        }

    def _single_lister(*args: Any, **kwargs: Any) -> list[dict]:
        raise AssertionError("notebook list --workspace all should not query workspaces serially")

    monkeypatch.setattr(
        notebook_cmd_module,
        "_list_notebooks_for_workspaces",
        _multi_lister,
        raising=False,
    )
    monkeypatch.setattr(notebook_cmd_module, "_list_notebooks_for_workspace", _single_lister)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "list", "--workspace", "all", "-s", "RUNNING"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"]["items"][0]["name"] == "running-notebook"
    assert captured["workspace_ids"] == ["ws-a", "ws-b"]
    assert captured["user_ids"] == ["user-1"]
    assert captured["status"] == ["RUNNING"]


def test_notebook_list_fetches_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[int] = []

    def _fake_request_json(session, method, url, *, body, timeout):  # noqa: ANN001, ARG001
        assert method == "POST"
        assert url == "https://example.invalid/api/v1/notebook/list"
        assert timeout == 30
        page = int(body["page"])
        pages.append(page)
        if page == 1:
            return {
                "code": 0,
                "data": {
                    "total": 3,
                    "list": [{"name": "n3"}, {"name": "n2"}],
                },
            }
        if page == 2:
            return {"code": 0, "data": {"total": 3, "list": [{"name": "n1"}]}}
        raise AssertionError(f"unexpected page: {page}")

    monkeypatch.setattr(_NBL_MOD.web_session_module, "request_json", _fake_request_json)

    rows = _NBL_MOD._list_notebooks_for_workspace(
        SimpleNamespace(),
        base_url="https://example.invalid",
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
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
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
    result = runner.invoke(
        cli_main,
        ["notebook", "start", "ring-8h100-test", "--workspace", "a"],
    )

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
        path_aliases={"me": str(tmp_path / "logs")},
        log_cache_dir=str(tmp_path / "log_cache"),
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
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
        log_cache_dir=str(tmp_path / "log_cache"),
        notebook_post_start="echo from config",
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
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


def test_run_notebook_ssh_validates_dropbear_setup_script(
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
        lambda notebook_id, session=None: {
            "name": "test-nb",
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}},
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
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    fake_tunnel_config = FakeTunnelConfig()
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: False)

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

    assert exc.value.code != EXIT_CONFIG_ERROR


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
    assert "Notebook/account mismatch" in captured["message"]


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

    monkeypatch.setattr(ssh_flow_module.subprocess, "call", lambda args: 0)

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

    monkeypatch.setattr(ssh_flow_module.subprocess, "call", lambda args: 0)

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
    monkeypatch.setattr(ssh_flow_module.subprocess, "call", lambda args: next(ssh_rc))

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


def test_run_notebook_ssh_command_uses_non_interactive_executor(
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

    fake_tunnel_config = FakeTunnelConfig()
    fake_tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="nb-notebook",
            proxy_url="wss://proxy.example/notebook/",
            notebook_id="notebook-12345678",
        )
    )
    streamed: dict[str, object] = {}

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
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: False)
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )
    monkeypatch.setattr(
        tunnel_module,
        "run_ssh_command_streaming",
        lambda command, bridge_name=None, config=None, timeout=None, output_callback=None, pass_stdin=False: (
            streamed.update(
                {
                    "command": command,
                    "bridge_name": bridge_name,
                    "config": config,
                    "timeout": timeout,
                    "pass_stdin": pass_stdin,
                }
            )
            or 0
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    probe: dict[str, object] = {}

    def fake_probe_run(
        args: list[str],
        *,
        stdin=None,
        capture_output: bool,
        timeout: int,
        text: bool,
    ) -> subprocess.CompletedProcess:
        probe.update(
            {
                "args": args,
                "stdin": stdin,
                "capture_output": capture_output,
                "timeout": timeout,
                "text": text,
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(ssh_flow_module.subprocess, "run", fake_probe_run)

    ssh_flow_module.run_notebook_ssh(
        Context(),
        notebook_id="nb-name",
        workspace="Test Workspace",
        wait=True,
        pubkey=None,
        port=31337,
        ssh_port=22222,
        command="git status",
        debug_playwright=False,
        setup_timeout=60,
    )

    assert streamed["command"] == "git status"
    assert streamed["bridge_name"] == "nb-notebook"
    assert streamed["config"] is fake_tunnel_config
    assert streamed["timeout"] == 300
    assert streamed["pass_stdin"] is True
    assert probe["stdin"] is subprocess.DEVNULL


def test_run_notebook_ssh_name_uses_cached_bridge_metadata(
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

    fake_tunnel_config = FakeTunnelConfig()
    fake_tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="nb-notebook",
            proxy_url="wss://proxy.example/notebook/",
            notebook_id="notebook-12345678",
            notebook_name="container-config",
        )
    )
    streamed: dict[str, object] = {}

    monkeypatch.setattr(ssh_flow_module, "require_web_session", lambda ctx, hint: FakeSession())
    monkeypatch.setattr(ssh_flow_module, "load_config", lambda ctx: make_test_config(tmp_path))
    monkeypatch.setattr(
        ssh_flow_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not resolve via web")),
    )
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )
    monkeypatch.setattr(
        tunnel_module,
        "run_ssh_command_streaming",
        lambda command, bridge_name=None, config=None, timeout=None, output_callback=None, pass_stdin=False: (
            streamed.update(
                {
                    "command": command,
                    "bridge_name": bridge_name,
                    "config": config,
                    "timeout": timeout,
                    "pass_stdin": pass_stdin,
                }
            )
            or 0
        ),
    )
    monkeypatch.setattr(
        ssh_flow_module.subprocess,
        "run",
        lambda args, stdin=None, capture_output=True, timeout=10, text=True: (
            subprocess.CompletedProcess(
                args,
                0,
                stdout="ok\n",
                stderr="",
            )
        ),
    )

    ssh_flow_module.run_notebook_ssh(
        Context(),
        notebook_id="container-config",
        workspace="Test Workspace",
        wait=True,
        pubkey=None,
        port=31337,
        ssh_port=22222,
        command="echo fast-name",
        debug_playwright=False,
        setup_timeout=60,
    )

    assert streamed["command"] == "echo fast-name"
    assert streamed["bridge_name"] == "nb-notebook"
    assert streamed["config"] is fake_tunnel_config
    assert streamed["timeout"] == 300
    assert streamed["pass_stdin"] is True


def test_run_notebook_ssh_command_timeout_is_reported(
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

    fake_tunnel_config = FakeTunnelConfig()
    fake_tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="nb-notebook",
            proxy_url="wss://proxy.example/notebook/",
            notebook_id="notebook-12345678",
        )
    )
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
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: False)
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )
    monkeypatch.setattr(
        tunnel_module,
        "run_ssh_command_streaming",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ssh", timeout=5)
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        ssh_flow_module.subprocess,
        "run",
        lambda args, stdin=None, capture_output=True, timeout=10, text=True: (
            subprocess.CompletedProcess(
                args,
                0,
                stdout="ok\n",
                stderr="",
            )
        ),
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
            command="git pull",
            command_timeout=5,
            debug_playwright=False,
            setup_timeout=60,
        )

    assert exc.value.code == EXIT_TIMEOUT
    assert captured["type"] == "Timeout"
    assert "timed out after 5s" in captured["message"]
    assert "--command-timeout" in captured["hint"]


def test_run_notebook_ssh_command_failure_reports_exit_code_and_grep_hint(
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

    fake_tunnel_config = FakeTunnelConfig()
    fake_tunnel_config.add_bridge(
        tunnel_module.BridgeProfile(
            name="nb-notebook",
            proxy_url="wss://proxy.example/notebook/",
            notebook_id="notebook-12345678",
        )
    )
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
    monkeypatch.setattr(
        tunnel_module, "load_tunnel_config", lambda account=None: fake_tunnel_config
    )
    monkeypatch.setattr(tunnel_module, "has_internet_for_gpu_type", lambda gpu_type: False)
    monkeypatch.setattr(
        tunnel_module,
        "get_ssh_command_args",
        lambda bridge_name, config, remote_command=None: ["ssh", "root@localhost"],
    )
    monkeypatch.setattr(
        tunnel_module,
        "run_ssh_command_streaming",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr(
        browser_api_module,
        "setup_notebook_rtunnel",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(
        ssh_flow_module.subprocess,
        "run",
        lambda args, stdin=None, capture_output=True, timeout=10, text=True: (
            subprocess.CompletedProcess(
                args,
                0,
                stdout="ok\n",
                stderr="",
            )
        ),
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
            command="grep -c missing tasks/*/data.json",
            debug_playwright=False,
            setup_timeout=60,
        )

    assert exc.value.code == 1
    assert captured["type"] == "CommandFailed"
    assert "exit code 1" in captured["message"]
    assert "grep returns exit code 1" in captured["hint"]


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
    assert "Proxy readiness report:" in captured["hint"]


def test_notebook_connection_status_json_exposes_proxy_url_without_notebook_id(
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
    bridge = payload["data"]["bridge"]
    assert bridge["proxy_url"] == "https://proxy.example/proxy/31337/"
    assert bridge["rtunnel_port"] == 31337
    assert "notebook_id" not in bridge


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
    config_path = tmp_path / ".inspire" / "accounts" / "alice" / "config.toml"
    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "[path_aliases]" in content
    assert 'me = "/inspire/ssd/project/topic/alice/"' in content

    list_result = runner.invoke(cli_main, ["notebook", "path", "list"])
    assert list_result.exit_code == EXIT_SUCCESS
    assert "Project path aliases" in list_result.output
    assert "me" in list_result.output
    assert "/inspire/ssd/project/topic/alice/" in list_result.output

    show_result = runner.invoke(cli_main, ["notebook", "path", "show", "me"])
    assert show_result.exit_code == EXIT_SUCCESS
    assert "Path alias: me" in show_result.output
    assert "/inspire/ssd/project/topic/alice/" in show_result.output

    delete_result = runner.invoke(cli_main, ["notebook", "path", "delete", "me", "--yes"])
    assert delete_result.exit_code == EXIT_SUCCESS
    assert "Deleted path alias: me" in delete_result.output
    assert "[path_aliases]" not in config_path.read_text(encoding="utf-8")


def test_notebook_help_uses_path_group_instead_of_set_path() -> None:
    runner = CliRunner()

    notebook_help = runner.invoke(cli_main, ["notebook", "--help"])
    assert notebook_help.exit_code == EXIT_SUCCESS
    assert "path" in notebook_help.output
    assert "set-path" not in notebook_help.output

    path_help = runner.invoke(cli_main, ["notebook", "path", "--help"])
    assert path_help.exit_code == EXIT_SUCCESS
    assert "Manage project-level remote path aliases." in path_help.output
    assert "not bound to any one notebook instance" in path_help.output


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

    def fake_get_ssh_command_args(bridge_name, config, remote_command=None):  # type: ignore[no-untyped-def]
        captured["bridge_name"] = bridge_name
        captured["remote_command"] = remote_command
        return ["ssh", "root@localhost"]

    monkeypatch.setattr(remote_shell_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(remote_shell_module.subprocess, "call", lambda args: 0)

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

    def fake_get_ssh_command_args(bridge_name, config, remote_command=None):  # type: ignore[no-untyped-def]
        captured["bridge_name"] = bridge_name
        captured["remote_command"] = remote_command
        return ["ssh", "root@localhost"]

    monkeypatch.setattr(remote_shell_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(remote_shell_module.subprocess, "call", lambda args: 0)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["bridge_name"] == "gpu-main"
    assert captured["remote_command"] is None
    assert "Working directory: $HOME" in result.output
