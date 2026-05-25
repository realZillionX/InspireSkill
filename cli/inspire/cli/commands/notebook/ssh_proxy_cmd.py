"""OpenSSH ProxyCommand entry for notebook SSH."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import (
    exec_rtunnel_proxy,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .notebook_ssh_flow import run_notebook_ssh


@click.command("ssh-proxy")
@click.argument("notebook")
@click.option("--workspace", required=False, help="Workspace name.")
@click.option(
    "--port",
    "ssh_port",
    type=click.IntRange(1, 65535),
    default=22222,
    show_default=True,
    help="SSH service port inside notebook; OpenSSH passes this as %p.",
)
@click.option(
    "--connection-port",
    type=click.IntRange(1, 65535),
    default=31337,
    show_default=True,
    help="Advanced: connection service port inside notebook.",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize if bootstrap is needed.",
)
@click.option(
    "--timeout",
    "setup_timeout",
    type=click.IntRange(1),
    default=300,
    show_default=True,
    help="Timeout in seconds for notebook connection setup.",
)
@pass_context
def ssh_proxy_cmd(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    ssh_port: int,
    connection_port: int,
    pubkey: str | None,
    setup_timeout: int,
) -> None:
    """Connect OpenSSH to a notebook SSH server through Inspire's tunnel.

    This command is intended for OpenSSH ProxyCommand. It streams raw SSH
    traffic on stdin/stdout; diagnostics are written to stderr.
    """
    config = load_tunnel_config()
    bridge = config.get_bridge(notebook)
    needs_bootstrap = bridge is None
    if bridge is not None:
        ready = is_tunnel_available(
            bridge_name=bridge.name,
            config=config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        needs_bootstrap = not ready

    if needs_bootstrap:
        bootstrap_workspace = workspace or (bridge.workspace_name if bridge else None)
        if not bootstrap_workspace:
            click.echo(
                (
                    "No cached notebook connection and no workspace was provided. "
                    "Generate config with `inspire notebook ssh-config <notebook> --workspace <workspace>`."
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)
        click.echo(
            f"Preparing notebook SSH connection for {scrub_raw_ids(notebook)}...",
            err=True,
        )
        run_notebook_ssh(
            ctx,
            notebook_id=notebook,
            workspace=bootstrap_workspace,
            wait=True,
            pubkey=pubkey,
            port=connection_port,
            ssh_port=ssh_port,
            command=None,
            command_timeout=None,
            debug_playwright=False,
            setup_timeout=setup_timeout,
            setup_only=True,
        )
        config = load_tunnel_config()
        bridge = config.get_bridge(notebook)

    if bridge is None:
        click.echo(
            f"No cached notebook connection for {scrub_raw_ids(notebook)} after bootstrap.",
            err=True,
        )
        sys.exit(EXIT_GENERAL_ERROR)

    try:
        exec_rtunnel_proxy(
            bridge,
            config,
            target_host="localhost",
            target_port=ssh_port,
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Notebook ssh proxy failed: {exc}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)


__all__ = ["ssh_proxy_cmd"]
