import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
import importlib

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy
from inspire.cli.main import main as cli_main
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.cli.logging_setup import clear_debug_logging
from inspire.config import Config

# Import the submodules where the patched names actually live
exec_cmd_module = importlib.import_module("inspire.cli.commands.notebook.remote_exec")
ssh_cmd_module = importlib.import_module("inspire.cli.commands.notebook.remote_shell")


@pytest.fixture(autouse=True)
def _allow_exec_transport_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def _allow_policy(*_args: object, **_kwargs: object) -> NotebookTransportPolicy:
        return NotebookTransportPolicy(
            notebook="gpu-main",
            notebook_id="nb-public",
            public_internet=True,
            reason="test",
        )

    monkeypatch.setattr(
        exec_cmd_module,
        "preflight_notebook_transport_policy",
        _allow_policy,
        raising=False,
    )
    monkeypatch.setattr(
        ssh_cmd_module,
        "preflight_notebook_transport_policy",
        _allow_policy,
        raising=False,
    )


def make_sync_config(tmp_path: Path) -> Config:
    return Config(
        username="",
        password="",
        path_aliases={"me": str(tmp_path)},
    )


def make_tunnel_config(name: str = "gpu-main") -> TunnelConfig:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name=name,
            proxy_url="https://proxy.example.com/proxy/31337/",
        )
    )
    return tunnel_config


