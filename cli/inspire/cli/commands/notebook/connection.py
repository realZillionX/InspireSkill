"""Notebook connection cache management commands."""

from __future__ import annotations

import logging
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
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import validate_workspace_operation_name

from .notebook_ssh_flow import run_notebook_ssh
from .public_output import public_operation, sanitize_public_text
from .target_resolver import forget_notebook_targets, list_notebook_targets
from .transport import emit_ssh_policy_error, preflight_notebook_transport_policy

logger = logging.getLogger(__name__)


def _safe_bridge_name(bridge: BridgeProfile) -> str:
    for candidate in (bridge.notebook_name, bridge.name):
        name = sanitize_public_text(candidate, omit_urls=True)
        if name:
            return name
    return "(unknown)"


def _validate_workspace_selector(ctx: Context, workspace: str | None) -> str | None:
    if workspace is None or not workspace.strip():
        return None
    workspace = reject_id_at_boundary(
        ctx,
        workspace,
        resource_type="workspace",
        list_command="inspire account context",
    )
    try:
        return validate_workspace_operation_name(workspace)
    except ConfigError as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        raise RuntimeError("unreachable")


def _bridge_payload(bridge: BridgeProfile, *, healthy: bool | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": _safe_bridge_name(bridge),
    }
    if bridge.workspace_name:
        payload["workspace"] = sanitize_public_text(bridge.workspace_name, omit_urls=True)
    if healthy is not None:
        payload["connected"] = healthy
    return payload


def _load_bridge_or_exit(ctx: Context, notebook: str) -> tuple[TunnelConfig, BridgeProfile]:
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    config = load_tunnel_config()
    bridge = config.get_bridge(notebook)
    if bridge is not None:
        return config, bridge
    message = f"No cached notebook connection for '{scrub_raw_ids(notebook)}'"
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error("NotFound", message, EXIT_CONFIG_ERROR),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(EXIT_CONFIG_ERROR)


def _validate_cached_workspace(
    ctx: Context,
    *,
    notebook: str,
    requested: str | None,
    bridge: BridgeProfile,
) -> None:
    if not requested or not bridge.workspace_name or bridge.workspace_name == requested:
        return
    _handle_error(
        ctx,
        "ValidationError",
        (
            f"Cached notebook '{notebook}' belongs to workspace "
            f"'{bridge.workspace_name}', not '{requested}'."
        ),
        EXIT_CONFIG_ERROR,
    )


@click.group("connection")
def notebook_connection() -> None:
    """Inspect and manage cached notebook SSH connections."""


@notebook_connection.group("target")
def connection_target() -> None:
    """Inspect and reset remembered notebook targets in the selected account."""


@connection_target.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum remembered targets to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every remembered target.")
@pass_context
def connection_target_list(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List remembered notebook target selections."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    rows = list_notebook_targets()
    page = bound_collection(rows, limit=effective_limit)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": page.items,
                    **page.metadata(),
                }
            )
        )
        return

    if not page.items:
        click.echo("No remembered notebook targets.")
        return

    for row in page.items:
        name = str(row.get("name") or "(unknown)")
        account = str(row.get("account") or "(none)")
        workspace = str(row.get("workspace") or "(any)")
        click.echo(
            f"{scrub_raw_ids(name)}  account={scrub_raw_ids(account)}  "
            f"workspace={scrub_raw_ids(workspace)}"
        )
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


@connection_target.command("forget")
@click.argument("notebook", metavar="NAME")
@click.option(
    "--workspace",
    required=False,
    metavar="NAME",
    help="Workspace selector to narrow the deletion.",
)
@click.option(
    "--account",
    required=False,
    metavar="NAME",
    help="Account selector to narrow the deletion.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def connection_target_forget(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    account: str | None,
    yes: bool,
) -> None:
    """Forget remembered target selections without removing SSH connections."""
    workspace = _validate_workspace_selector(ctx, workspace)
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Forget remembered notebook target '{scrub_raw_ids(notebook)}'?",
        message="Notebook target removal requires confirmation.",
    )
    removed = forget_notebook_targets(
        notebook=notebook,
        workspace=workspace,
        account=account,
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed" if removed else "not_found",
                    "name": notebook,
                    "removed_count": len(removed),
                }
            )
        )
        return

    if not removed:
        click.echo(f"No remembered notebook target matched: {scrub_raw_ids(notebook)}")
        return
    click.echo(
        f"Removed remembered notebook target entries for {scrub_raw_ids(notebook)}: {len(removed)}"
    )


@notebook_connection.command("list")
@click.option(
    "--verify/--no-verify",
    default=False,
    help="Verify each cached connection with SSH before printing.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum cached connections to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every cached connection.")
