from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook import connection as connection_module
from inspire.cli.commands.notebook import ssh as ssh_module
from inspire.cli.context import EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_SUCCESS
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import NotebookFailedError

flow_module = importlib.import_module("inspire.cli.commands.notebook.notebook_ssh_flow")
ssh_tunnel_module = importlib.import_module("inspire.bridge.tunnel")
ssh_config_module = importlib.import_module("inspire.cli.commands.notebook.ssh_config_cmd")
ssh_proxy_module = importlib.import_module("inspire.cli.commands.notebook.ssh_proxy_cmd")
workspace_module = importlib.import_module("inspire.config.workspaces")


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


def test_notebook_ssh_stopped_error_is_actionable(monkeypatch) -> None:  # noqa: ANN001
    events = "\n".join(
        [
            "The service is starting up...",
            "Notebook stopped because its CPU/GPU/MEM usage has not met the auto-recycle rules set by the manager.",
            "Heartbeat lost when saving notebook nb-raw as image demo: Error invalid response code",
        ]
    )

    monkeypatch.setattr(
        flow_module,
        "require_web_session",
        lambda *_args, **_kwargs: SimpleNamespace(
            all_workspace_ids=["ws-cpu"],
            all_workspace_names={"ws-cpu": "CPU资源空间"},
        ),
    )
    monkeypatch.setattr(flow_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        flow_module,
        "load_config",
        lambda _ctx: SimpleNamespace(tunnel_retries=0, tunnel_retry_pause=0.0),
    )
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_query_scope",
        lambda *_args, **_kwargs: (["ws-cpu"], "CPU资源空间"),
    )
    monkeypatch.setattr(
        flow_module,
        "_resolve_notebook_id",
        lambda *_args, **_kwargs: ("nb-stopped", "ws-cpu"),
    )
    monkeypatch.setattr(
        ssh_tunnel_module,
        "load_tunnel_config",
        lambda account=None: TunnelConfig(),
    )

    def fake_wait_for_notebook_running(*_args, **_kwargs):  # noqa: ANN202
        raise NotebookFailedError(
            "nb-stopped",
            "STOPPED",
            {"status": "STOPPED", "name": "demo-box"},
            events=events,
        )

    monkeypatch.setattr(
        flow_module.browser_api_module,
        "wait_for_notebook_running",
        fake_wait_for_notebook_running,
    )

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "ssh", "demo-box", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == EXIT_API_ERROR
    assert "Notebook is stopped: demo-box" in result.output
    assert "Notebook failed to start" not in result.output
    assert "Stop reason: Notebook stopped because its CPU/GPU/MEM usage" in result.output
    assert "Heartbeat lost" not in result.output
    assert "inspire notebook start demo-box --workspace 'CPU资源空间' --wait" in result.output
    assert "inspire notebook ssh demo-box --workspace 'CPU资源空间'" in result.output
