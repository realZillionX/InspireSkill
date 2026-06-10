"""`notebook scp` command -- transfer files to/from a cached notebook via SCP."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.config import Config, ConfigError, resolve_remote_path_alias
from inspire.bridge.tunnel import (
    TunnelNotAvailableError,
    BridgeNotFoundError,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.bridge.tunnel.scp import run_scp_transfer
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .target_resolver import resolve_cached_notebook_target


def _scp_failure_details(result: object) -> str | None:
    for attr in ("stderr", "stdout"):
        value = getattr(result, attr, None)
        text = str(value or "").strip()
        if not text:
            continue
        line = text.splitlines()[-1].strip()
        if line:
            return line[:400]
    return None


def _warn_if_remote_path_is_relative(remote_path: str, *, download: bool) -> None:
    if remote_path.startswith("/"):
        return

    role = "source" if download else "destination"
    click.echo(
        (
            f"Warning: remote {role} '{remote_path}' is relative on the notebook; "
            "it does not use path aliases. Prefer an absolute path."
        ),
        err=True,
    )


@click.command("scp")
@click.argument("notebook")
@click.argument("source")
@click.argument("destination")
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
@click.option("--download", "-d", is_flag=True, help="Download from remote (default is upload)")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories recursively")
@click.option("--timeout", "-t", type=click.IntRange(1), default=None, help="Timeout in seconds")
@pass_context
def bridge_scp(
    ctx: Context,
    notebook: str,
    source: str,
    destination: str,
    account: str | None,
    ignore_target_cache: bool,
    download: bool,
    recursive: bool,
    timeout: Optional[int],
) -> None:
    """Transfer files to/from a cached notebook via SCP.

    Requires `inspire notebook connection refresh <notebook> --workspace <workspace>`
    first. NOTEBOOK is the notebook name.
    By default, uploads SOURCE (local) to DESTINATION (remote).
    Use --download to download SOURCE (remote) to DESTINATION (local).
    Remote paths may be absolute paths, aliases, or alias:sub/path values
    from [path_aliases]. Relative remote paths are allowed but trigger a
    warning because their meaning depends on the remote shell.

    \b
    Examples:
        inspire notebook scp my-notebook ./model.py me:repo/model.py
        inspire notebook scp my-notebook ./data/ me:repo/data/ -r
        inspire notebook scp my-notebook -d me:repo/results.tar.gz ./results.tar.gz
        inspire notebook scp my-notebook -d me:repo/checkpoints/ ./checkpoints/ -r
        inspire notebook scp my-notebook ./bundle.tar me:
    """
    from inspire.cli.utils.id_resolver import reject_id_at_boundary

    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    # Validate local path exists for uploads
    if not download:
        local = Path(source)
        if not local.exists():
            msg = f"Local path not found: {source}"
            _handle_error(ctx, "FileNotFound", msg, EXIT_GENERAL_ERROR)

        # Auto-enable recursive for directories
        if local.is_dir() and not recursive:
            recursive = True

    target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook,
        workspace=None,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=True,
        allow_prompt=not ctx.json_output,
    )
    if target is None:
        explicit_account = (
            str(account or "").strip()
            if str(account or "").strip() and str(account or "").strip().lower() != "all"
            else None
        )
        tunnel_config = (
            load_tunnel_config(account=explicit_account)
            if explicit_account
            else load_tunnel_config()
        )
        bridge_profile = tunnel_config.get_bridge(notebook)
    else:
        tunnel_config = target.config
        bridge_profile = target.bridge

    if bridge_profile is None:
        message = f"No cached notebook connection for '{notebook}'."
        hint = "Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh this notebook connection."
        _handle_error(ctx, "BridgeNotFound", message, EXIT_GENERAL_ERROR, hint=hint)
        return

    bridge = bridge_profile.name

    if not is_tunnel_available(bridge_name=bridge, config=tunnel_config):
        hint = (
            "Run 'inspire notebook connection status <notebook>' to troubleshoot. "
            "If needed, re-create the cached connection via "
            "'inspire notebook connection refresh <notebook> --workspace <workspace>'."
        )
        _handle_error(ctx, "TunnelError", "SSH tunnel not available", EXIT_GENERAL_ERROR, hint=hint)

    if download:
        local_path, remote_path = destination, source
    else:
        local_path, remote_path = source, destination

    try:
        remote_path, used_alias = resolve_remote_path_alias(
            remote_path,
            config.path_aliases,
            require_absolute_or_alias=False,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    _warn_if_remote_path_is_relative(remote_path, download=download)

    direction = "download" if download else "upload"

    if not ctx.json_output and ctx.debug:
        click.echo(f"SCP {direction}: {scrub_raw_ids(source)} -> {scrub_raw_ids(destination)}")
        if bridge:
            click.echo(f"Notebook: {scrub_raw_ids(bridge)}")
        if used_alias:
            click.echo(f"Remote path: {scrub_raw_ids(remote_path)}")
        if recursive:
            click.echo("Mode: recursive")

    try:
        result = run_scp_transfer(
            local_path=local_path,
            remote_path=remote_path,
            download=download,
            recursive=recursive,
            bridge_name=bridge,
            config=tunnel_config,
            timeout=timeout,
        )

        if result.returncode != 0:
            detail = _scp_failure_details(result)
            message = f"SCP {direction} failed with exit code {result.returncode}"
            if detail:
                message = f"{message}: {scrub_raw_ids(detail)}"
            _handle_error(
                ctx,
                "SCPFailed",
                message,
                EXIT_GENERAL_ERROR,
            )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "status": "success",
                        "direction": direction,
                        "source": source,
                        "destination": destination,
                        "recursive": recursive,
                    }
                )
            )
        else:
            click.echo("OK")

    except BridgeNotFoundError as e:
        _handle_error(ctx, "BridgeNotFound", scrub_raw_ids(e), EXIT_GENERAL_ERROR)
    except TunnelNotAvailableError as e:
        _handle_error(ctx, "TunnelError", scrub_raw_ids(e), EXIT_GENERAL_ERROR)
    except subprocess.TimeoutExpired:
        msg = f"SCP {direction} timed out after {timeout}s"
        _handle_error(ctx, "Timeout", msg, EXIT_TIMEOUT)
