from pathlib import Path
import subprocess
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.bridge import tunnel as tunnel_module
from inspire.cli.commands.notebook import notebook_commands as notebook_cmd_module
from inspire.cli.commands.notebook import notebook_ssh_flow as ssh_flow_module
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
)
from inspire.cli.main import main as cli_main
from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module


def make_test_config(tmp_path: Path, include_compute_groups: bool = False) -> config_module.Config:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
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


def test_notebook_create_accepts_priority_10(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_notebook_create(ctx: Context, **kwargs: Any) -> None:
        assert ctx is not None
        captured.update(kwargs)

    monkeypatch.setattr(notebook_cmd_module, "run_notebook_create", fake_run_notebook_create)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "create", "--priority", "10"])

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
    result = runner.invoke(cli_main, ["notebook", "create", "--post-start", "echo hi"])

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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        workspaces={"a": ws_cpu, "b": ws_gpu},
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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
    result = runner.invoke(cli_main, ["notebook", "start", "ring-8h100-test"])

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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        workspaces={"a": ws_cpu, "b": ws_gpu},
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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
        ["notebook", "start", "ring-8h100-test"],
        input="2\n",
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
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        workspaces={"a": ws_cpu, "b": ws_gpu},
        notebook_post_start="echo from config",
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )

    def fake_from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ):  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config, "from_files_and_env", classmethod(fake_from_files_and_env)
    )

    class FakeSession:
        workspace_id = "ws-00000000-0000-0000-0000-000000000000"
        storage_state = {}

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
    result = runner.invoke(cli_main, ["notebook", "start", "ring-8h100-test", "--no-wait"])

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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "H200"}}
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
        ssh_flow_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(
            dropbear_deb_dir="/project/dropbear",
            setup_script=None,
        ),
    )
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
            wait=True,
            pubkey=None,
            save_as=None,
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
            wait=True,
            pubkey=None,
            save_as=None,
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

    resolved_runtime = SshRuntimeConfig(
        rtunnel_download_url="https://project.example/rtunnel.tgz",
    )
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        ssh_flow_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: resolved_runtime,
    )

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
        wait=True,
        pubkey=None,
        save_as=None,
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert setup_kwargs["ssh_runtime"] is resolved_runtime


def test_run_notebook_ssh_refreshes_saved_profile_on_notebook_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        ssh_flow_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
    )

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
        wait=True,
        pubkey=None,
        save_as="shared-profile",
        port=31337,
        ssh_port=22222,
        command=None,
        debug_playwright=False,
        setup_timeout=60,
    )

    assert setup_called["value"] is True
    saved_profile = fake_tunnel_config.bridges["shared-profile"]
    assert getattr(saved_profile, "notebook_id", None) == "notebook-12345678"


def test_run_notebook_ssh_interactive_reconnects_after_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        ssh_flow_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
    )
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
        wait=True,
        pubkey=None,
        save_as=None,
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        lambda command, bridge_name=None, config=None, timeout=None, output_callback=None: (
            streamed.update(
                {
                    "command": command,
                    "bridge_name": bridge_name,
                    "config": config,
                    "timeout": timeout,
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
    monkeypatch.setattr(
        ssh_flow_module.subprocess,
        "run",
        lambda args, capture_output, timeout, text: subprocess.CompletedProcess(
            args,
            0,
            stdout="ok\n",
            stderr="",
        ),
    )

    ssh_flow_module.run_notebook_ssh(
        Context(),
        notebook_id="nb-name",
        wait=True,
        pubkey=None,
        save_as=None,
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


def test_run_notebook_ssh_name_uses_cached_bridge_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
        lambda command, bridge_name=None, config=None, timeout=None, output_callback=None: (
            streamed.update(
                {
                    "command": command,
                    "bridge_name": bridge_name,
                    "config": config,
                    "timeout": timeout,
                }
            )
            or 0
        ),
    )
    monkeypatch.setattr(
        ssh_flow_module.subprocess,
        "run",
        lambda args, capture_output, timeout, text: subprocess.CompletedProcess(
            args,
            0,
            stdout="ok\n",
            stderr="",
        ),
    )

    ssh_flow_module.run_notebook_ssh(
        Context(),
        notebook_id="container-config",
        wait=True,
        pubkey=None,
        save_as=None,
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


def test_run_notebook_ssh_command_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSession:
        workspace_id = "ws-test"
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        lambda args, capture_output, timeout, text: subprocess.CompletedProcess(
            args,
            0,
            stdout="ok\n",
            stderr="",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        ssh_flow_module.run_notebook_ssh(
            Context(),
            notebook_id="nb-name",
            wait=True,
            pubkey=None,
            save_as=None,
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        lambda args, capture_output, timeout, text: subprocess.CompletedProcess(
            args,
            0,
            stdout="ok\n",
            stderr="",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        ssh_flow_module.run_notebook_ssh(
            Context(),
            notebook_id="nb-name",
            wait=True,
            pubkey=None,
            save_as=None,
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
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "CPU"}}
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
        ssh_flow_module,
        "resolve_ssh_runtime_config",
        lambda cli_overrides=None: SshRuntimeConfig(),
    )
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
            wait=True,
            pubkey=None,
            save_as=None,
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

