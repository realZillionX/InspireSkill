"""OpenSSH ProxyCommand entry for notebook SSH."""

from __future__ import annotations

import logging
import sys

import click

from inspire.bridge.tunnel import exec_rtunnel_proxy, is_tunnel_available, load_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .bootstrap_lock import notebook_target_is_ready, serialize_notebook_bootstrap
from .notebook_ssh_flow import run_notebook_ssh
from .target_resolver import (
    NOTEBOOK_TARGET_WORKSPACE_HELP,
    NotebookConnectionTarget,
    resolve_cached_notebook_target,
    validate_specific_workspace,
)
from .transport import emit_ssh_policy_error, preflight_notebook_transport_policy

logger = logging.getLogger(__name__)


def _load_proxy_target(
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
        allow_prompt=False,
        pick=pick,
    )
    if target is not None:
        return target

    explicit_account = (
        str(account or "").strip()
        if str(account or "").strip() and str(account or "").strip().lower() != "all"
        else None
    )
    config = (
        load_tunnel_config(account=explicit_account) if explicit_account else load_tunnel_config()
    )
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
    "--quiet",
    is_flag=True,
    hidden=True,
    help="Accepted for compatibility with pre-6.3 ssh_config entries; ignored.",
)
@pass_context
def ssh_proxy_cmd(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    account: str | None,
    pick: int | None,
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

    ``--quiet`` is now the only behaviour, but ProxyCommand lines written by
    ``ssh-config`` at or below v6.2.0 still pass it. Those live in users'
    ``~/.ssh/config`` and are never rewritten, so the flag stays accepted.
    """
    del quiet
    if ctx.json_output:
        _handle_error(
            ctx,
            "UnsupportedOutput",
            "notebook ssh-proxy does not support JSON output.",
            EXIT_CONFIG_ERROR,
        )
        return

    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    target = _load_proxy_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        pick=pick,
    )
    config = target.config if target else None
    bridge = target.bridge if target else None
    needs_bootstrap = not notebook_target_is_ready(target, is_tunnel_available)

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
        requested_account = str(account or "").strip() or None
        bootstrap_account = target.account if target and target.account else requested_account
        if bootstrap_account and bootstrap_account.lower() == "all":
            bootstrap_account = None
        with serialize_notebook_bootstrap(
            account=bootstrap_account,
            notebook=notebook,
            workspace=bootstrap_workspace,
            setup_timeout=setup_timeout,
        ):
            target = _load_proxy_target(
                ctx,
                notebook=notebook,
                workspace=bootstrap_workspace,
                account=bootstrap_account,
                ignore_target_cache=True,
                pick=pick,
            )
            if not notebook_target_is_ready(target, is_tunnel_available):
                click.echo(
                    f"Preparing notebook SSH connection for {scrub_raw_ids(notebook)}...",
                    err=True,
                )
                policy = preflight_notebook_transport_policy(
                    ctx,
                    notebook=notebook,
                    workspace=bootstrap_workspace,
                    account=bootstrap_account,
                    pick=pick,
                )
                if not policy.allow_ssh:
                    raise SystemExit(emit_ssh_policy_error(ctx, policy))
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
                    account=bootstrap_account,
                    ignore_target_cache=ignore_target_cache,
                    pick=pick,
                )
            target = _load_proxy_target(
                ctx,
                notebook=notebook,
                workspace=bootstrap_workspace,
                account=bootstrap_account,
                ignore_target_cache=True,
                pick=pick,
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
            quiet=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Notebook SSH proxy failed", exc_info=True)
        click.echo("Notebook SSH proxy failed.", err=True)
        sys.exit(EXIT_GENERAL_ERROR)


__all__ = ["ssh_proxy_cmd"]
