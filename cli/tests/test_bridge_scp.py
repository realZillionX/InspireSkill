import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.bridge.tunnel.scp import _build_scp_base_args
from inspire.cli.main import main as cli_main
from inspire.cli.context import EXIT_GENERAL_ERROR, EXIT_SUCCESS, EXIT_TIMEOUT
from inspire.cli.logging_setup import clear_debug_logging
from inspire.config import Config
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy

import importlib

scp_cmd_module = importlib.import_module("inspire.cli.commands.notebook.remote_scp")


@pytest.fixture(autouse=True)
def _allow_scp_transport_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scp_cmd_module,
        "preflight_notebook_transport_policy",
        lambda *_args, **_kwargs: NotebookTransportPolicy(
            notebook="default",
            notebook_id="nb-public",
            gpu_model="",
        ),
    )


# ---------------------------------------------------------------------------
# Unit tests for _build_scp_base_args
# ---------------------------------------------------------------------------


def test_build_scp_base_args_basic() -> None:
    bridge = BridgeProfile(name="test", proxy_url="https://proxy.example.com")
    args = _build_scp_base_args(
        bridge=bridge, proxy_cmd="rtunnel proxy --url https://proxy.example.com"
    )

    assert args[0] == "scp"
    assert "-P" in args
    port_idx = args.index("-P")
    assert args[port_idx + 1] == str(bridge.ssh_port)
    assert "ProxyCommand=rtunnel proxy --url https://proxy.example.com" in " ".join(args)
    assert "-r" not in args


def test_build_scp_base_args_recursive() -> None:
    bridge = BridgeProfile(name="test", proxy_url="https://proxy.example.com")
    args = _build_scp_base_args(
        bridge=bridge,
        proxy_cmd="rtunnel proxy --url https://proxy.example.com",
        recursive=True,
    )

    assert "-r" in args


def test_build_scp_base_args_no_user_host() -> None:
    """Base args should NOT include user@host -- that's part of the remote specifier."""
    bridge = BridgeProfile(name="test", proxy_url="https://proxy.example.com")
    args = _build_scp_base_args(bridge=bridge, proxy_cmd="cmd")

    joined = " ".join(args)
    assert "@localhost" not in joined


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_bridge_scp_upload_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(
        scp_cmd_module,
        "run_scp_transfer",
        lambda **kw: FakeResult(),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "scp", "default", str(local_file), "/tmp/test.txt"])

    assert result.exit_code == EXIT_SUCCESS
    assert result.output.strip() == "OK"


def test_bridge_scp_forwards_workspace_account_and_pick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")
    tunnel_config = TunnelConfig(account="alice")
    bridge = BridgeProfile(
        name="gpu-box",
        notebook_name="gpu-box",
        workspace_name="CPU资源空间",
        proxy_url="https://proxy.example.com",
    )
    tunnel_config.add_bridge(bridge)
    policy_calls: dict[str, Any] = {}
    target_calls: dict[str, Any] = {}
    transfer_calls: dict[str, Any] = {}

    monkeypatch.setattr(
        scp_cmd_module,
        "preflight_notebook_transport_policy",
        lambda _ctx, **kwargs: (
            policy_calls.update(kwargs)
            or NotebookTransportPolicy(
                notebook="gpu-box",
                notebook_id="nb-123",
                gpu_model="",
            )
        ),
    )
    monkeypatch.setattr(
        scp_cmd_module,
        "resolve_cached_notebook_target",
        lambda _ctx, **kwargs: (
            target_calls.update(kwargs)
            or SimpleNamespace(config=tunnel_config, bridge=bridge)
        ),
    )
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kwargs: True)
    monkeypatch.setattr(
        scp_cmd_module,
        "run_scp_transfer",
        lambda **kwargs: transfer_calls.update(kwargs) or SimpleNamespace(returncode=0),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "scp",
            "gpu-box",
            "--workspace",
            "CPU资源空间",
            "--account",
            "alice",
            "--pick",
            "2",
            str(local_file),
            "/tmp/test.txt",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert policy_calls["workspace"] == "CPU资源空间"
    assert policy_calls["account"] == "alice"
    assert policy_calls["pick"] == 2
    assert target_calls["workspace"] == "CPU资源空间"
    assert target_calls["account"] == "alice"
    assert target_calls["pick"] == 2
    assert transfer_calls["config"] is tunnel_config
    assert transfer_calls["bridge_name"] == "gpu-box"


def test_bridge_scp_reads_active_account_notebook_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    config = Config(username="alice", password="")
    tunnel_config = TunnelConfig(account="default")
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def fake_load_tunnel_config(account=None):  # type: ignore[no-untyped-def]
        captured["account"] = account
        return tunnel_config

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", fake_load_tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kwargs: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", lambda **kwargs: FakeResult())

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "scp", "default", str(local_file), "/tmp/test.txt"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["account"] is None


def test_bridge_scp_warns_when_remote_path_is_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(
        scp_cmd_module,
        "run_scp_transfer",
        lambda **kw: FakeResult(),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "scp", "default", str(local_file), "artifacts/test.txt"],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "prefer an absolute path" in result.output
    assert "Warning: remote destination 'artifacts/test.txt'" in result.output


