"""Windows-only compatibility checks for native tunnel support."""

from __future__ import annotations

from pathlib import Path

from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.bridge.tunnel.scp import _build_scp_base_args
from inspire.bridge.tunnel.ssh import _get_proxy_command
from inspire.bridge.tunnel.ssh_exec import _build_ssh_base_args


def test_windows_uses_native_paths_and_open_ssh_null_device(monkeypatch) -> None:  # noqa: ANN001
    import inspire.bridge.tunnel.models as models
    import inspire.bridge.tunnel.scp as scp
    import inspire.bridge.tunnel.ssh_exec as ssh_exec

    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(scp.sys, "platform", "win32")
    monkeypatch.setattr(ssh_exec.sys, "platform", "win32")
    bridge = BridgeProfile(name="demo", proxy_url="https://example.invalid/proxy/31337/")
    assert TunnelConfig().rtunnel_bin.name == "rtunnel.exe"
    assert "UserKnownHostsFile=NUL" in _build_ssh_base_args(bridge=bridge, proxy_cmd="proxy")
    assert "UserKnownHostsFile=NUL" in _build_scp_base_args(bridge=bridge, proxy_cmd="proxy")


def test_windows_proxy_command_uses_cmd_quoting(monkeypatch) -> None:  # noqa: ANN001
    import inspire.bridge.tunnel.ssh as ssh

    monkeypatch.setattr(ssh.sys, "platform", "win32")
    bridge = BridgeProfile(name="demo", proxy_url="https://example.invalid/proxy/31337/")
    command = _get_proxy_command(bridge, Path(r"C:\\Users\\me\\.inspire\\bin\\rtunnel.exe"), quiet=True)
    assert "2>NUL" in command
    assert "sh -c" not in command