def test_bridge_exec_without_default_path_alias_runs_in_remote_default_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {}
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: make_tunnel_config())
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda **kwargs: True)

    def fake_run_streaming(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(exec_cmd_module, "run_ssh_command_streaming", fake_run_streaming)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "hostname"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["command"] == "hostname"
    assert 'cd "' not in captured["command"]


def test_bridge_exec_invalid_remote_env_human_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "load_tunnel_config",
        lambda: (_ for _ in ()).throw(AssertionError("should not load tunnel config")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Invalid remote_env key" in result.output


def test_bridge_exec_invalid_remote_env_json_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "load_tunnel_config",
        lambda: (_ for _ in ()).throw(AssertionError("should not load tunnel config")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Invalid remote_env key" in payload["error"]["message"]


def test_bridge_ssh_invalid_remote_env_human_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: make_tunnel_config())
    monkeypatch.setattr(
        ssh_cmd_module,
        "is_tunnel_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not check tunnel")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Invalid remote_env key" in result.output


def test_bridge_ssh_invalid_remote_env_json_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: make_tunnel_config())
    monkeypatch.setattr(
        ssh_cmd_module,
        "is_tunnel_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not check tunnel")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Invalid remote_env key" in payload["error"]["message"]


# Tests for SSH tunnel streaming functionality


def test_bridge_exec_ssh_streaming_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that SSH tunnel uses streaming for human output."""
    config = make_sync_config(tmp_path)
    streamed_lines: List[str] = []

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(
        command: str,
        bridge_name: Any = None,
        config: Any = None,
        timeout: Any = None,
        output_callback: Any = None,
    ) -> int:
        # Simulate streaming output
        lines = ["Line 1\n", "Line 2\n", "Line 3\n"]
        for line in lines:
            streamed_lines.append(line)
            if output_callback:
                output_callback(line)
        return 0

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    assert result.output.strip().endswith("OK")
    # Verify streaming function was called (output was streamed)
    assert len(streamed_lines) == 3


def test_exec_uses_jupyter_when_policy_blocks_ssh(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    config = make_sync_config(tmp_path)
    target_session = SimpleNamespace(account="secondary")
    capture_kwargs: dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            public_internet=False,
            reason="live_probe",
            session=target_session,
        ),
    )

    def fake_capture(**kwargs):  # noqa: ANN202
        capture_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, output="ok\n", completed=True, marker="m")

    monkeypatch.setattr(
        exec_cmd_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_capture,
    )
    monkeypatch.setattr(exec_cmd_module, "try_exec_via_ssh_tunnel", lambda *_a, **_k: 99)

    result = CliRunner().invoke(cli_main, ["notebook", "exec", "gpu-box", "echo ok"])

    assert result.exit_code == 0
    assert "ok" in result.output
    assert capture_kwargs["session"] is target_session


def test_exec_json_reports_jupyter_transport(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            public_internet=False,
            reason="live_probe",
        ),
    )
    monkeypatch.setattr(
        exec_cmd_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: SimpleNamespace(
            returncode=3,
            output="bad token=secret /tmp/job?trace=1\n",
            completed=True,
            marker="m",
        ),
    )

    result = CliRunner().invoke(cli_main, ["--json", "notebook", "exec", "gpu-box", "false"])

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload == {
            "success": False,
            "error": {
                "type": "CommandFailed",
                "code": 3,
                "message": "Remote command failed",
            },
        "data": {
            "returncode": 3,
            "output": "bad token=secret /tmp/job?trace=1\n",
        },
    }
    assert result.output.count("\n") == 1
    assert "returncode" not in payload["error"]
    assert "3" not in payload["error"]["message"]
    assert "method" not in payload["data"]


def test_exec_forwards_workspace_account_and_pick_to_target_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = make_sync_config(tmp_path)
    policy_calls: dict[str, Any] = {}
    target_calls: dict[str, Any] = {}
    execution_calls: dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "preflight_notebook_transport_policy",
        lambda _ctx, **kwargs: (
            policy_calls.update(kwargs)
            or NotebookTransportPolicy(
                notebook="gpu-box",
                notebook_id="nb-123",
                public_internet=True,
                reason="test",
            )
        ),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "resolve_cached_notebook_target",
        lambda _ctx, **kwargs: (
            target_calls.update(kwargs)
            or SimpleNamespace(
                account="alice",
                bridge=SimpleNamespace(name="gpu-box"),
            )
        ),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "try_exec_via_ssh_tunnel",
        lambda _ctx, **kwargs: execution_calls.update(kwargs) or EXIT_SUCCESS,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "exec",
            "gpu-box",
            "--workspace",
            "CPU资源空间",
            "--account",
            "alice",
            "--pick",
            "2",
            "echo",
            "ok",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert policy_calls["workspace"] == "CPU资源空间"
    assert policy_calls["account"] == "alice"
    assert policy_calls["pick"] == 2
    assert target_calls["workspace"] == "CPU资源空间"
    assert target_calls["account"] == "alice"
    assert target_calls["pick"] == 2
    assert execution_calls["bridge_name"] == "gpu-box"
    assert execution_calls["tunnel_account"] == "alice"


def test_bridge_exec_supports_command_after_double_dash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        captured["command"] = kwargs.get("command")
        return 0

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "--", "bash", "-s"])

    assert result.exit_code == EXIT_SUCCESS
    assert 'cd "' in captured["command"]
    assert "&& bash -s" in captured["command"]


def test_bridge_exec_stdin_streaming_passes_stdin_mode_to_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        captured["command"] = kwargs.get("command")
        captured["pass_stdin"] = kwargs.get("pass_stdin")
        return 0

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "--stdin", "--", "bash", "-s"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["pass_stdin"] is True
    assert "&& bash -s" in captured["command"]


def test_bridge_exec_auto_stdin_streaming_passes_stdin_mode_to_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "_should_auto_passthrough_stdin", lambda: True)

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        captured["pass_stdin"] = kwargs.get("pass_stdin")
        return 0

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["pass_stdin"] is True


def test_bridge_exec_ssh_json_uses_buffered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that JSON mode uses buffered output, not streaming."""
    config = make_sync_config(tmp_path)
    streaming_called = {"value": False}
    buffered_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        streaming_called["value"] = True
        return 0

    class FakeCompletedProcess:
        returncode = 0
        stdout = "buffered output"
        stderr = ""

    def fake_run_ssh_command(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        buffered_called["value"] = True
        return FakeCompletedProcess()

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "run_ssh_command", fake_run_ssh_command)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "gpu-main", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    # Buffered should be used, not streaming
    assert buffered_called["value"] is True
    assert streaming_called["value"] is False
    # Verify JSON output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "method" not in payload["data"]
    assert payload["data"]["output"] == "buffered output"


def test_bridge_exec_ssh_json_stdin_uses_buffered_with_pass_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    class FakeCompletedProcess:
        returncode = 0
        stdout = "buffered output"
        stderr = ""

    def fake_run_ssh_command(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        captured["pass_stdin"] = kwargs.get("pass_stdin")
        captured["command"] = kwargs.get("command")
        return FakeCompletedProcess()

    monkeypatch.setattr(exec_cmd_module, "run_ssh_command", fake_run_ssh_command)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "exec", "gpu-main", "--stdin", "--", "bash", "-s"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["pass_stdin"] is True
    assert "&& bash -s" in captured["command"]
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert "method" not in payload["data"]
    assert payload["data"]["output"] == "buffered output"


def test_bridge_exec_ssh_streaming_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles timeout correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "sleep 100", "--timeout", "5"])

    assert result.exit_code == EXIT_TIMEOUT
    assert "timed out" in result.output.lower()