def test_bridge_scp_preserves_absolute_remote_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    def fake_scp(**kwargs: Any) -> FakeResult:
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", fake_scp)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "notebook",
            "scp",
            "default",
            str(local_file),
            "/inspire/ssd/project/p1/alice/artifacts/test.txt",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "relative" not in result.output
    assert captured["remote_path"] == "/inspire/ssd/project/p1/alice/artifacts/test.txt"


def test_bridge_scp_warns_when_remote_source_is_relative_on_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(
        scp_cmd_module,
        "run_scp_transfer",
        lambda **kw: FakeResult(),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "scp", "default", "-d", "artifacts/test.txt", str(tmp_path / "local.txt")],
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "prefer an absolute path" in result.output
    assert "Warning: remote source 'artifacts/test.txt'" in result.output


def test_bridge_scp_download_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(
        scp_cmd_module,
        "run_scp_transfer",
        lambda **kw: FakeResult(),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "scp", "default", "--download", "/tmp/remote.txt", str(tmp_path / "local.txt")]
    )

    assert result.exit_code == EXIT_SUCCESS
    assert result.output.strip() == "OK"


def test_bridge_scp_recursive_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    captured: Dict[str, Any] = {}

    class FakeResult:
        returncode = 0

    def fake_scp(**kwargs: Any) -> FakeResult:
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", fake_scp)

    # Create a directory so upload path validation passes
    src_dir = tmp_path / "mydir"
    src_dir.mkdir()
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--debug", "notebook", "scp", "default", "-r", str(src_dir), "/tmp/mydir"],
    )
    clear_debug_logging()

    assert result.exit_code == EXIT_SUCCESS
    assert result.output == "OK\n"
    assert captured["recursive"] is True
    for noise in ("SCP upload:", "Notebook:", "Remote path:", "Mode: recursive"):
        assert noise not in result.output
    [log_path] = list(log_dir.glob("inspire-debug-*.log"))
    assert "Notebook SCP transfer started" in log_path.read_text(encoding="utf-8")


def test_bridge_scp_auto_recursive_for_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    captured: Dict[str, Any] = {}

    class FakeResult:
        returncode = 0

    def fake_scp(**kwargs: Any) -> FakeResult:
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", fake_scp)

    src_dir = tmp_path / "autodir"
    src_dir.mkdir()

    runner = CliRunner()
    # No -r flag, but source is a directory
    result = runner.invoke(cli_main, ["notebook", "scp", "default", str(src_dir), "/tmp/autodir"])

    assert result.exit_code == EXIT_SUCCESS
    assert captured["recursive"] is True


def test_bridge_scp_tunnel_not_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "scp", "default", str(local_file), "/tmp/test.txt"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "SSH tunnel not available" in result.output


def test_bridge_scp_local_path_not_found(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "scp", "default", "/nonexistent/file.txt", "/tmp/test.txt"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Local path not found" in result.output


def test_bridge_scp_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", lambda **kw: FakeResult())

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "scp", "default", str(local_file), "/tmp/test.txt"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "success"
    assert payload["data"]["direction"] == "upload"


def test_bridge_scp_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    def fake_scp(**kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="scp", timeout=5)

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", fake_scp)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "scp", "default", str(local_file), "/tmp/test.txt", "--timeout", "5"]
    )

    assert result.exit_code == EXIT_TIMEOUT
    assert "timed out" in result.output.lower()


def test_bridge_scp_scp_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="default", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 1
        stderr = "scp: /tmp/test.txt: Permission denied\n"

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", lambda **kw: FakeResult())

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "scp", "default", str(local_file), "/tmp/test.txt"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "SCP upload failed" in result.output
    assert "Permission denied" in result.output


def test_bridge_scp_bridge_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com"))

    captured: Dict[str, Any] = {}

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(scp_cmd_module, "is_tunnel_available", lambda **kw: True)

    class FakeResult:
        returncode = 0

    def fake_scp(**kwargs: Any) -> FakeResult:
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(scp_cmd_module, "run_scp_transfer", fake_scp)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "scp", "gpu-main", str(local_file), "/tmp/test.txt"]
    )

    assert result.exit_code == EXIT_SUCCESS
    assert captured["bridge_name"] == "gpu-main"


def test_bridge_scp_missing_bridge_reports_bridge_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "test.txt"
    local_file.write_text("hello", encoding="utf-8")

    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(BridgeProfile(name="gpu-main", proxy_url="https://proxy.example.com"))

    monkeypatch.setattr(scp_cmd_module, "load_tunnel_config", lambda: tunnel_config)

    def fail_if_checked(**kwargs: Any) -> bool:  # noqa: ARG001
        raise AssertionError("should not check availability")

    monkeypatch.setattr(
        scp_cmd_module,
        "is_tunnel_available",
        fail_if_checked,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["notebook", "scp", "missing", str(local_file), "/tmp/test.txt"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No cached notebook connection for 'missing'" in result.output
