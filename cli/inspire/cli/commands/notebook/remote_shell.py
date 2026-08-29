"""`notebook shell` command -- open an interactive shell to a cached notebook."""

from __future__ import annotations

import logging
import shlex
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
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.output import emit_success as emit_output_success
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.remote_paths import explicit_remote_cwd
from inspire.cli.utils.terminal_io import run_interactive_pty
from inspire.cli.utils.tunnel_reconnect import (
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config import Config, ConfigError, build_env_exports
from inspire.platform.web import browser_api as browser_api_module

from .target_resolver import (
    NOTEBOOK_TARGET_WORKSPACE_HELP,
    resolve_cached_notebook_target,
    validate_specific_workspace,
)
from .transport import preflight_notebook_transport_policy

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"


def _resolve_shell_remote_cwd(*, cwd: Optional[str]) -> Optional[str]:
    return explicit_remote_cwd(cwd)


def _build_remote_shell_command(*, remote_cwd: Optional[str], env_exports: str) -> Optional[str]:
    if remote_cwd:
        return f'{env_exports}cd "{remote_cwd}" && exec $SHELL -l'
    if env_exports:
        return f"{env_exports}exec $SHELL -l"
    return None


def _build_shell_check_command(*, remote_cwd: Optional[str], env_exports: str) -> str:
    command = "printf 'shell-check-ok\\n'"
    if remote_cwd:
        return f"{env_exports}cd {shlex.quote(remote_cwd)} && {command}"
    return f"{env_exports}{command}"


def _emit_shell_check_success(
    ctx: Context,
    *,
    transport: str,
    returncode: int,
    output: str,
) -> None:
    del transport, output
    emit_output_success(
        ctx,
        payload={
            "status": "success",
            "returncode": returncode,
        },
    )
    if not ctx.json_output:
        click.echo("OK")


def _load_tunnel_config_for_account(account: str | None):
    return load_tunnel_config(account=account) if account else load_tunnel_config()


@click.command("shell")
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
    help="Ignore remembered connections and resolve the current notebook instance live.",
)
@click.option(
    "--cwd",
    default=None,
    help="Absolute remote working directory (default: do not inject cd)",
)
@click.option(
    "--check",
    is_flag=True,
    help="Validate the shell transport without opening an interactive session.",
)
@pass_context
def bridge_ssh(
    ctx: Context,
    notebook: str,
    workspace: str | None,
    account: str | None,
    pick: int | None,
    ignore_target_cache: bool,
    cwd: Optional[str],
    check: bool,
) -> None:
    """Open an interactive shell; SSH when allowed, otherwise JupyterTerminal.

    SSH-capable notebooks use cached SSH, so a freshly created one needs
    `inspire notebook connection refresh <name> --workspace <workspace>` once.
    Restricted H100/H200 notebooks open a JupyterTerminal-backed shell and need
    no setup; leave that one with `exit`, or press Ctrl+] to drop the session.

    \b
    Example:
        inspire notebook connection refresh my-notebook --workspace <workspace>
        inspire notebook shell my-notebook
        inspire notebook shell my-notebook --cwd /inspire/ssd/project/topic/user
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
        remote_cwd = _resolve_shell_remote_cwd(cwd=cwd)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    try:
        env_exports = build_env_exports(config.remote_env)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    policy = preflight_notebook_transport_policy(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        pick=pick,
        ignore_target_cache=ignore_target_cache,
    )
    if policy.exec_transport == "jupyter":
        if check:
            result = browser_api_module.run_command_capture_in_notebook(
                notebook_id=policy.notebook_id,
                command=_build_shell_check_command(
                    remote_cwd=remote_cwd,
                    env_exports=env_exports,
                ),
                session=policy.session,
                timeout=30,
            )
            if result.returncode == 0:
                _emit_shell_check_success(
                    ctx,
                    transport="jupyter_terminal",
                    returncode=result.returncode,
                    output=result.output,
                )
                sys.exit(EXIT_SUCCESS)
            _handle_error(
                ctx,
                "ShellCheckFailed",
                (
                    "JupyterTerminal did not establish or complete the shell check."
                    if not result.completed
                    else f"JupyterTerminal shell check failed with exit code {result.returncode}"
                ),
                EXIT_GENERAL_ERROR,
                hint=(
                    result.output.strip()
                    or (
                        "The notebook target was checked against the live platform. Retry with "
                        "`inspire --debug notebook shell ... --check` to distinguish access URL, "
                        "Jupyter REST, proxy, WebSocket, and completion-marker failures; a manual "
                        "cache refresh should not be needed."
                    )
                ),
            )
        code = browser_api_module.open_jupyter_terminal_shell(
            notebook_id=policy.notebook_id,
            session=policy.session,
            cwd=remote_cwd,
            env_exports=env_exports,
        )
        sys.exit(code)

    target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=True,
        allow_prompt=not ctx.json_output,
        pick=pick,
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
    logger.debug("Notebook shell session starting")

    remote_command = _build_remote_shell_command(
        remote_cwd=remote_cwd,
        env_exports=env_exports,
    )
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_attempt = 0
    should_rebuild = False
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
                    "Notebook status preflight skipped error=%s",
                    scrub_raw_ids(status_error),
                )

            reconnect_attempt += 1
            logger.debug(
                "Notebook shell tunnel rebuild scheduled attempt=%s/%s",
                reconnect_attempt,
                reconnect_limit,
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
            remote_command=(
                _build_shell_check_command(remote_cwd=remote_cwd, env_exports=env_exports)
                if check
                else remote_command
            ),
        )
        if check:
            try:
                completed = subprocess.run(
                    ssh_args,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
            except KeyboardInterrupt:
                logger.debug("Notebook shell check interrupted")
                raise SystemExit(130) from None

            returncode = completed.returncode
            output = (completed.stdout or "") + (completed.stderr or "")
            if returncode == 0:
                _emit_shell_check_success(
                    ctx,
                    transport="ssh",
                    returncode=returncode,
                    output=output,
                )
                sys.exit(EXIT_SUCCESS)
            if should_attempt_ssh_reconnect(
                returncode,
                interactive=False,
                allow_non_interactive=True,
            ):
                logger.debug("Notebook shell check requested automatic tunnel rebuild")
                should_rebuild = True
                continue
            if not ctx.json_output and output.strip():
                click.echo(scrub_raw_ids(output.rstrip()), err=True)
            sys.exit(returncode if returncode is not None else EXIT_GENERAL_ERROR)

        try:
            returncode = run_interactive_pty(ssh_args)
        except KeyboardInterrupt:
            logger.debug("Notebook shell interrupted")
            raise SystemExit(130) from None

        logger.debug("Notebook shell finished returncode=%s", returncode)
        if returncode == 0:
            sys.exit(0)
        if should_attempt_ssh_reconnect(returncode, interactive=True):
            logger.debug("Notebook shell requested automatic tunnel rebuild after disconnect")
            should_rebuild = True
            continue
        sys.exit(returncode if returncode is not None else EXIT_GENERAL_ERROR)
