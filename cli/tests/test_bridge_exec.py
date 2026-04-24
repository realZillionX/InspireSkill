import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
import importlib

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.main import main as cli_main
from inspire.cli.context import EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.config import Config

# Import the submodules where the patched names actually live
exec_cmd_module = importlib.import_module("inspire.cli.commands.notebook.remote_exec")
ssh_cmd_module = importlib.import_module("inspire.cli.commands.notebook.remote_shell")


def make_sync_config(tmp_path: Path) -> Config:
    return Config(
        username="",
        password="",
        target_dir=str(tmp_path),
        github_repo="owner/repo",
        github_token="ghp_test",
        github_server="https://github.com",
        default_remote="origin",
        remote_timeout=5,
        bridge_action_timeout=5,
        bridge_action_denylist=[],
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


def test_bridge_exec_invalid_remote_env_human_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "load_tunnel_config",
        lambda: (_ for _ in ()).throw(AssertionError("should not load tunnel config")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        exec_cmd_module,
        "load_tunnel_config",
        lambda: (_ for _ in ()).throw(AssertionError("should not load tunnel config")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "echo hi"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Invalid remote_env key" in payload["error"]["message"]


def test_bridge_exec_invalid_remote_env_workflow_branch_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.remote_env = {"NOT-VALID": "value"}
    called: Dict[str, bool] = {"workflow": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        called["workflow"] = True

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "exec", "echo hi", "--artifact-path", ".cache"],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Invalid remote_env key" in result.output
    assert called["workflow"] is False


def test_bridge_ssh_invalid_remote_env_human_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: make_tunnel_config())
    monkeypatch.setattr(
        ssh_cmd_module,
        "is_tunnel_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not check tunnel")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Invalid remote_env key" in result.output


def test_bridge_ssh_invalid_remote_env_json_returns_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.remote_env = {"NOT-VALID": "value"}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(ssh_cmd_module, "load_tunnel_config", lambda: make_tunnel_config())
    monkeypatch.setattr(
        ssh_cmd_module,
        "is_tunnel_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not check tunnel")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ConfigError"
    assert "Invalid remote_env key" in payload["error"]["message"]


def test_bridge_exec_triggers_and_no_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)

    called: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(
        config: Config,
        raw_command: str,
        artifact_paths: List[str],
        request_id: str,
        denylist: Optional[List[str]] = None,
    ) -> None:
        called["trigger"] = {
            "raw_command": raw_command,
            "artifact_paths": artifact_paths,
            "request_id": request_id,
            "denylist": denylist,
        }

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "exec", "echo hi", "--no-wait", "--artifact-path", ".cache"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "trigger" in called
    assert called["trigger"]["raw_command"] == "echo hi"


def test_bridge_exec_uses_env_denylist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)
    config.bridge_action_denylist = ["rm -rf /"]

    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(
        config: Config,
        raw_command: str,
        artifact_paths: List[str],
        request_id: str,
        denylist: Optional[List[str]] = None,
    ) -> None:
        captured["denylist"] = denylist

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "exec", "echo hi", "--no-wait", "--artifact-path", ".cache"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["denylist"] == ["rm -rf /"]


