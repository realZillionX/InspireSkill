"""Notebook connection cache management commands."""

from __future__ import annotations

import sys
import time

import click

from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    is_tunnel_available,
    load_tunnel_config,
    run_ssh_command,
    save_tunnel_config,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .notebook_ssh_flow import run_notebook_ssh


def _bridge_payload(bridge: BridgeProfile, *, healthy: bool | None = None) -> dict[str, object]:
    payload = bridge.to_dict()
    if healthy is not None:
        payload["healthy"] = healthy
    return payload


def _load_bridge_or_exit(ctx: Context, notebook: str) -> tuple[TunnelConfig, BridgeProfile]:
    config = load_tunnel_config()
    bridge = config.get_bridge(notebook)
    if bridge is not None:
        return config, bridge
    message = f"No cached notebook connection for '{notebook}'"
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error("NotFound", message, EXIT_CONFIG_ERROR),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(EXIT_CONFIG_ERROR)


@click.group("connection")
def notebook_connection() -> None:
    """Inspect and manage cached notebook SSH connections."""


@notebook_connection.command("list")
@click.option(
    "--verify/--no-verify",
    default=False,
    help="Verify each cached connection with SSH before printing.",
)
@pass_context
def connection_list(ctx: Context, verify: bool) -> None:
    """List cached notebook connections."""
    config = load_tunnel_config()
    rows: list[dict[str, object]] = []
    for bridge in config.list_bridges():
        healthy = (
            is_tunnel_available(
                bridge_name=bridge.name,
                config=config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
            if verify
            else None
        )
        rows.append(_bridge_payload(bridge, healthy=healthy))

    if ctx.json_output:
        click.echo(json_formatter.format_json({"connections": rows}))
        return

    if not rows:
        click.echo("No cached notebook connections.")
        return

    for bridge in config.list_bridges():
        workspace = bridge.workspace_name or "(workspace unknown)"
        status = ""
        if verify:
            healthy = is_tunnel_available(
                bridge_name=bridge.name,
                config=config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
            status = "  healthy=yes" if healthy else "  healthy=no"
        click.echo(
            f"{scrub_raw_ids(bridge.name)}  workspace={scrub_raw_ids(workspace)}  "
            f"ssh={scrub_raw_ids(bridge.ssh_user)}:{bridge.ssh_port}{status}"
        )


@notebook_connection.command("status")
@click.argument("notebook")
@click.option(
    "--workspace",
    required=False,
    help="Workspace name. Used only when a refresh is needed later.",
)
@pass_context
def connection_status(ctx: Context, notebook: str, workspace: str | None) -> None:
    """Test a cached notebook connection."""
    del workspace
    config, bridge = _load_bridge_or_exit(ctx, notebook)
    start = time.time()
    try:
        result = run_ssh_command("hostname", bridge_name=bridge.name, config=config, timeout=30)
    except Exception as exc:  # noqa: BLE001
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(exc), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Connection failed: {exc}"), err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    elapsed_ms = int((time.time() - start) * 1000)
    ok = result.returncode == 0
    hostname = (result.stdout or "").strip()
    if ctx.json_output:
        if ok:
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook": bridge.name,
                        "hostname": hostname,
                        "elapsed_ms": elapsed_ms,
                        "bridge": _bridge_payload(bridge, healthy=True),
                    }
                )
            )
        else:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelError",
                    result.stderr or "Connection failed",
                    EXIT_GENERAL_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_GENERAL_ERROR)
        return

    if ok:
        click.echo(human_formatter.format_success(f"Notebook '{bridge.name}': connected"))
        click.echo(f"Hostname: {scrub_raw_ids(hostname)}")
        click.echo(f"Response time: {elapsed_ms}ms")
        return

    click.echo(human_formatter.format_error(f"Connection failed: {result.stderr}"), err=True)
    sys.exit(EXIT_GENERAL_ERROR)


@notebook_connection.command("refresh")
@click.argument("notebook")
@click.option("--workspace", required=False, help="Workspace name.")
@click.option("--wait/--no-wait", default=True, help="Wait for notebook to reach RUNNING status")
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize",
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
@click.option("--debug-playwright", is_flag=True, help="Run browser automation visibly")
@click.option(
    "--timeout",
    "setup_timeout",
    type=click.IntRange(1),
    default=300,
    show_default=True,
    help="Timeout in seconds for notebook connection setup",
)
@pass_context
def connection_refresh(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    wait: bool,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """Create or refresh the cached connection without opening SSH."""
    run_notebook_ssh(
        ctx,
        notebook_id=notebook,
        workspace=workspace,
        wait=wait,
        pubkey=pubkey,
        port=port,
        ssh_port=ssh_port,
        command=None,
        command_timeout=None,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
        setup_only=True,
    )
    if not ctx.json_output:
        click.echo(f"Refreshed cached notebook connection: {scrub_raw_ids(notebook)}")


@notebook_connection.command("forget")
@click.argument("notebook")
@click.option("--workspace", required=False, help="Workspace name used to disambiguate metadata.")
@pass_context
def connection_forget(ctx: Context, notebook: str, workspace: str | None) -> None:
    """Forget a cached notebook connection."""
    config, bridge = _load_bridge_or_exit(ctx, notebook)
    if workspace and bridge.workspace_name and bridge.workspace_name != workspace:
        message = (
            f"Cached notebook '{notebook}' belongs to workspace "
            f"'{bridge.workspace_name}', not '{workspace}'."
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ValidationError", message, EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(message), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    was_default = notebook == config.default_bridge
    config.remove_bridge(notebook)
    save_tunnel_config(config)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed",
                    "notebook": notebook,
                    "new_default": config.default_bridge,
                }
            )
        )
        return
    click.echo(f"Removed cached notebook connection: {scrub_raw_ids(notebook)}")
    click.echo("OpenSSH config was not modified.")
    if was_default and config.default_bridge:
        click.echo(f"New default: {scrub_raw_ids(config.default_bridge)}")


@notebook_connection.command("prune")
@click.option("--dry-run", is_flag=True, help="Show stale entries without removing them.")
@pass_context
def connection_prune(ctx: Context, dry_run: bool) -> None:
    """Remove cached connections that fail a lightweight SSH check."""
    config = load_tunnel_config()
    stale: list[str] = []
    for bridge in list(config.list_bridges()):
        healthy = is_tunnel_available(
            bridge_name=bridge.name,
            config=config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if not healthy:
            stale.append(bridge.name)
            if not dry_run:
                config.remove_bridge(bridge.name)
    if stale and not dry_run:
        save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "stale": stale,
                    "removed": [] if dry_run else stale,
                    "dry_run": dry_run,
                }
            )
        )
        return

    if not stale:
        click.echo("No stale cached notebook connections found.")
        return
    action = "Would remove" if dry_run else "Removed"
    for name in stale:
        click.echo(f"{action}: {scrub_raw_ids(name)}")


__all__ = ["notebook_connection"]