def test_bridge_exec_ssh_streaming_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles command failure correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        return 1  # Non-zero exit code

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", make_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "false"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Command failed with exit code 1" in result.output
    assert result.output.count("Command failed with exit code 1") == 1


def test_bridge_exec_does_not_fallback_after_ssh_execution_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = make_tunnel_config()
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("stream broke")

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "SSH execution failed: stream broke" in result.output


def test_bridge_exec_errors_when_bridge_configured_but_not_responding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = make_tunnel_config(name="ring8h100")

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "ring8h100", "echo test"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "SSH tunnel not available" in result.output
    assert "ring8h100" in result.output


def test_bridge_exec_json_errors_when_bridge_configured_but_not_responding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = make_tunnel_config(name="ring8h100")

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "ring8h100", "echo test"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TunnelError"


def test_bridge_exec_fails_fast_when_notebook_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(
        exec_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {"notebook_id": notebook_id, "status": "STOPPED"},
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "\'gpu-main\' is STOPPED" in result.output
    assert "inspire notebook start gpu-main" in result.output
    assert "inspire notebook status gpu-main" in result.output
    assert calls["rebuild"] == 0


def test_bridge_exec_fails_fast_when_notebook_is_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(
        exec_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {"notebook_id": notebook_id, "status": "PENDING"},
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "\'gpu-main\' is PENDING" in result.output
    assert "inspire notebook start gpu-main" in result.output
    assert "inspire notebook status gpu-main" in result.output
    assert calls["rebuild"] == 0


def test_bridge_exec_json_fails_fast_when_notebook_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(
        exec_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {"notebook_id": notebook_id, "status": "STOPPED"},
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "exec", "gpu-main", "echo hi"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TunnelError"
    assert "\'gpu-main\' is STOPPED" in payload["error"]["message"]
    assert "inspire notebook status gpu-main" in payload["error"]["hint"]
    assert calls["rebuild"] == 0


def test_bridge_exec_errors_when_no_bridge_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_load_tunnel_config() -> TunnelConfig:
        return TunnelConfig()

    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", fake_load_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "anything", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No cached notebook connection for 'anything'" in result.output


def test_bridge_exec_passes_requested_bridge_to_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        captured["available_bridge"] = kwargs.get("bridge_name")
        return True

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        captured["stream_bridge"] = kwargs.get("bridge_name")
        return 0

    tunnel_config = make_tunnel_config()

    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["available_bridge"] == "gpu-main"
    assert captured["stream_bridge"] == "gpu-main"


def test_bridge_exec_errors_when_requested_bridge_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(name="other-bridge", proxy_url="https://proxy.example.com")
    )

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "missing", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No cached notebook connection for 'missing'" in result.output


def test_bridge_exec_rebuilds_notebook_tunnel_before_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"availability": 0, "rebuild": 0, "stream": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )

    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        return calls["availability"] > 1

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        calls["stream"] += 1
        return 0

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls["rebuild"] == 1
    assert calls["stream"] == 1
    assert "rebuilding" not in result.output
    assert "attempt" not in result.output


def test_bridge_exec_debug_logs_rebuild_without_expanding_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"availability": 0}
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-12345678",
        )
    )
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        return calls["availability"] > 1

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "run_ssh_command_streaming", lambda **_kwargs: 0)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(
        exec_cmd_module,
        "rebuild_notebook_bridge_profile",
        lambda *args, **kwargs: tunnel_config.bridges["gpu-main"],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--debug", "notebook", "exec", "gpu-main", "echo hi"],
    )
    clear_debug_logging()

    assert result.exit_code == EXIT_SUCCESS
    assert result.output == "OK\n"
    for noise in (
        "Tunnel unavailable",
        "Using SSH tunnel",
        "Notebook:",
        "Command:",
        "Working dir:",
        "Command Output",
    ):
        assert noise not in result.output

    [log_path] = list(log_dir.glob("inspire-debug-*.log"))
    debug_log = log_path.read_text(encoding="utf-8")
    assert "Notebook SSH tunnel rebuild scheduled" in debug_log
    assert "attempt=1/2" in debug_log
    assert "notebook-12345678" not in debug_log


