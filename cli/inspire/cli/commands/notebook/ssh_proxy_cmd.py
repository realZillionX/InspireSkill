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
from .target_resolver import NotebookConnectionTarget, resolve_cached_notebook_target


def _load_proxy_target(
    ctx: Context,
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
    ignore_target_cache: bool,
) -> NotebookConnectionTarget | None:
    target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=False,
        allow_prompt=False,
    )
    if target is not None:
        return target

    explicit_account = (
        str(account or "").strip()
        if str(account or "").strip() and str(account or "").strip().lower() != "all"
        else None
    )
    config = load_tunnel_config(account=explicit_account) if explicit_account else load_tunnel_config()
    bridge = config.get_bridge(notebook)
    if bridge is None:
        return None
    return NotebookConnectionTarget(
        account=config.account,
        config=config,
        bridge=bridge,
        source="active_bridge_cache",
    )


@click.command("ssh-proxy")
@click.argument("notebook")
@click.option("--workspace", required=False, help="Workspace name.")
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
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
@click.option(
    "--quiet/--verbose",
    default=True,
    show_default=True,
    help="Suppress rtunnel client lifecycle logs after the proxy starts.",
)
@pass_context
def ssh_proxy_cmd(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    account: str | None,
    ignore_target_cache: bool,
    ssh_port: int,
    connection_port: int,
    pubkey: str | None,
    setup_timeout: int,
    quiet: bool,
) -> None:
    """Connect OpenSSH to a notebook SSH server through Inspire's tunnel.

    This command is intended for OpenSSH ProxyCommand. It streams raw SSH
    traffic on stdin/stdout. Bootstrap diagnostics are written to stderr;
    rtunnel's own lifecycle logs are suppressed by default.
    """
    target = _load_proxy_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
    )
    config = target.config if target else None
    bridge = target.bridge if target else None
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
            account=account,
            ignore_target_cache=ignore_target_cache,
        )
        target = _load_proxy_target(
            ctx,
            notebook=notebook,
            workspace=bootstrap_workspace,
            account=account,
            ignore_target_cache=True,
        )
        config = target.config if target else None
        bridge = target.bridge if target else None

    if bridge is None or config is None:
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
            quiet=quiet,
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Notebook ssh proxy failed: {exc}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)


__all__ = ["ssh_proxy_cmd"]
