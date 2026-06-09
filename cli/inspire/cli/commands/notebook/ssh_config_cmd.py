"""OpenSSH config output for notebook connections."""

from __future__ import annotations

import re
import shlex
import sys

import click

from inspire.bridge.tunnel import BridgeProfile, load_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .notebook_ssh_flow import run_notebook_ssh
from .target_resolver import NotebookConnectionTarget, resolve_cached_notebook_target


def _default_host_alias(notebook: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", notebook.strip()).strip("-")
    return f"inspire-{slug or 'notebook'}"


def _load_cached_target(
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
        allow_prompt=not ctx.json_output,
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


def _format_ssh_config(*, host: str, bridge: BridgeProfile, account: str | None) -> str:
    proxy_parts = [
        "inspire",
        "notebook",
        "ssh-proxy",
        "%h",
    ]
    if account:
        proxy_parts.extend(["--account", account])
    if bridge.workspace_name:
        proxy_parts.extend(["--workspace", bridge.workspace_name])
    proxy_parts.extend(["--port", "%p"])
    proxy_parts.append("--quiet")
    proxy_command = " ".join(shlex.quote(part) for part in proxy_parts)

    lines = [
        f"Host {host}",
        f"  HostName {bridge.notebook_name or bridge.name}",
        f"  User {bridge.ssh_user}",
        f"  Port {bridge.ssh_port}",
        f"  ProxyCommand {proxy_command}",
        "  StrictHostKeyChecking accept-new",
    ]
    if bridge.identity_file:
        lines.insert(5, f"  IdentityFile {bridge.identity_file}")
    return "\n".join(lines) + "\n"


@click.command("ssh-config")
@click.argument("notebook")
@click.option("--workspace", required=False, help="Workspace name.")
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
@click.option("--host", "host_alias", required=False, help="OpenSSH Host alias to emit.")
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize before printing config.",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=31337,
    show_default=True,
    help="Advanced: connection service port inside notebook",
)
@click.option(
    "--ssh-port",
    type=click.IntRange(1, 65535),
    default=22222,
    show_default=True,
    help="Advanced: SSH service port inside notebook",
)
@click.option(
    "--timeout",
    "setup_timeout",
    type=click.IntRange(1),
    default=300,
    show_default=True,
    help="Timeout in seconds for notebook connection setup",
)
@pass_context
def ssh_config_cmd(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    account: str | None,
    ignore_target_cache: bool,
    host_alias: str | None,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    setup_timeout: int,
) -> None:
    """Print an OpenSSH config snippet for a notebook."""
    target = _load_cached_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
    )
    if target is None:
        run_notebook_ssh(
            ctx,
            notebook_id=notebook,
            workspace=workspace,
            wait=True,
            pubkey=pubkey,
            port=port,
            ssh_port=ssh_port,
            command=None,
            command_timeout=None,
            debug_playwright=False,
            setup_timeout=setup_timeout,
            setup_only=True,
            account=account,
            ignore_target_cache=ignore_target_cache,
        )
        target = _load_cached_target(
            ctx,
            notebook=notebook,
            workspace=workspace,
            account=account,
            ignore_target_cache=True,
        )

    if target is None:
        message = f"No cached notebook connection for '{notebook}'"
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("NotFound", message, EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(message), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    bridge = target.bridge
    if not bridge.workspace_name:
        click.echo(
            "Warning: cached connection has no workspace metadata; "
            "regenerate with --workspace to make ssh_config stable.",
            err=True,
        )

    host = host_alias or _default_host_alias(notebook)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "host": host,
                    "notebook": bridge.name,
                    "account": target.account,
                    "workspace": bridge.workspace_name,
                    "config": _format_ssh_config(
                        host=host,
                        bridge=bridge,
                        account=target.account,
                    ),
                }
            )
        )
        return

    click.echo(_format_ssh_config(host=host, bridge=bridge, account=target.account), nl=False)
    click.echo(
        f"# Add this to ~/.ssh/config, then run: ssh {scrub_raw_ids(host)}",
        err=True,
    )


__all__ = ["ssh_config_cmd"]