def test_bridge_exec_cross_account_rebuild_uses_target_account_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, Any] = {"availability": 0, "rebuild": 0, "stream": 0, "accounts": []}

    tunnel_config = TunnelConfig(account="alice")
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    session = object()

    def fake_load_tunnel_config(account=None):  # type: ignore[no-untyped-def]
        calls.setdefault("load_accounts", []).append(account)
        return tunnel_config

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        return calls["availability"] > 1

    def fake_require_web_session(ctx, hint, account=None):  # type: ignore[no-untyped-def]
        del ctx, hint
        calls["accounts"].append(account)
        return session

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        calls["rebuild_session"] = kwargs.get("session")
        calls["rebuild_config"] = kwargs.get("tunnel_config")
        return tunnel_config.bridges["gpu-main"]

    def fake_stream(*args: Any, **kwargs: Any) -> int:
        calls["stream"] += 1
        return 0

    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", fake_load_tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", fake_require_web_session)
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)
    monkeypatch.setattr(exec_cmd_module.browser_api_module, "get_notebook_detail", lambda **_: {"status": "RUNNING"})

    exit_code = exec_cmd_module.try_exec_via_ssh_tunnel(
        Context(),
        command="echo hi",
        bridge_name="gpu-main",
        tunnel_account="alice",
        stdin_mode=False,
        config=config,
        remote_cwd=None,
        env_exports="",
        timeout_s=30,
        is_tunnel_available_fn=fake_is_tunnel_available,
        run_ssh_command_fn=lambda **_: None,
        run_ssh_command_streaming_fn=fake_stream,
    )

    assert exit_code == EXIT_SUCCESS
    assert calls["accounts"] == ["alice"]
    assert calls["rebuild"] == 1
    assert calls["rebuild_session"] is session
    assert calls["rebuild_config"] is tunnel_config
    assert "alice" in calls["load_accounts"]
    assert calls["stream"] == 1


def test_bridge_exec_reconnects_after_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"availability": 0, "rebuild": 0, "stream": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        # 1: initial preflight before first command
        # 2: post-failure probe after SSH exit 255 (simulate dropped tunnel)
        # 3+: preflight checks after rebuild
        if calls["availability"] == 2:
            return False
        return True

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)

    stream_exit_codes = iter([255, 0])

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        calls["stream"] += 1
        return next(stream_exit_codes)

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls["rebuild"] == 1
    assert calls["stream"] == 2
    assert "rebuilding" not in result.output
    assert "attempt" not in result.output


def test_bridge_exec_non_notebook_bridge_exit_255_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(exec_cmd_module, "run_ssh_command_streaming", lambda *args, **kwargs: 255)

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Command failed with exit code 255" in result.output
    assert "cannot be rebuilt automatically" not in result.output
    assert calls["rebuild"] == 0


def test_bridge_exec_json_exit_255_is_not_retried_when_tunnel_is_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    class FakeCompletedProcess:
        returncode = 255
        stdout = ""
        stderr = "remote command failed"

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command", lambda *args, **kwargs: FakeCompletedProcess()
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--json", "notebook", "exec", "gpu-main", "echo hi"]
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "CommandFailed"
    assert "exit code 255" in payload["error"]["message"]
    assert calls["rebuild"] == 0


