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


def _default_host_alias(notebook: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", notebook.strip()).strip("-")
    return f"inspire-{slug or 'notebook'}"


def _load_cached_bridge(notebook: str) -> BridgeProfile | None:
    config = load_tunnel_config()
    return config.get_bridge(notebook)


def _format_ssh_config(*, host: str, bridge: BridgeProfile) -> str:
    proxy_parts = [
        "inspire",
        "notebook",
        "ssh-proxy",
        "%h",
    ]
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
    host_alias: str | None,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    setup_timeout: int,
) -> None:
    """Print an OpenSSH config snippet for a notebook."""
    bridge = _load_cached_bridge(notebook)
    if bridge is None or workspace:
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
        )
        bridge = _load_cached_bridge(notebook)

    if bridge is None:
        message = f"No cached notebook connection for '{notebook}'"
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("NotFound", message, EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(message), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

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
                    "workspace": bridge.workspace_name,
                    "config": _format_ssh_config(host=host, bridge=bridge),
                }
            )
        )
        return

    click.echo(_format_ssh_config(host=host, bridge=bridge), nl=False)
    click.echo(
        f"# Add this to ~/.ssh/config, then run: ssh {scrub_raw_ids(host)}",
        err=True,
    )


__all__ = ["ssh_config_cmd"]
