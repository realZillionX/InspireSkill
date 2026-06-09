"""`notebook exec` command -- execute a shell command on a cached notebook."""

from __future__ import annotations

import os
import logging
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.config import Config, ConfigError, build_env_exports, resolve_remote_cwd
from inspire.bridge.forge import (
    ForgeAuthError,
    ForgeError,
    trigger_bridge_action_workflow,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
)
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
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.output import (
    emit_error as emit_output_error,
    emit_success as emit_output_success,
)
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

from .target_resolver import resolve_cached_notebook_target

logger = logging.getLogger(__name__)
_RUNNING_NOTEBOOK_STATUS = "RUNNING"


def split_denylist(items: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    for raw in items:
        for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


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


def _verbose_output(ctx: Context) -> bool:
    return not ctx.json_output and ctx.debug


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
) -> Optional[int]:
    """Attempt the fast-path SSH tunnel execution.

    Returns:
        Exit code if the SSH path handled the request (success/failure/timeout),
        otherwise None to fall back to workflow execution.
    """
    reconnect_limit = max(0, int(getattr(config, "tunnel_retries", 0)))
    reconnect_pause = float(getattr(config, "tunnel_retry_pause", 0.0) or 0.0)
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=reconnect_pause,
    )
    resolved_bridge_name = bridge_name
    force_rebuild = False
    opened_once = False
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
                    "Skipping notebook status preflight bridge=%s notebook_id=%s error=%s",
                    bridge.name,
                    notebook_id,
                    status_error,
                )

        if not ctx.json_output:
            click.echo(
                (
                    f"{reason} "
                    f"(attempt {reconnect_state.reconnect_attempt + 1}/{reconnect_limit})..."
                ),
                err=True,
            )

        result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=bridge.name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: require_web_session(
                ctx,
                hint=WEB_AUTH_HINT,
                account=tunnel_account,
            )
            if tunnel_account
            else require_web_session(ctx, hint=WEB_AUTH_HINT),
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
            logger.debug("Skipping auto-retry after SSH 255: tunnel probe failed: %s", probe_error)
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
                            "method": "ssh_tunnel",
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

            if _verbose_output(ctx) and not opened_once:
                click.echo("Using SSH tunnel (fast path)")
                click.echo(f"Notebook: {scrub_raw_ids(resolved_bridge_name)}")
                click.echo(f"Command: {scrub_raw_ids(command)}")
                click.echo(f"Working dir: {scrub_raw_ids(remote_cwd or '$HOME')}")
                if stdin_mode:
                    click.echo("Stdin: passthrough")
                click.echo("--- Command Output ---")
                opened_once = True

            ssh_execution_started = True
            stream_kwargs: dict[str, object] = {
                "command": full_command,
                "bridge_name": resolved_bridge_name,
                "timeout": timeout_s,
            }
            if stdin_mode:
                stream_kwargs["pass_stdin"] = True
            exit_code = run_ssh_command_streaming_fn(**stream_kwargs)
            if _verbose_output(ctx):
                click.echo("--- End Output ---")

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
            emit_output_error(
                ctx,
                error_type="Timeout",
                message=f"Command timed out after {timeout_s}s",
                exit_code=EXIT_TIMEOUT,
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


def exec_via_workflow(
    ctx: Context,
    *,
    command: str,
    env_exports: str,
    denylist: tuple[str, ...],
    artifact_path: tuple[str, ...],
    download: Optional[str],
    wait: bool,
    timeout_s: int,
    config: Config,
    remote_cwd: Optional[str],
    trigger_bridge_action_workflow_fn: Callable[..., None],
    wait_for_bridge_action_completion_fn: Callable[..., dict],
    fetch_bridge_output_log_fn: Callable[..., Optional[str]],
    download_bridge_artifact_fn: Callable[..., None],
) -> int:
    workflow_command = f"{env_exports}{command}" if env_exports else command

    merged_denylist: list[str] = []
    if config.bridge_action_denylist:
        merged_denylist.extend(config.bridge_action_denylist)
    merged_denylist.extend(split_denylist(denylist))

    if not merged_denylist and _verbose_output(ctx):
        click.echo("Warning: no denylist provided; proceeding", err=True)

    request_id = f"{int(time.time())}-{os.getpid()}"
    artifact_paths_list = list(artifact_path)

    if _verbose_output(ctx):
        click.echo("Triggering bridge exec")
        click.echo(f"Command: {scrub_raw_ids(command)}")
        click.echo(f"Working dir: {scrub_raw_ids(remote_cwd or '$HOME')}")
        if merged_denylist:
            click.echo(f"Denylist: {scrub_raw_ids(merged_denylist)}")
        if artifact_paths_list:
            click.echo(f"Artifact paths: {scrub_raw_ids(artifact_paths_list)}")

    try:
        logger.debug(
            "bridge_exec workflow trigger request_id=%s wait=%s timeout=%s command=%s",
            request_id,
            wait,
            timeout_s,
            command,
        )
        trigger_bridge_action_workflow_fn(
            config=config,
            raw_command=workflow_command,
            remote_cwd=remote_cwd,
            artifact_paths=artifact_paths_list,
            request_id=request_id,
            denylist=merged_denylist,
        )
    except (ForgeError, ForgeAuthError) as e:
        emit_output_error(
            ctx,
            error_type="ForgeError",
            message=str(e),
            exit_code=EXIT_GENERAL_ERROR,
            human_lines=[f"Error: {e}"],
        )
        return EXIT_GENERAL_ERROR

    if not wait:
        emit_output_success(
            ctx,
            payload={
                "status": "triggered",
                "request_id": request_id,
                "command": command,
            },
            text="Triggered bridge exec request",
        )
        return EXIT_SUCCESS

    if _verbose_output(ctx):
        click.echo(f"Waiting for completion (timeout {timeout_s}s)...")

    try:
        result = wait_for_bridge_action_completion_fn(
            config=config,
            request_id=request_id,
            timeout=timeout_s,
        )
        logger.debug("bridge_exec workflow result request_id=%s result=%s", request_id, result)
    except TimeoutError as e:
        emit_output_error(
            ctx,
            error_type="Timeout",
            message=str(e),
            exit_code=EXIT_TIMEOUT,
            human_lines=[f"Timeout: {e}"],
        )
        return EXIT_TIMEOUT
    except ForgeError as e:
        emit_output_error(
            ctx,
            error_type="ForgeError",
            message=str(e),
            exit_code=EXIT_GENERAL_ERROR,
            human_lines=[f"Error: {e}"],
        )
        return EXIT_GENERAL_ERROR

    output_log: Optional[str] = None
    try:
        output_log = fetch_bridge_output_log_fn(config, request_id)
    except ForgeError:
        pass
    if output_log:
        logger.debug("bridge_exec workflow output request_id=%s\n%s", request_id, output_log)

    if output_log and not ctx.json_output:
        if _verbose_output(ctx):
            click.echo("")
            click.echo("--- Command Output ---")
            click.echo(scrub_raw_ids(output_log))
            click.echo("--- End Output ---")
            click.echo("")
        else:
            click.echo(scrub_raw_ids(output_log))

    if result.get("conclusion") != "success":
        hint = result.get("html_url") or None
        emit_output_error(
            ctx,
            error_type="BridgeActionFailed",
            message=f"Action failed: {result.get('conclusion')}",
            exit_code=EXIT_GENERAL_ERROR,
            hint=hint,
            human_lines=[
                f"Action failed: {result.get('conclusion')} (see {result.get('html_url', '')})"
            ],
        )
        return EXIT_GENERAL_ERROR

    if download:
        if _verbose_output(ctx):
            click.echo(f"Downloading artifact to {scrub_raw_ids(download)}...")
        try:
            download_bridge_artifact_fn(config, request_id, Path(download))
        except ForgeError as e:
            emit_output_error(
                ctx,
                error_type="ArtifactError",
                message=f"Artifact download failed: {e}",
                exit_code=EXIT_GENERAL_ERROR,
                human_lines=[f"Warning: artifact download failed: {e}"],
            )
            return EXIT_GENERAL_ERROR

    if _verbose_output(ctx):
        click.echo("OK Action completed successfully")
        if result.get("html_url"):
            click.echo(f"Workflow: {result.get('html_url')}")
        if download:
            click.echo("Artifacts downloaded")
    else:
        emit_output_success(
            ctx,
            payload={
                "status": "success",
                "request_id": request_id,
                "artifact_downloaded": bool(download),
                "output": output_log,
            },
            text="OK (artifacts downloaded)" if download else "OK",
        )

    return EXIT_SUCCESS


@click.command("exec")
@click.argument("notebook")
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
@click.option(
    "denylist",
    "--denylist",
    multiple=True,
    help="Denylist pattern to block (repeatable or comma-separated)",
)
@click.option(
    "artifact_path",
    "--artifact-path",
    multiple=True,
    help="Path relative to the remote working directory selected by --cwd or the 'me' path alias",
)
@click.option(
    "download",
    "--download",
    type=click.Path(),
    help="Local directory to download artifact contents",
)
@click.option("wait", "--wait/--no-wait", default=True, help="Wait for completion (default: wait)")
@click.option(
    "timeout",
    "--timeout",
    type=click.IntRange(1),
    default=None,
    help="Timeout in seconds (default: config value)",
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
    account: str | None,
    ignore_target_cache: bool,
    denylist: tuple[str, ...],
    artifact_path: tuple[str, ...],
    download: Optional[str],
    wait: bool,
    timeout: Optional[int],
    cwd: Optional[str],
    stdin_mode: bool,
) -> None:
    """Execute a command on a cached notebook.

    Requires `inspire notebook connection refresh <notebook> --workspace <workspace>`
    first. NOTEBOOK is the notebook name. Each call runs an independent remote shell command;
    use one quoted command string when cwd, environment variables, or shell
    state must stay together.

    COMMAND is the shell command to run remotely (in --cwd, the `me` path alias, or $HOME).
    Command output (stdout/stderr) is automatically displayed after completion.
    Artifact options can fetch generated paths after the command finishes.

    \b
    Examples:
        inspire notebook exec my-notebook --cwd me:repo "uv venv .venv"
        inspire notebook exec my-notebook --cwd me "pwd"
        inspire notebook exec my-notebook --cwd me:repo "pip install torch" --timeout 600
        inspire notebook exec my-notebook --stdin -- bash -s < scripts/setup.sh
        inspire notebook exec my-notebook --cwd me:repo "uv venv .venv" \\
            --artifact-path .venv --download ./local
        inspire notebook exec my-notebook --cwd me:repo "python train.py" --no-wait
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
        emit_output_error(
            ctx,
            error_type="ConfigError",
            message=str(e),
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=[f"Configuration error: {e}"],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        env_exports = build_env_exports(config.remote_env)
    except ConfigError as e:
        emit_output_error(
            ctx,
            error_type="ConfigError",
            message=str(e),
            exit_code=EXIT_CONFIG_ERROR,
            human_lines=[f"Configuration error: {e}"],
        )
        sys.exit(EXIT_CONFIG_ERROR)

    action_timeout = int(timeout) if timeout is not None else int(config.bridge_action_timeout)

    if stdin_mode and (artifact_path or download):
        emit_output_error(
            ctx,
            error_type="UsageError",
            message="--stdin/--bash-stdin is only supported for SSH execution (no artifacts).",
            exit_code=EXIT_GENERAL_ERROR,
            human_lines=[
                "--stdin/--bash-stdin cannot be combined with --artifact-path/--download."
            ],
        )
        sys.exit(EXIT_GENERAL_ERROR)

    # SSH tunnel is the default command transport when artifacts are not requested.
    if not artifact_path and not download:
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
            bridge = notebook
            tunnel_account = explicit_account
        else:
            bridge = target.bridge.name
            tunnel_account = target.account

        effective_stdin_mode = stdin_mode or _should_auto_passthrough_stdin()
        ssh_exit_code = try_exec_via_ssh_tunnel(
            ctx,
            command=command,
            bridge_name=bridge,
            tunnel_account=tunnel_account,
            stdin_mode=effective_stdin_mode,
            config=config,
            remote_cwd=remote_cwd,
            env_exports=env_exports,
            timeout_s=action_timeout,
            is_tunnel_available_fn=is_tunnel_available,
            run_ssh_command_fn=run_ssh_command,
            run_ssh_command_streaming_fn=run_ssh_command_streaming,
        )
        sys.exit(ssh_exit_code if ssh_exit_code is not None else EXIT_GENERAL_ERROR)

    workflow_exit_code = exec_via_workflow(
        ctx,
        command=command,
        env_exports=env_exports,
        denylist=denylist,
        artifact_path=artifact_path,
        download=download,
        wait=wait,
        timeout_s=action_timeout,
        config=config,
        remote_cwd=remote_cwd,
        trigger_bridge_action_workflow_fn=trigger_bridge_action_workflow,
        wait_for_bridge_action_completion_fn=wait_for_bridge_action_completion,
        fetch_bridge_output_log_fn=fetch_bridge_output_log,
        download_bridge_artifact_fn=download_bridge_artifact,
    )
    sys.exit(workflow_exit_code)