def test_bridge_exec_exit_255_probe_exception_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"availability": 0, "rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        if calls["availability"] == 1:
            return True
        raise RuntimeError("probe failed")

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "run_ssh_command_streaming", lambda *args, **kwargs: 255)
    monkeypatch.setattr(exec_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Command failed with exit code 255" in result.output
    assert calls["rebuild"] == 0


def test_bridge_exec_rebuild_failure_errors_after_retry_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 1
    config.tunnel_retry_pause = 0.0

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(exec_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(exec_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(
        exec_cmd_module,
        "rebuild_notebook_bridge_profile",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "gpu-main", "echo hi"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Automatic tunnel rebuild failed" in result.output


def test_bridge_exec_json_errors_after_reconnect_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.tunnel_retries = 0
    config.tunnel_retry_pause = 0.0

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "exec", "gpu-main", "echo hi"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TunnelError"
    assert "Auto-rebuild retries exhausted" in payload["error"]["hint"]


def test_bridge_ssh_uses_requested_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        captured["available_bridge"] = kwargs.get("bridge_name")
        return True

    def fake_get_ssh_command_args(*args: Any, **kwargs: Any) -> List[str]:
        captured["ssh_bridge"] = kwargs.get("bridge_name")
        return ["ssh", "root@localhost"]

    def fake_pty(args: List[str]) -> int:
        captured["ssh_args"] = args
        return 0

    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(ssh_cmd_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(ssh_cmd_module, "run_scrubbed_pty", fake_pty)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == 0
    assert captured["available_bridge"] == "gpu-main"
    assert captured["ssh_bridge"] == "gpu-main"
    assert captured["ssh_args"][0] == "ssh"


def test_bridge_ssh_rebuilds_notebook_tunnel_before_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, Any] = {"availability": 0, "rebuild": 0, "ssh": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-12345678",
        )
    )
    log_dir = tmp_path / "debug-shell-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        return calls["availability"] > 1

    def fake_get_ssh_command_args(*args: Any, **kwargs: Any) -> List[str]:
        return ["ssh", "root@localhost"]

    def fake_pty(args: List[str]) -> int:  # noqa: ARG001
        calls["ssh"] += 1
        return 0

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(ssh_cmd_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(ssh_cmd_module, "run_scrubbed_pty", fake_pty)
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(ssh_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--debug", "notebook", "shell", "gpu-main"])
    clear_debug_logging()

    assert result.exit_code == 0
    assert result.output == ""
    assert calls["rebuild"] == 1
    assert calls["ssh"] == 1
    [log_path] = list(log_dir.glob("inspire-debug-*.log"))
    debug_log = log_path.read_text(encoding="utf-8")
    assert "Notebook shell tunnel rebuild scheduled" in debug_log
    assert "notebook-12345678" not in debug_log


def test_bridge_ssh_fails_fast_when_notebook_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(
        ssh_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {"notebook_id": notebook_id, "status": "STOPPED"},
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "\'gpu-main\' is STOPPED" in result.output
    assert "inspire notebook start gpu-main" in result.output
    assert "inspire notebook status gpu-main" in result.output
    assert calls["rebuild"] == 0


def test_bridge_ssh_fails_fast_when_notebook_is_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(
        ssh_cmd_module.browser_api_module,
        "get_notebook_detail",
        lambda notebook_id, session=None: {"notebook_id": notebook_id, "status": "PENDING"},
    )

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "\'gpu-main\' is PENDING" in result.output
    assert "inspire notebook start gpu-main" in result.output
    assert "inspire notebook status gpu-main" in result.output
    assert calls["rebuild"] == 0


def test_bridge_ssh_reconnects_after_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, Any] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="gpu-main",
            proxy_url="https://proxy.example.com/proxy/31337/",
            notebook_id="notebook-1",
        )
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        ssh_cmd_module, "get_ssh_command_args", lambda *args, **kwargs: ["ssh", "root@localhost"]
    )

    ssh_return_codes = iter([255, 0])
    monkeypatch.setattr(ssh_cmd_module, "run_scrubbed_pty", lambda args: next(ssh_return_codes))
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(ssh_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == 0
    assert calls["rebuild"] == 1


def test_bridge_ssh_unavailable_non_notebook_bridge_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        ssh_cmd_module,
        "rebuild_notebook_bridge_profile",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not rebuild")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "cannot be rebuilt automatically" in result.output


def test_bridge_ssh_missing_bridge_reports_bridge_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.path_aliases = {"me": str(tmp_path / "project")}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(name="other-bridge", proxy_url="https://proxy.example.com")
    )

    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fail_if_checked(*args: Any, **kwargs: Any) -> bool:  # noqa: ARG001
        raise AssertionError("should not be called")

    monkeypatch.setattr(
        ssh_cmd_module,
        "is_tunnel_available",
        fail_if_checked,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "missing"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No cached notebook connection for 'missing'" in result.output
