"""OpenSSH config output for notebook connections."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import click

from inspire.bridge.tunnel import BridgeProfile, load_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .notebook_ssh_flow import run_notebook_ssh
from .public_output import sanitize_public_text
from .target_resolver import (
    NOTEBOOK_TARGET_WORKSPACE_HELP,
    NotebookConnectionTarget,
    resolve_cached_notebook_target,
    validate_specific_workspace,
)
from .transport import emit_ssh_policy_error, preflight_notebook_transport_policy


def _default_host_alias(notebook: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", notebook.strip()).strip("-")
    return f"inspire-{slug or 'notebook'}"


def _public_identity_file(identity_file: str | None) -> str | None:
    if not identity_file:
        return None

    path = Path(identity_file).expanduser()
    if not path.is_absolute():
        return str(path)
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return None
    return f"~/{relative.as_posix()}"


def _load_cached_target(
    ctx: Context,
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
    ignore_target_cache: bool,
    pick: int | None,
) -> NotebookConnectionTarget | None:
    target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=False,
        allow_prompt=not ctx.json_output,
        pick=pick,
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


def _format_ssh_config(
    *,
    host: str,
    notebook_name: str,
    bridge: BridgeProfile,
    account: str | None,
    pick: int | None,
) -> str:
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
    if pick is not None:
        proxy_parts.extend(["--pick", str(pick)])
    proxy_parts.extend(["--port", "%p"])
    proxy_command = " ".join(shlex.quote(part) for part in proxy_parts)

    lines = [
        f"Host {host}",
        f"  HostName {sanitize_public_text(notebook_name, omit_urls=True)}",
        f"  User {sanitize_public_text(bridge.ssh_user, omit_urls=True)}",
        f"  Port {bridge.ssh_port}",
        f"  ProxyCommand {proxy_command}",
        "  StrictHostKeyChecking accept-new",
    ]
    identity_file = _public_identity_file(bridge.identity_file)
    if identity_file:
        lines.insert(5, f"  IdentityFile {shlex.quote(identity_file)}")
    return "\n".join(lines) + "\n"


@click.command("ssh-config")
@click.argument("notebook", metavar="NAME")
@click.option(
    "--workspace",
    required=False,
    metavar="NAME",
    callback=validate_specific_workspace,
    help=NOTEBOOK_TARGET_WORKSPACE_HELP,
)
@click.option(
    "--account",
    required=False,
    metavar="NAME",
    help="Account name for this notebook target.",
)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
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
    pick: int | None,
    ignore_target_cache: bool,
    host_alias: str | None,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    setup_timeout: int,
) -> None:
    """Print OpenSSH config for a public-internet notebook.

    Use this Host entry for ssh, scp, VS Code Remote SSH, or external rsync
    against /inspire/... shared paths. For restricted notebooks, use a
    public-internet notebook's config entry and keep the same /inspire/... path.
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    target = _load_cached_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        pick=pick,
    )
    if target is None:
        policy = preflight_notebook_transport_policy(
            ctx,
            notebook=notebook,
            workspace=workspace,
            account=account,
            timeout=min(setup_timeout, 30),
            pick=pick,
        )
        if not policy.allow_ssh:
            raise SystemExit(emit_ssh_policy_error(ctx, policy))
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
            pick=pick,
        )
        target = _load_cached_target(
            ctx,
            notebook=notebook,
            workspace=workspace,
            account=account,
            ignore_target_cache=True,
            pick=pick,
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
    host = host_alias or _default_host_alias(notebook)
    config_text = _format_ssh_config(
        host=host,
        notebook_name=notebook,
        bridge=bridge,
        account=target.account,
        pick=pick,
    )
    if ctx.json_output:
        click.echo(json_formatter.format_json(config_text))
        return

    if not bridge.workspace_name:
        click.echo(
            "Warning: cached connection has no workspace metadata; "
            "regenerate with --workspace to make ssh_config stable.",
            err=True,
        )

    click.echo(config_text, nl=False)
    if ctx.debug:
        click.echo(f"SSH config ready for {scrub_raw_ids(host)}.", err=True)


__all__ = ["ssh_config_cmd"]