def test_bridge_exec_reports_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "failure", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return None

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(exec_cmd_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(exec_cmd_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--artifact-path", ".cache"])

    assert result.exit_code == EXIT_GENERAL_ERROR


def test_bridge_exec_displays_output_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that command output is displayed to the user."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return "Hello from Bridge!\nCommand completed."

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(exec_cmd_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(exec_cmd_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--artifact-path", ".cache"])

    assert result.exit_code == EXIT_SUCCESS
    assert "Hello from Bridge!" in result.output
    assert "Command completed." in result.output
    assert result.output.strip().endswith("OK")


def test_bridge_exec_json_includes_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that JSON output includes the command output."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_wait(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "completed", "conclusion": "success", "html_url": "http://example.com"}

    def fake_fetch_log(*args: Any, **kwargs: Any) -> Optional[str]:
        return "Test output"

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)
    monkeypatch.setattr(exec_cmd_module, "wait_for_bridge_action_completion", fake_wait)
    monkeypatch.setattr(exec_cmd_module, "fetch_bridge_output_log", fake_fetch_log)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "exec", "echo hi", "--artifact-path", ".cache"],
    )

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "success"
    assert payload["data"]["output"] == "Test output"


# Tests for SSH tunnel streaming functionality


def test_bridge_exec_ssh_streaming_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that SSH tunnel uses streaming for human output."""
    config = make_sync_config(tmp_path)
    streamed_lines: List[str] = []

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    assert result.output.strip().endswith("OK")
    # Verify streaming function was called (output was streamed)
    assert len(streamed_lines) == 3


def test_bridge_exec_supports_command_after_double_dash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "--", "bash", "-s"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "--stdin", "--", "bash", "-s"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "echo test"])

    assert result.exit_code == EXIT_SUCCESS
    # Buffered should be used, not streaming
    assert buffered_called["value"] is True
    assert streaming_called["value"] is False
    # Verify JSON output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["method"] == "ssh_tunnel"
    assert payload["data"]["output"] == "buffered output"


def test_bridge_exec_ssh_json_stdin_uses_buffered_with_pass_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
        ["--json", "notebook", "exec", "--stdin", "--", "bash", "-s"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["pass_stdin"] is True
    assert "&& bash -s" in captured["command"]
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["method"] == "ssh_tunnel"
    assert payload["data"]["output"] == "buffered output"


def test_bridge_exec_stdin_rejects_artifact_workflow_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    called: Dict[str, Any] = {"workflow": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        called["workflow"] = True

    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "exec", "--stdin", "echo hi", "--artifact-path", ".cache"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "cannot be combined" in result.output
    assert called["workflow"] is False


def test_bridge_exec_ssh_streaming_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles timeout correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "sleep 100", "--timeout", "5"])

    assert result.exit_code == EXIT_TIMEOUT
    assert "timed out" in result.output.lower()


def test_bridge_exec_ssh_streaming_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that streaming mode handles command failure correctly."""
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "false"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Command failed with exit code 1" in result.output
    assert result.output.count("Command failed with exit code 1") == 1


def test_bridge_exec_does_not_fallback_after_ssh_execution_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    called: Dict[str, bool] = {"workflow": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    tunnel_config = make_tunnel_config()
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", lambda *args, **kwargs: True)

    def fake_run_ssh_command_streaming(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("stream broke")

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        called["workflow"] = True

    monkeypatch.setattr(
        exec_cmd_module, "run_ssh_command_streaming", fake_run_ssh_command_streaming
    )
    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "SSH execution failed: stream broke" in result.output
    assert called["workflow"] is False


def test_bridge_exec_errors_when_bridge_configured_but_not_responding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = make_tunnel_config(name="ring8h100")

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo test"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = make_tunnel_config(name="ring8h100")

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "exec", "echo test"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "notebook 'notebook-1' is STOPPED" in result.output
    assert "inspire notebook start notebook-1" in result.output
    assert "inspire notebook status notebook-1" in result.output
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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "notebook 'notebook-1' is PENDING" in result.output
    assert "inspire notebook start notebook-1" in result.output
    assert "inspire notebook status notebook-1" in result.output
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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
        ["--json", "notebook", "exec", "echo hi", "--bridge", "gpu-main"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TunnelError"
    assert "notebook 'notebook-1' is STOPPED" in payload["error"]["message"]
    assert "inspire notebook status notebook-1" in payload["error"]["hint"]
    assert calls["rebuild"] == 0


def test_bridge_exec_errors_when_no_bridge_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_load_tunnel_config() -> TunnelConfig:
        return TunnelConfig()

    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", fake_load_tunnel_config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--no-wait"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No bridge configured for SSH execution" in result.output


def test_bridge_exec_passes_requested_bridge_to_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["available_bridge"] == "gpu-main"
    assert captured["stream_bridge"] == "gpu-main"


def test_bridge_exec_errors_when_requested_bridge_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    workflow_called = {"value": False}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
    )

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        return False

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(name="other-bridge", proxy_url="https://proxy.example.com")
    )

    def fake_trigger(*args: Any, **kwargs: Any) -> None:
        workflow_called["value"] = True

    monkeypatch.setattr(exec_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(exec_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(exec_cmd_module, "trigger_bridge_action_workflow", fake_trigger)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "missing"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Bridge 'missing' not found" in result.output
    assert workflow_called["value"] is False


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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls["rebuild"] == 1
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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls["rebuild"] == 1
    assert calls["stream"] == 2


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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
        cli_main, ["--json", "notebook", "exec", "echo hi", "--bridge", "gpu-main"]
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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "exec", "echo hi", "--bridge", "gpu-main"])

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
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
        ["--json", "notebook", "exec", "echo hi", "--bridge", "gpu-main"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TunnelError"
    assert "Auto-rebuild retries exhausted" in payload["error"]["hint"]


def test_bridge_ssh_uses_requested_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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

    def fake_call(args: List[str]) -> int:
        captured["ssh_args"] = args
        return 0

    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(ssh_cmd_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(ssh_cmd_module.subprocess, "call", fake_call)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == 0
    assert captured["available_bridge"] == "gpu-main"
    assert captured["ssh_bridge"] == "gpu-main"
    assert captured["ssh_args"][0] == "ssh"


def test_bridge_ssh_rebuilds_notebook_tunnel_before_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, Any] = {"availability": 0, "rebuild": 0, "ssh": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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

    def fake_is_tunnel_available(*args: Any, **kwargs: Any) -> bool:
        calls["availability"] += 1
        return calls["availability"] > 1

    def fake_get_ssh_command_args(*args: Any, **kwargs: Any) -> List[str]:
        return ["ssh", "root@localhost"]

    def fake_call(args: List[str]) -> int:  # noqa: ARG001
        calls["ssh"] += 1
        return 0

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(ssh_cmd_module, "get_ssh_command_args", fake_get_ssh_command_args)
    monkeypatch.setattr(ssh_cmd_module.subprocess, "call", fake_call)
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(ssh_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")
    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == 0
    assert calls["rebuild"] == 1
    assert calls["ssh"] == 1


def test_bridge_ssh_fails_fast_when_notebook_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "notebook 'notebook-1' is STOPPED" in result.output
    assert "inspire notebook start notebook-1" in result.output
    assert "inspire notebook status notebook-1" in result.output
    assert calls["rebuild"] == 0


def test_bridge_ssh_fails_fast_when_notebook_is_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.tunnel_retries = 3
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, int] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "notebook 'notebook-1' is PENDING" in result.output
    assert "inspire notebook start notebook-1" in result.output
    assert "inspire notebook status notebook-1" in result.output
    assert calls["rebuild"] == 0


def test_bridge_ssh_reconnects_after_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")
    config.tunnel_retries = 2
    config.tunnel_retry_pause = 0.0
    calls: Dict[str, Any] = {"rebuild": 0}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    monkeypatch.setattr(ssh_cmd_module.subprocess, "call", lambda args: next(ssh_return_codes))
    monkeypatch.setattr(ssh_cmd_module, "require_web_session", lambda ctx, hint: object())
    monkeypatch.setattr(ssh_cmd_module, "load_ssh_public_key_material", lambda: "ssh-ed25519 AAA")

    def fake_rebuild(*args: Any, **kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        return tunnel_config.bridges["gpu-main"]

    monkeypatch.setattr(ssh_cmd_module, "rebuild_notebook_bridge_profile", fake_rebuild)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == 0
    assert calls["rebuild"] == 1


def test_bridge_ssh_unavailable_non_notebook_bridge_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "gpu-main"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "cannot be rebuilt automatically" in result.output


def test_bridge_ssh_missing_bridge_reports_bridge_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_sync_config(tmp_path)
    config.target_dir = str(tmp_path / "project")

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (config, {})),
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
    result = runner.invoke(cli_main, ["notebook", "shell", "--bridge", "missing"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Bridge 'missing' not found" in result.output
