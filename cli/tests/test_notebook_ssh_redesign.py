from __future__ import annotations

import importlib
import json

from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook import connection as connection_module
from inspire.cli.commands.notebook import ssh as ssh_module
from inspire.cli.context import EXIT_CONFIG_ERROR, EXIT_SUCCESS
from inspire.cli.main import main as cli_main

ssh_config_module = importlib.import_module("inspire.cli.commands.notebook.ssh_config_cmd")
ssh_proxy_module = importlib.import_module("inspire.cli.commands.notebook.ssh_proxy_cmd")


def test_notebook_ssh_default_route_runs_notebook_command(monkeypatch) -> None:  # noqa: ANN001
    calls = []

    def fake_run_notebook_ssh(ctx, **kwargs):  # noqa: ANN001
        del ctx
        calls.append(kwargs)

    monkeypatch.setattr(ssh_module, "run_notebook_ssh", fake_run_notebook_ssh)

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "ssh",
            "demo-box",
            "--workspace",
            "CPU资源空间",
            "--",
            "hostname",
            "-f",
        ],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert calls == [
        {
            "notebook_id": "demo-box",
            "workspace": "CPU资源空间",
            "wait": True,
            "pubkey": None,
            "port": 31337,
            "ssh_port": 22222,
            "command": "hostname -f",
            "command_timeout": None,
            "debug_playwright": False,
            "setup_timeout": 300,
        }
    ]


def test_notebook_help_exposes_connection_and_openssh_commands() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "--help"])

    assert result.exit_code == EXIT_SUCCESS
    for command in ("connection", "ssh", "ssh-config", "ssh-proxy"):
        assert f"\n  {command} " in result.output


def test_notebook_ssh_help_keeps_compatibility_commands() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "ssh", "--help"])

    assert result.exit_code == EXIT_SUCCESS
    assert "Open SSH to a notebook or run a remote command" in result.output
    for subcommand in ("connect", "refresh", "forget", "test"):
        assert f"\n  {subcommand} " in result.output


def test_ssh_refresh_compat_entry_uses_connection_refresh_semantics() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "ssh", "refresh", "--help"])

    assert result.exit_code == EXIT_SUCCESS
    assert "Create or refresh the cached connection without opening SSH" in result.output
    assert "--url" not in result.output
    assert "--has-internet" not in result.output


def test_ssh_config_uses_cached_bridge_and_proxy_command(monkeypatch) -> None:  # noqa: ANN001
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="demo-box",
            proxy_url="https://proxy.invalid/proxy/31337/",
            notebook_name="demo-box",
            workspace_name="CPU资源空间",
            identity_file="/home/me/.ssh/id_ed25519",
        )
    )

    monkeypatch.setattr(ssh_config_module, "load_tunnel_config", lambda: tunnel_config)

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "ssh-config", "demo-box", "--host", "inspire-demo"],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Host inspire-demo" in result.output
    assert "HostName demo-box" in result.output
    assert "IdentityFile /home/me/.ssh/id_ed25519" in result.output
    assert (
        "ProxyCommand inspire notebook ssh-proxy %h --workspace "
        "'CPU资源空间' --port %p"
    ) in result.output
    assert "proxy.invalid" not in result.output


def test_connection_list_json_keeps_proxy_url(monkeypatch) -> None:  # noqa: ANN001
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="demo-box",
            proxy_url="https://proxy.invalid/proxy/31337/",
            workspace_name="CPU资源空间",
        )
    )

    monkeypatch.setattr(connection_module, "load_tunnel_config", lambda: tunnel_config)

    result = CliRunner().invoke(cli_main, ["--json", "notebook", "connection", "list"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["data"]["connections"][0]["proxy_url"] == (
        "https://proxy.invalid/proxy/31337/"
    )


def test_connection_forget_removes_cache_only(monkeypatch) -> None:  # noqa: ANN001
    tunnel_config = TunnelConfig()
    tunnel_config.add_bridge(
        BridgeProfile(
            name="demo-box",
            proxy_url="https://proxy.invalid/proxy/31337/",
            workspace_name="CPU资源空间",
        )
    )
    saved = []

    monkeypatch.setattr(connection_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(connection_module, "save_tunnel_config", lambda cfg: saved.append(cfg))

    result = CliRunner().invoke(cli_main, ["notebook", "connection", "forget", "demo-box"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "OpenSSH config was not modified" in result.output
    assert saved == [tunnel_config]
    assert "demo-box" not in tunnel_config.bridges


def test_ssh_proxy_requires_workspace_without_cached_bridge(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(ssh_proxy_module, "load_tunnel_config", lambda: TunnelConfig())

    result = CliRunner().invoke(cli_main, ["notebook", "ssh-proxy", "demo-box"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "No cached notebook connection and no workspace was provided" in result.output
