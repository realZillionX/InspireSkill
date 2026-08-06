"""`notebook exec` command -- execute a shell command on a cached notebook."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import time
from typing import Callable, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.config import Config, ConfigError, build_env_exports, resolve_remote_cwd
from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
    is_tunnel_available,
    run_ssh_command,
    run_ssh_command_streaming,
    load_tunnel_config,
)
from inspire.cli.utils.errors import emit_error as _emit_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.output import emit_success as emit_output_success
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.tunnel_reconnect import (
    NotebookBridgeReconnectState,
    NotebookBridgeReconnectStatus,
    attempt_notebook_bridge_rebuild,
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    should_attempt_ssh_reconnect,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

from .target_resolver import (
    NOTEBOOK_TARGET_WORKSPACE_HELP,
    resolve_cached_notebook_target,
    validate_specific_workspace,
)
from .transport import preflight_notebook_transport_policy

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"
_DEFAULT_EXEC_TIMEOUT = 600


def _build_remote_command(*, command: str, remote_cwd: Optional[str], env_exports: str) -> str:
    if not remote_cwd:
        return f"{env_exports}{command}"
    return f'{env_exports}cd "{remote_cwd}" && {command}'


def _resolve_exec_remote_cwd(*, cwd: Optional[str], config: Config) -> Optional[str]:
    return resolve_remote_cwd(cwd=cwd, aliases=config.path_aliases)


def _normalize_exec_command(command_parts: tuple[str, ...]) -> str:
    if not command_parts:
        raise click.UsageError("Provide a command to execute.")
    if len(command_parts) == 1:
        return command_parts[0]
    return shlex.join(command_parts)


def _should_auto_passthrough_stdin() -> bool:
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        if stdin.isatty():
            return False
    except Exception:  # noqa: BLE001
        return False

    try:
        mode = os.fstat(stdin.fileno()).st_mode
    except Exception:  # noqa: BLE001
        return False

    import stat as _stat

    return _stat.S_ISFIFO(mode) or _stat.S_ISREG(mode)


def _emit_command_failed(ctx: Context, *, returncode: int) -> int:
    return _emit_error(ctx, "CommandFailed", f"Command failed with exit code {returncode}")


def _load_tunnel_config_for_account(account: Optional[str]) -> TunnelConfig:
    return load_tunnel_config(account=account) if account else load_tunnel_config()


def try_exec_via_ssh_tunnel(
    ctx: Context,
    *,
    command: str,
    bridge_name: Optional[str],
    tunnel_account: Optional[str],
    stdin_mode: bool,
    config: Config,
    remote_cwd: Optional[str],
    env_exports: str,
    timeout_s: int,
    is_tunnel_available_fn: Callable[..., bool],
    run_ssh_command_fn: Callable[..., object],
    run_ssh_command_streaming_fn: Callable[..., int],
) -> int:
    """Execute the command through the cached SSH tunnel."""
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=reconnect_pause,
    )
    resolved_bridge_name = bridge_name
    force_rebuild = False
    ssh_execution_started = False
    full_command = _build_remote_command(
        command=command,
        remote_cwd=remote_cwd,
        env_exports=env_exports,
    )

    def _load_public_key(_path: Optional[str] = None) -> str:
        return load_ssh_public_key_material()

    def _require_rebuild(
        bridge: BridgeProfile,
        tunnel_config: TunnelConfig,
        *,
        reason: str,
    ) -> Optional[int]:
        nonlocal force_rebuild

        if not str(bridge.notebook_id or "").strip():
            hint = (
                "Run 'inspire notebook connection status <notebook>' to troubleshoot. "
                "If needed, re-create the cached connection via "
                "'inspire notebook connection refresh <notebook> --workspace <workspace>'."
            )
            return _emit_error(
                ctx,
                "TunnelError",
                "SSH tunnel not available. "
                f"Notebook '{bridge.name}' is not responding "
                "(notebook may be stopped).",
                hint=hint,
            )

        if reconnect_state.reconnect_attempt >= reconnect_limit:
            return _emit_error(
                ctx,
                "TunnelError",
                "SSH tunnel not available",
                hint=(
                    "Auto-rebuild retries exhausted. Run "
                    "'inspire notebook connection status <notebook>' and retry "
                    "'inspire notebook connection refresh <notebook> --workspace <workspace>'."
                ),
            )

        notebook_id = str(bridge.notebook_id or "").strip()
        if notebook_id:
            try:
                if reconnect_state.web_session is None:
                    if tunnel_account:
                        reconnect_state.web_session = require_web_session(
                            ctx,
                            hint=WEB_AUTH_HINT,
                            account=tunnel_account,
                        )
                    else:
                        reconnect_state.web_session = require_web_session(
                            ctx,
                            hint=WEB_AUTH_HINT,
                        )
                notebook_detail = browser_api_module.get_notebook_detail(
                    notebook_id=notebook_id,
                    session=reconnect_state.web_session,
                )
                notebook_status = str((notebook_detail or {}).get("status") or "").strip().upper()
                if notebook_status and notebook_status != _RUNNING_NOTEBOOK_STATUS:
                    return _emit_error(
                        ctx,
                        "TunnelError",
                        f"SSH tunnel not available. Notebook '{bridge.name}' is {notebook_status}.",
                        hint=(
                            f"Start it with `inspire notebook start {bridge.name} --workspace <workspace>` if needed, "
                            f"or wait until `inspire notebook status {bridge.name} --workspace <workspace>` reports "
                            "RUNNING, then retry."
                        ),
                    )
            except Exception as status_error:  # noqa: BLE001
                logger.debug(
                    "Notebook status preflight skipped error=%s",
                    scrub_raw_ids(status_error),
                )

        logger.debug(
            "Notebook SSH tunnel rebuild scheduled reason=%s attempt=%s/%s",
            reason,
            reconnect_state.reconnect_attempt + 1,
            reconnect_limit,
        )

        result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=bridge.name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: (
                require_web_session(
                    ctx,
                    hint=WEB_AUTH_HINT,
                    account=tunnel_account,
                )
                if tunnel_account
                else require_web_session(ctx, hint=WEB_AUTH_HINT)
            ),
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=_load_public_key,
        )

        if result.status is NotebookBridgeReconnectStatus.REBUILT:
            force_rebuild = False
            return None

        if result.status is NotebookBridgeReconnectStatus.RETRY_LATER:
            if result.pause_seconds > 0:
                time.sleep(result.pause_seconds)
            return None

        # EXHAUSTED or unexpected status — rebuild failed.
        if isinstance(result.error, (ValueError, ConfigError)):
            return _emit_error(
                ctx,
                "TunnelError",
                f"Automatic tunnel rebuild failed: {result.error}",
                hint="Check credentials, SSH key, and notebook status, then retry.",
            )

        return _emit_error(
            ctx,
            "TunnelError",
            (
                f"Automatic tunnel rebuild failed: {result.error}"
                if result.error
                else "SSH tunnel not available"
            ),
            hint="Verify the notebook is RUNNING and retry.",
        )

    def _should_retry_after_disconnect_code(
        *,
        returncode: int,
        tunnel_config: object,
        bridge_name_to_check: str,
    ) -> bool:
        """Retry non-interactive SSH only when 255 also coincides with tunnel loss.

        SSH uses exit code 255 both for transport failures and some command failures.
        To avoid re-running non-idempotent commands incorrectly, require a quick
        tunnel health probe to fail before attempting rebuild/retry.
        """
        if not should_attempt_ssh_reconnect(
            returncode,
            interactive=False,
            allow_non_interactive=True,
        ):
            return False

        try:
            tunnel_still_ready = is_tunnel_available_fn(
                bridge_name=bridge_name_to_check,
                config=tunnel_config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
        except Exception as probe_error:  # noqa: BLE001
            logger.debug(
                "Skipping auto-retry after SSH 255: tunnel probe failed: %s",
                scrub_raw_ids(probe_error),
            )
            return False

        return not tunnel_still_ready

    while True:
        try:
            tunnel_config = _load_tunnel_config_for_account(tunnel_account)
            bridge = tunnel_config.get_bridge(resolved_bridge_name)
            if bridge_name and bridge is None:
                return _emit_error(
                    ctx,
                    "ConfigError",
                    f"No cached notebook connection for '{bridge_name}'.",
                    hint="Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh this notebook connection.",
                )
            if bridge is None:
                return _emit_error(
                    ctx,
                    "TunnelError",
                    "No cached notebook connection for SSH execution.",
                    hint="Create one with: inspire notebook connection refresh <notebook> --workspace <workspace>",
                )

            resolved_bridge_name = bridge.name
            availability_retries = 0 if force_rebuild else int(config.tunnel_retries)
            availability_pause = 0.0 if force_rebuild else float(config.tunnel_retry_pause)
            tunnel_ready = is_tunnel_available_fn(
                bridge_name=resolved_bridge_name,
                config=tunnel_config,
                retries=availability_retries,
                retry_pause=availability_pause,
                progressive=not force_rebuild,
            )

            if force_rebuild or not tunnel_ready:
                reconnect_error = _require_rebuild(
                    bridge,
                    tunnel_config,
                    reason=(
                        "SSH connection dropped; rebuilding tunnel automatically"
                        if force_rebuild
                        else "Tunnel unavailable; rebuilding automatically"
                    ),
                )
                if reconnect_error is not None:
                    return reconnect_error
                continue

            if ctx.json_output:
                ssh_execution_started = True
                run_kwargs: dict[str, object] = {
                    "command": full_command,
                    "bridge_name": resolved_bridge_name,
                    "timeout": timeout_s,
                    "capture_output": True,
                }
                if stdin_mode:
                    run_kwargs["pass_stdin"] = True
                result = run_ssh_command_fn(
                    **run_kwargs,
                )
                returncode = getattr(result, "returncode", 1)
                if returncode == 0:
                    stdout = getattr(result, "stdout", "") or ""
                    stderr = getattr(result, "stderr", "") or ""
                    emit_output_success(
                        ctx,
                        payload={
                            "status": "success",
                            "returncode": returncode,
                            "output": stdout + stderr,
                        },
                    )
                    return EXIT_SUCCESS

                if _should_retry_after_disconnect_code(
                    returncode=returncode,
                    tunnel_config=tunnel_config,
                    bridge_name_to_check=resolved_bridge_name,
                ):
                    force_rebuild = True
                    continue

                return _emit_command_failed(ctx, returncode=returncode)

            logger.debug(
                "Notebook SSH command started stdin_passthrough=%s",
                stdin_mode,
            )
            ssh_execution_started = True
            stream_kwargs: dict[str, object] = {
                "command": full_command,
                "bridge_name": resolved_bridge_name,
                "timeout": timeout_s,
            }
            if stdin_mode:
                stream_kwargs["pass_stdin"] = True
            exit_code = run_ssh_command_streaming_fn(**stream_kwargs)
            logger.debug("Notebook SSH command finished returncode=%s", exit_code)

            if exit_code == 0:
                click.echo("OK")
                return EXIT_SUCCESS

            if _should_retry_after_disconnect_code(
                returncode=exit_code,
                tunnel_config=tunnel_config,
                bridge_name_to_check=resolved_bridge_name,
            ):
                force_rebuild = True
                continue

            return _emit_command_failed(ctx, returncode=exit_code)

        except TunnelNotAvailableError as e:
            if ssh_execution_started:
                return _emit_error(
                    ctx,
                    "TunnelError",
                    f"SSH execution failed: {e}",
                )
            force_rebuild = True
            continue
        except subprocess.TimeoutExpired:
            _emit_error(
                ctx,
                "Timeout",
                f"Command timed out after {timeout_s}s",
                EXIT_TIMEOUT,
                human_lines=[f"Command timed out after {timeout_s}s"],
            )
            return EXIT_TIMEOUT
        except Exception as e:
            if ssh_execution_started:
                return _emit_error(
                    ctx,
                    "SSHExecutionError",
                    f"SSH execution failed: {e}",
                )
            return _emit_error(
                ctx,
                "SSHExecutionError",
                f"SSH execution failed before command start: {e}",
            )


def try_exec_via_jupyter_terminal(
    ctx: Context,
    *,
    notebook_id: str,
    command: str,
    session: WebSession | None,
    remote_cwd: Optional[str],
    env_exports: str,
    timeout_s: int,
) -> int:
    full_command = _build_remote_command(
        command=command,
        remote_cwd=remote_cwd,
        env_exports=env_exports,
    )
    result = browser_api_module.run_command_capture_in_notebook(
        notebook_id=notebook_id,
        command=full_command,
        session=session,
        timeout=timeout_s,
    )
    if ctx.json_output:
        if result.returncode == 0:
            emit_output_success(
                ctx,
                payload={
                    "status": "success",
                    "returncode": result.returncode,
                    "output": result.output,
                },
            )
            return EXIT_SUCCESS
        click.echo(
            json_formatter.format_json_error(
                "CommandFailed",
                "Remote command failed",
                result.returncode,
                data={
                    "returncode": result.returncode,
                    "output": result.output,
                },
            ),
            err=True,
        )
        return result.returncode
    if result.output:
        click.echo(scrub_raw_ids(result.output), nl=False)
    if result.returncode == 0:
        click.echo("OK")
        return EXIT_SUCCESS
    return _emit_command_failed(ctx, returncode=result.returncode)


@click.command("exec")
@click.argument("notebook", metavar="NAME")
@click.argument(
    "command_parts",
    metavar="COMMAND...",
    nargs=-1,
    type=click.UNPROCESSED,
    required=True,
)
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
    "timeout",
    "--timeout",
    type=click.IntRange(1),
    default=_DEFAULT_EXEC_TIMEOUT,
    show_default=True,
    help="Command timeout in seconds",
)
@click.option(
    "--cwd",
    default=None,
    help="Remote working directory or path alias (default: 'me' alias, else $HOME)",
)
@click.option(
    "stdin_mode",
    "--stdin",
    "--bash-stdin",
    is_flag=True,
    help="Pass local stdin through to the remote command over SSH",
)
@pass_context
def exec_command(
    ctx: Context,
    notebook: str,
    command_parts: tuple[str, ...],
    workspace: str | None,
    account: str | None,
    pick: int | None,
    ignore_target_cache: bool,
    timeout: int,
    cwd: Optional[str],
    stdin_mode: bool,
) -> None:
    """Execute a notebook command; SSH when allowed, otherwise JupyterTerminal.

    NOTEBOOK is the notebook name. Each call runs an independent remote shell command;
    use one quoted command string when cwd, environment variables, or shell
    state must stay together.

    COMMAND is the shell command to run remotely (in --cwd, the `me` path alias, or $HOME).
    Command output (stdout/stderr) is automatically displayed after completion.

    \b
    Examples:
        inspire notebook exec my-notebook --cwd me:repo "uv venv .venv"
        inspire notebook exec my-notebook --cwd me "pwd"
        inspire notebook exec my-notebook --cwd me:repo "pip install torch" --timeout 600
        inspire notebook exec my-notebook --stdin -- bash -s < scripts/setup.sh
        inspire notebook exec my-notebook "hostname"
    """
    from inspire.cli.utils.id_resolver import reject_id_at_boundary

    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    command = _normalize_exec_command(command_parts)

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        remote_cwd = _resolve_exec_remote_cwd(cwd=cwd, config=config)
    except ConfigError as e:
        _emit_error(
            ctx,
            "ConfigError",
            str(e),
            EXIT_CONFIG_ERROR,
            human_lines=[f"Configuration error: {e}"],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        env_exports = build_env_exports(config.remote_env)
    except ConfigError as e:
        _emit_error(
            ctx,
            "ConfigError",
            str(e),
            EXIT_CONFIG_ERROR,
            human_lines=[f"Configuration error: {e}"],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    policy = preflight_notebook_transport_policy(
        ctx,
        notebook=notebook,
        workspace=workspace,
        account=account,
        pick=pick,
    )

    if policy.exec_transport == "jupyter":
        sys.exit(
            try_exec_via_jupyter_terminal(
                ctx,
                notebook_id=policy.notebook_id,
                command=command,
                session=policy.session,
                remote_cwd=remote_cwd,
                env_exports=env_exports,
                timeout_s=timeout,
            )
        )
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
        bridge = notebook
        tunnel_account = explicit_account
    else:
        bridge = target.bridge.name
        tunnel_account = target.account

    effective_stdin_mode = stdin_mode or _should_auto_passthrough_stdin()
    sys.exit(
        try_exec_via_ssh_tunnel(
            ctx,
            command=command,
            bridge_name=bridge,
            tunnel_account=tunnel_account,
            stdin_mode=effective_stdin_mode,
            config=config,
            remote_cwd=remote_cwd,
            env_exports=env_exports,
            timeout_s=timeout,
            is_tunnel_available_fn=is_tunnel_available,
            run_ssh_command_fn=run_ssh_command,
            run_ssh_command_streaming_fn=run_ssh_command_streaming,
        )
    )
