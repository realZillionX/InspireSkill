"""`notebook shell` command -- open an interactive shell to a cached notebook."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Optional

import click

from inspire.bridge.tunnel import (
    get_ssh_command_args,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.tunnel_reconnect import (
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config import Config, ConfigError, build_env_exports, resolve_remote_cwd
from inspire.platform.web import browser_api as browser_api_module

from .target_resolver import resolve_cached_notebook_target

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"


def _resolve_shell_remote_cwd(*, cwd: Optional[str], config: Config) -> Optional[str]:
    return resolve_remote_cwd(cwd=cwd, aliases=config.path_aliases)


def _build_remote_shell_command(*, remote_cwd: Optional[str], env_exports: str) -> Optional[str]:
    if remote_cwd:
        return f'{env_exports}cd "{remote_cwd}" && exec $SHELL -l'
    if env_exports:
        return f"{env_exports}exec $SHELL -l"
    return None


def _load_tunnel_config_for_account(account: str | None):
    return load_tunnel_config(account=account) if account else load_tunnel_config()


@click.command("shell")
@click.argument("notebook")
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
@click.option(
    "--cwd",
    default=None,
    help="Remote working directory or path alias (default: 'me' alias, else $HOME)",
)
@pass_context
def bridge_ssh(
    ctx: Context,
    notebook: str,
    account: str | None,
    ignore_target_cache: bool,
    cwd: Optional[str],
) -> None:
    """Open an interactive SSH shell to a cached notebook.

    Requires a cached notebook connection. Create one with
    ``inspire notebook connection refresh <notebook> --workspace <workspace>``.

    \b
    Example:
        inspire notebook connection refresh my-notebook --workspace <workspace>
        inspire notebook shell my-notebook
        inspire notebook shell my-notebook --cwd me
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
        remote_cwd = _resolve_shell_remote_cwd(cwd=cwd, config=config)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    try:
        env_exports = build_env_exports(config.remote_env)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook,
        workspace=None,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=False,
        allow_prompt=not ctx.json_output,
    )
    if target is None:
        explicit_account = (
            str(account or "").strip()
            if str(account or "").strip() and str(account or "").strip().lower() != "all"
            else None
        )
        tunnel_config = _load_tunnel_config_for_account(explicit_account)
        selected_bridge = tunnel_config.get_bridge(notebook)
        target_account = tunnel_config.account
    else:
        tunnel_config = target.config
        selected_bridge = target.bridge
        target_account = target.account

    if selected_bridge is None:
        _handle_error(
            ctx,
            "TunnelError",
            f"No cached notebook connection for '{notebook}'.",
            hint="Create one with: inspire notebook connection refresh <notebook> --workspace <workspace>",
        )
        raise RuntimeError("unreachable")

    bridge_name = selected_bridge.name
    logger.debug("bridge_ssh start bridge=%s", bridge_name)

    remote_command = _build_remote_shell_command(
        remote_cwd=remote_cwd,
        env_exports=env_exports,
    )
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_attempt = 0
    should_rebuild = False
    opened_once = False
    web_session = None
    ssh_public_key = ""

    while True:
        tunnel_config = _load_tunnel_config_for_account(target_account)
        bridge_profile = tunnel_config.get_bridge(bridge_name)
        if bridge_profile is None:
            _handle_error(
                ctx,
                "BridgeNotFound",
                f"No cached notebook connection for '{bridge_name}'.",
                hint="Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh this notebook connection.",
            )
            raise RuntimeError("unreachable")

        tunnel_ready = is_tunnel_available(
            bridge_name=bridge_name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if should_rebuild or not tunnel_ready:
            if reconnect_attempt >= reconnect_limit:
                _handle_error(
                    ctx,
                    "TunnelError",
                    "SSH tunnel not available",
                    hint=(
                        "Auto-rebuild retries exhausted. Run "
                        "'inspire notebook connection status <notebook>' and retry "
                        "'inspire notebook connection refresh <notebook> --workspace <workspace>'."
                    ),
                )

            notebook_id = str(getattr(bridge_profile, "notebook_id", "") or "").strip()
            if not notebook_id:
                _handle_error(
                    ctx,
                        "TunnelError",
                        "SSH tunnel not available",
                        hint=(
                            "This cached connection is missing notebook metadata, so it cannot be "
                            "rebuilt automatically. Re-create it via "
                            "'inspire notebook connection refresh <notebook> --workspace <workspace>'."
                        ),
                    )

            try:
                if web_session is None:
                    if target_account:
                        web_session = require_web_session(
                            ctx,
                            hint=WEB_AUTH_HINT,
                            account=target_account,
                        )
                    else:
                        web_session = require_web_session(ctx, hint=WEB_AUTH_HINT)
                notebook_detail = browser_api_module.get_notebook_detail(
                    notebook_id=notebook_id,
                    session=web_session,
                )
                notebook_status = str((notebook_detail or {}).get("status") or "").strip().upper()
                if notebook_status and notebook_status != _RUNNING_NOTEBOOK_STATUS:
                    _handle_error(
                        ctx,
                        "TunnelError",
                        f"SSH tunnel not available. Notebook '{bridge_name}' is {notebook_status}.",
                        hint=(
                            f"Start it with `inspire notebook start {bridge_name} --workspace <workspace>` if needed, "
                            f"or wait until `inspire notebook status {bridge_name} --workspace <workspace>` reports "
                            "RUNNING, then retry."
                        ),
                    )
            except Exception as status_error:  # noqa: BLE001
                logger.debug(
                    "Skipping notebook status preflight bridge=%s notebook_id=%s error=%s",
                    bridge_name,
                    notebook_id,
                    status_error,
                )

            reconnect_attempt += 1
            if not ctx.json_output:
                click.echo(
                    f"Tunnel unavailable; rebuilding automatically "
                    f"(attempt {reconnect_attempt}/{reconnect_limit})...",
                    err=True,
                )
            try:
                if web_session is None:
                    if target_account:
                        web_session = require_web_session(
                            ctx,
                            hint=WEB_AUTH_HINT,
                            account=target_account,
                        )
                    else:
                        web_session = require_web_session(ctx, hint=WEB_AUTH_HINT)
                if not ssh_public_key:
                    ssh_public_key = load_ssh_public_key_material()
                rebuild_notebook_bridge_profile(
                    bridge_name=bridge_name,
                    bridge=bridge_profile,
                    tunnel_config=tunnel_config,
                    session=web_session,
                    ssh_public_key=ssh_public_key,
                )
                should_rebuild = False
            except (ValueError, ConfigError) as e:
                if reconnect_attempt >= reconnect_limit:
                    _handle_error(
                        ctx,
                        "TunnelError",
                        f"Automatic tunnel rebuild failed: {e}",
                        hint="Check credentials, SSH key, and notebook status, then retry.",
                    )
                pause_s = retry_pause_seconds(
                    reconnect_attempt,
                    base_pause=reconnect_pause,
                    progressive=True,
                )
                if pause_s > 0:
                    time.sleep(pause_s)
            except Exception as e:
                if reconnect_attempt >= reconnect_limit:
                    _handle_error(
                        ctx,
                        "TunnelError",
                        f"Automatic tunnel rebuild failed: {e}",
                        hint="Verify the notebook is RUNNING and retry.",
                    )
                pause_s = retry_pause_seconds(
                    reconnect_attempt,
                    base_pause=reconnect_pause,
                    progressive=True,
                )
                if pause_s > 0:
                    time.sleep(pause_s)
            continue

        ssh_args = get_ssh_command_args(
            bridge_name=bridge_name,
            config=tunnel_config,
            remote_command=remote_command,
        )
        if not opened_once and not ctx.json_output:
            click.echo("Opening SSH connection...")
            click.echo(f"Notebook: {scrub_raw_ids(bridge_name)}")
            click.echo(f"Working directory: {scrub_raw_ids(remote_cwd or '$HOME')}")
            click.echo("Press Ctrl+D or type 'exit' to disconnect")
            click.echo("")
            opened_once = True

        try:
            returncode = subprocess.call(ssh_args)
        except KeyboardInterrupt:
            logger.debug("bridge_ssh interrupted bridge=%s", bridge_name)
            raise SystemExit(130) from None

        logger.debug("bridge_ssh returncode bridge=%s code=%s", bridge_name, returncode)
        if returncode == 0:
            sys.exit(0)
        if should_attempt_ssh_reconnect(returncode, interactive=True):
            if not ctx.json_output:
                click.echo(
                    "SSH connection dropped; attempting automatic tunnel rebuild...",
                    err=True,
                )
            should_rebuild = True
            continue
        sys.exit(returncode if returncode is not None else EXIT_GENERAL_ERROR)
