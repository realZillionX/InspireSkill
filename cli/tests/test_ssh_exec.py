"""Tests for SSH exec helpers."""

from __future__ import annotations

import io
import shlex
import subprocess
from typing import Any, Optional

import pytest

from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.bridge.tunnel.ssh_exec import run_ssh_command, run_ssh_command_streaming


def _stub_resolve(*args: Any, **kwargs: Any) -> tuple[TunnelConfig, BridgeProfile, str]:
    return (
        TunnelConfig(),
        BridgeProfile(name="default", proxy_url="https://proxy.example.com"),
        "proxy-cmd",
    )


def test_run_ssh_command_forces_c_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    import inspire.bridge.tunnel.ssh_exec as ssh_exec_module

    monkeypatch.setattr(ssh_exec_module, "_resolve_bridge_and_proxy", _stub_resolve)

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ssh_exec_module.subprocess, "run", fake_run)

    result = run_ssh_command("echo ok")

    assert result.returncode == 0
    assert captured["cmd"][-1] == "bash -l"
    assert captured["kwargs"]["env"]["LC_ALL"] == "C"
    assert captured["kwargs"]["env"]["LANG"] == "C"


def test_run_ssh_command_streaming_forces_c_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    import inspire.bridge.tunnel.ssh_exec as ssh_exec_module

    monkeypatch.setattr(ssh_exec_module, "_resolve_bridge_and_proxy", _stub_resolve)

    captured: dict[str, Any] = {}

    class FakeStdin:
        def __init__(self) -> None:
            self.data = ""
            self.closed = False

        def write(self, text: str) -> None:
            self.data += text

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = io.StringIO("hello\n")
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProcess:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        process = FakeProcess()
        captured["process"] = process
        return process

    monkeypatch.setattr(ssh_exec_module.subprocess, "Popen", fake_popen)

    emitted: list[str] = []
    exit_code = run_ssh_command_streaming("echo hello", output_callback=emitted.append)

    assert exit_code == 0
    assert emitted == ["hello\n"]
    assert captured["cmd"][-1] == "bash -l"
    assert captured["kwargs"]["env"]["LC_ALL"] == "C"
    assert captured["kwargs"]["env"]["LANG"] == "C"
    assert captured["process"].stdin.data.startswith("export LC_ALL=C LANG=C; echo hello")


def test_run_ssh_command_streaming_does_not_reemit_lines_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspire.bridge.tunnel.ssh_exec as ssh_exec_module

    monkeypatch.setattr(ssh_exec_module, "_resolve_bridge_and_proxy", _stub_resolve)

    class FakeStdin:
        def __init__(self) -> None:
            self.data = ""

        def write(self, text: str) -> None:
            self.data += text

        def close(self) -> None:
            return None

    class FakeStdout:
        def __init__(self) -> None:
            self.readline_calls = 0

        def readline(self) -> str:
            self.readline_calls += 1
            if self.readline_calls == 1:
                return "dupe\n"
            return ""

        def __iter__(self) -> Any:
            # Simulate a stream implementation that may still expose a line iterator
            # after a readline was already consumed.
            return iter(["dupe\n"])

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.returncode = 2
            self.poll_calls = 0

        def poll(self) -> Optional[int]:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return None
            return self.returncode

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return self.returncode

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProcess:
        return FakeProcess()

    select_calls = {"count": 0}

    def fake_select(read: Any, write: Any, err: Any, timeout: float) -> tuple[Any, Any, Any]:
        select_calls["count"] += 1
        if select_calls["count"] == 1:
            return (read, [], [])
        return ([], [], [])

    monkeypatch.setattr(ssh_exec_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ssh_exec_module.select, "select", fake_select)

    emitted: list[str] = []
    exit_code = run_ssh_command_streaming("echo hello", output_callback=emitted.append)

    assert exit_code == 2
    assert emitted == ["dupe\n"]


def test_run_ssh_command_pass_stdin_uses_remote_command_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspire.bridge.tunnel.ssh_exec as ssh_exec_module

    monkeypatch.setattr(ssh_exec_module, "_resolve_bridge_and_proxy", _stub_resolve)

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ssh_exec_module.subprocess, "run", fake_run)

    result = run_ssh_command("echo ok", pass_stdin=True)

    assert result.returncode == 0
    assert captured["cmd"][-1] == f"bash -l -c {shlex.quote('echo ok')}"
    assert captured["kwargs"]["input"] is None


def test_run_ssh_command_streaming_pass_stdin_does_not_write_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspire.bridge.tunnel.ssh_exec as ssh_exec_module

    monkeypatch.setattr(ssh_exec_module, "_resolve_bridge_and_proxy", _stub_resolve)

    captured: dict[str, Any] = {}

    class FakeStdout:
        def readline(self) -> str:
            return ""

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = None
            self.stdout = FakeStdout()
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProcess:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(ssh_exec_module.subprocess, "Popen", fake_popen)

    exit_code = run_ssh_command_streaming("echo hello", pass_stdin=True)

    assert exit_code == 0
    assert captured["cmd"][-1] == f"bash -l -c {shlex.quote('echo hello')}"
    assert captured["kwargs"]["stdin"] is None
