"""Bridge ssh command -- open an interactive SSH shell to Bridge."""

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
from inspire.cli.utils.notebook_cli import require_web_session
from inspire.cli.utils.tunnel_reconnect import (
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config import Config, ConfigError, build_env_exports
from inspire.platform.web import browser_api as browser_api_module

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"


@click.command("ssh")
@click.option("--alias", "-a", "bridge", help="Saved notebook alias to connect to")
@click.option("--bridge", "-b", "bridge", hidden=True, help="(Deprecated) same as --alias")
@pass_context
def bridge_ssh(ctx: Context, bridge: Optional[str]) -> None:
    """Open an interactive SSH shell to Bridge.

    Requires a saved notebook alias with a reachable SSH tunnel. Create one
    with ``inspire notebook ssh <notebook-name> --save-as <alias>``.

    \b
    Example:
        inspire notebook ssh <notebook-name> --save-as mybridge
        inspire notebook shell --bridge mybridge
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True, require_credentials=False)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    try:
        env_exports = build_env_exports(config.remote_env)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    tunnel_config = load_tunnel_config()
    selected_bridge = tunnel_config.get_bridge(bridge)
    if bridge and selected_bridge is None:
        _handle_error(
            ctx,
            "BridgeNotFound",
            f"Bridge '{bridge}' not found.",
            hint="Run 'inspire notebook connections' to see saved notebook aliases.",
        )
    if selected_bridge is None:
        _handle_error(
            ctx,
            "TunnelError",
            "No bridge configured.",
            hint="Run 'inspire notebook ssh <notebook-name> --save-as <name>' first.",
        )

    bridge_name = selected_bridge.name
    logger.debug("bridge_ssh start bridge=%s", bridge_name)

    # Build interactive SSH command with env exports and cd to target dir
    remote_command = f'{env_exports}cd "{config.target_dir}" && exec $SHELL -l'
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_attempt = 0
    should_rebuild = False
    opened_once = False
    web_session = None
    ssh_public_key = ""

    while True:
        tunnel_config = load_tunnel_config()
        bridge_profile = tunnel_config.get_bridge(bridge_name)
        if bridge_profile is None:
            _handle_error(
                ctx,
                "BridgeNotFound",
                f"Bridge '{bridge_name}' not found.",
                hint="Run 'inspire notebook connections' to see saved notebook aliases.",
            )

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
                        "Auto-rebuild retries exhausted. Run 'inspire notebook test' and "
                        "retry 'inspire notebook ssh <notebook-name> --save-as <name>'."
                    ),
                )

            notebook_id = str(getattr(bridge_profile, "notebook_id", "") or "").strip()
            if not notebook_id:
                _handle_error(
                    ctx,
                    "TunnelError",
                    "SSH tunnel not available",
                    hint=(
                        "This bridge has no notebook_id metadata, so it cannot be rebuilt "
                        "automatically. Re-create it via "
                        "'inspire notebook ssh <notebook-name> --save-as <name>'."
                    ),
                )

            try:
                if web_session is None:
                    web_session = require_web_session(
                        ctx,
                        hint=(
                            "Automatic tunnel rebuild needs web authentication. "
                            "Set [auth].username and configure password via INSPIRE_PASSWORD "
                            'or [accounts."<username>"].password.'
                        ),
                    )
                notebook_detail = browser_api_module.get_notebook_detail(
                    notebook_id=notebook_id,
                    session=web_session,
                )
                notebook_status = str((notebook_detail or {}).get("status") or "").strip().upper()
                if notebook_status and notebook_status != _RUNNING_NOTEBOOK_STATUS:
                    _handle_error(
                        ctx,
                        "TunnelError",
                        (
                            "SSH tunnel not available. "
                            f"Bridge '{bridge_name}' notebook '{notebook_id}' "
                            f"is {notebook_status}."
                        ),
                        hint=(
                            f"Start it with 'inspire notebook start {notebook_id}' if needed, "
                            f"or wait until 'inspire notebook status {notebook_id}' reports "
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
                    web_session = require_web_session(
                        ctx,
                        hint=(
                            "Automatic tunnel rebuild needs web authentication. "
                            "Set [auth].username and configure password via INSPIRE_PASSWORD "
                            'or [accounts."<username>"].password.'
                        ),
                    )
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
            click.echo("Opening SSH connection to Bridge...")
            click.echo(f"Bridge: {bridge_name}")
            click.echo(f"Working directory: {config.target_dir}")
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