@pass_context
def connection_list(
    ctx: Context,
    verify: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """List cached notebook connections."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    config = load_tunnel_config()
    bridge_page = bound_collection(
        list(config.list_bridges()),
        limit=effective_limit,
    )
    rows: list[dict[str, object]] = []
    for bridge in bridge_page.items:
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
        click.echo(
            json_formatter.format_json(
                {
                    "items": rows,
                    **bridge_page.metadata(),
                }
            )
        )
        return

    if not rows:
        click.echo("No cached notebook connections.")
        return

    for row in rows:
        workspace = row.get("workspace") or "(workspace unknown)"
        status = ""
        if verify:
            status = "  connected=yes" if row.get("connected") else "  connected=no"
        click.echo(f"{row['name']}  workspace={workspace}{status}")
    notice = truncation_notice(bridge_page)
    if notice:
        click.echo(notice)


@notebook_connection.command("status")
@click.argument("notebook", metavar="NAME")
@click.option(
    "--workspace",
    required=False,
    metavar="NAME",
    help="Workspace name used to validate the cached connection.",
)
@pass_context
def connection_status(ctx: Context, notebook: str, workspace: str | None) -> None:
    """Test a cached notebook connection."""
    workspace = _validate_workspace_selector(ctx, workspace)
    config, bridge = _load_bridge_or_exit(ctx, notebook)
    _validate_cached_workspace(
        ctx,
        notebook=notebook,
        requested=workspace,
        bridge=bridge,
    )
    start = time.monotonic()
    try:
        result = run_ssh_command("hostname", bridge_name=bridge.name, config=config, timeout=30)
    except Exception:  # noqa: BLE001
        logger.debug("Notebook connection health check failed", exc_info=True)
        _handle_error(
            ctx,
            "TunnelError",
            "Could not check notebook connection.",
            EXIT_GENERAL_ERROR,
        )
        return

    elapsed_ms = int((time.monotonic() - start) * 1000)
    ok = result.returncode == 0
    logger.debug(
        "Notebook connection health check completed connected=%s elapsed_ms=%s",
        ok,
        elapsed_ms,
    )
    if ctx.json_output:
        if ok:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": sanitize_public_text(notebook, omit_urls=True),
                        "status": "connected",
                    }
                )
            )
        else:
            click.echo(
                json_formatter.format_json_error(
                    "TunnelError", "Could not connect notebook.", EXIT_GENERAL_ERROR
                ),
                err=True,
            )
            sys.exit(EXIT_GENERAL_ERROR)
        return

    if ok:
        display_name = sanitize_public_text(notebook, omit_urls=True)
        click.echo(
            human_formatter.format_success(
                f"Notebook '{scrub_raw_ids(display_name)}': connected"
            )
        )
        return

    click.echo(
        human_formatter.format_error(
            "Could not connect notebook."
        ),
        err=True,
    )
    sys.exit(EXIT_GENERAL_ERROR)


@notebook_connection.command("refresh")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=False, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option("--wait/--no-wait", default=True, help="Wait for notebook to reach RUNNING status")
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    metavar="PATH",
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
    pick: int | None,
    wait: bool,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """Create or refresh SSH/rtunnel cache for SSH-capable notebooks."""
    workspace = _validate_workspace_selector(ctx, workspace)
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    policy = preflight_notebook_transport_policy(
        ctx,
        notebook=notebook,
        workspace=workspace,
        pick=pick,
    )
    if not policy.allow_ssh:
        raise SystemExit(emit_ssh_policy_error(ctx, policy))
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
        pick=pick,
    )
    if ctx.json_output:
        click.echo(json_formatter.format_json(public_operation(notebook, "refreshed")))
    else:
        click.echo(
            human_formatter.format_mutation_success(
                "Notebook connection",
                "refreshed",
                notebook,
            )
        )


@notebook_connection.command("forget")
@click.argument("notebook", metavar="NAME")
@click.option(
    "--workspace",
    required=False,
    metavar="NAME",
    help="Workspace name used to disambiguate metadata.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def connection_forget(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    yes: bool,
) -> None:
    """Forget a cached notebook connection."""
    workspace = _validate_workspace_selector(ctx, workspace)
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Forget cached notebook connection '{scrub_raw_ids(notebook)}'?",
        message="Notebook connection removal requires confirmation.",
    )
    config, bridge = _load_bridge_or_exit(ctx, notebook)
    _validate_cached_workspace(
        ctx,
        notebook=notebook,
        requested=workspace,
        bridge=bridge,
    )

    removed_targets = forget_notebook_targets(
        notebook=notebook,
        workspace=workspace,
        account=getattr(config, "account", None),
        bridge_name=bridge.name,
        notebook_id=bridge.notebook_id,
    )
    config.remove_bridge(notebook)
    save_tunnel_config(config)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed",
                    "name": notebook,
                    "target_cache_removed_count": len(removed_targets),
                }
            )
        )
        return
    click.echo(
        human_formatter.format_mutation_success(
            "Notebook connection",
            "removed",
            notebook,
        )
    )
    if removed_targets:
        click.echo(f"Removed remembered notebook target entries: {len(removed_targets)}")


@notebook_connection.command("prune")
@click.option("--dry-run", is_flag=True, help="Show stale entries without removing them.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def connection_prune(ctx: Context, dry_run: bool, yes: bool) -> None:
    """Remove cached connections that fail a lightweight SSH check."""
    if not dry_run:
        require_confirmation(
            ctx,
            yes=yes,
            prompt="Prune every stale cached notebook connection?",
            message="Notebook connection pruning requires confirmation.",
        )
    config = load_tunnel_config()
    stale: list[str] = []
    removed_targets: list[str] = []
    for bridge in list(config.list_bridges()):
        healthy = is_tunnel_available(
            bridge_name=bridge.name,
            config=config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if not healthy:
            stale.append(_safe_bridge_name(bridge))
            if not dry_run:
                removed_targets.extend(
                    forget_notebook_targets(
                        notebook=bridge.notebook_name or bridge.name,
                        account=getattr(config, "account", None),
                        bridge_name=bridge.name,
                        notebook_id=bridge.notebook_id,
                    )
                )
                config.remove_bridge(bridge.name)
    if stale and not dry_run:
        save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "would_remove" if dry_run else "removed",
                    "count": len(stale),
                    "target_cache_removed_count": 0 if dry_run else len(removed_targets),
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
    if removed_targets and not dry_run:
        click.echo(f"Removed remembered notebook target entries: {len(removed_targets)}")


__all__ = ["notebook_connection"]
