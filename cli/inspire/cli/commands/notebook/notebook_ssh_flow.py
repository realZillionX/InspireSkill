"""Notebook SSH and rtunnel setup flow."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_TIMEOUT
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    load_config,
    require_web_session,
)
from inspire.cli.utils.output import emit_success as emit_output_success
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.terminal_io import run_interactive_pty
from inspire.cli.utils.tunnel_reconnect import (
    NotebookBridgeReconnectState,
    NotebookBridgeReconnectStatus,
    attempt_notebook_bridge_rebuild,
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError

from .notebook_lookup import (
    _collect_workspace_ids_for_lookup,
    _get_current_user_detail,
    _notebook_compute_group,
    _resolve_notebook_id,
    _validate_notebook_account_access,
    _workspace_label,
)
from .target_resolver import (
    remember_notebook_target_aliases,
    resolve_cached_notebook_target,
)
from .transport import (
    gpu_model_supports_ssh,
    require_notebook_gpu_model,
    restricted_gpu_label,
)

logger = logging.getLogger(__name__)


def _describe_proxy_http_status(proxy_url: str, timeout_s: float = 4.0) -> str:
    parsed = urllib_parse.urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"}:
        return "not available"

    request = urllib_request.Request(proxy_url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout_s) as response:
            return f"HTTP {response.status}"
    except urllib_error.HTTPError as error:
        return f"HTTP {error.code}"
    except Exception:
        return "unreachable"


def load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    return load_ssh_public_key_material(pubkey_path)


def _identity_file_for_pubkey(pubkey_path: Optional[str] = None) -> str | None:
    candidates: list[Path] = []
    if pubkey_path:
        pubkey = Path(pubkey_path).expanduser()
        if pubkey.name.endswith(".pub"):
            candidates.append(pubkey.with_name(pubkey.name[:-4]))
        candidates.append(pubkey)
    else:
        candidates.extend(
            [
                Path.home() / ".ssh" / "id_ed25519",
                Path.home() / ".ssh" / "id_rsa",
            ]
        )
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        except OSError:
            # A Windows profile may contain an inherited or policy-protected
            # .ssh path. A missing usable private key is non-fatal here: SSH
            # can still use ssh-agent or an explicitly supplied key.
            continue
    return None


def _command_timeout_seconds(command_timeout: Optional[int]) -> Optional[int]:
    if command_timeout is None:
        return 300
    if int(command_timeout) <= 0:
        return None
    return int(command_timeout)


def _cached_bridge_for_identifier(
    *,
    identifier: str,
    account: Optional[str] = None,
    pick: int | None = None,
):
    from inspire.bridge.tunnel import load_tunnel_config

    normalized = str(identifier or "").strip()
    if not normalized:
        return None, None, None
    tunnel_account = account
    # An explicit pick must be resolved against the complete candidate set by
    # ``resolve_cached_notebook_target`` or the live notebook lookup. This
    # A single-account fast path cannot safely choose among duplicates.
    if pick is not None:
        return None, None, tunnel_account

    tunnel_config = load_tunnel_config(account=account) if account else load_tunnel_config()
    for bridge in tunnel_config.bridges.values():
        notebook_name = str(getattr(bridge, "notebook_name", "") or "").strip()
        notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
        if notebook_name and notebook_name == normalized and notebook_id:
            return bridge, notebook_id, tunnel_account
    return None, None, tunnel_account


def _command_failure_hint(command: str, exit_code: int) -> str | None:
    if exit_code == 1 and re.search(r"\bgrep\b", command):
        return "grep returns exit code 1 when no matches are found."
    return None


def _extract_notebook_stop_reason(events: str) -> str | None:
    lines = [line.strip() for line in str(events or "").splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if "notebook stopped because" in lowered:
            return line
    for line in reversed(lines):
        lowered = line.lower()
        if "stopped because" in lowered:
            return line
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith("notebook stopped") or " notebook stopped" in lowered:
            return line
    return None


def _workspace_name_for_hint(
    *,
    session,
    explicit_workspace: str | None,
    resolved_workspace_id: str | None,
) -> str | None:
    if explicit_workspace:
        return explicit_workspace
    if not resolved_workspace_id:
        return None
    label = _workspace_label(session, resolved_workspace_id)
    if label.startswith("("):
        return None
    return label


def _stopped_notebook_hint(
    *,
    notebook_name: str,
    workspace_name: str | None,
    events: str,
) -> str:
    workspace = workspace_name or "<workspace>"
    start_cmd = (
        "inspire notebook start "
        f"{shlex.quote(notebook_name)} --workspace {shlex.quote(workspace)} --wait"
    )
    retry_cmd = (
        "inspire notebook ssh "
        f"{shlex.quote(notebook_name)} --workspace {shlex.quote(workspace)}"
    )
    parts: list[str] = []
    stop_reason = _extract_notebook_stop_reason(events)
    if stop_reason:
        parts.append(f"Stop reason: {stop_reason}")
    parts.append("Start it first:")
    parts.append(f"  {start_cmd}")
    parts.append("Then retry:")
    parts.append(f"  {retry_cmd}")
    return "\n".join(parts)


def _should_retry_non_interactive_disconnect(
    *,
    returncode: int,
    profile_name: str,
    tunnel_account: Optional[str],
) -> bool:
    from inspire.bridge.tunnel import is_tunnel_available, load_tunnel_config

    if not should_attempt_ssh_reconnect(
        returncode,
        interactive=False,
        allow_non_interactive=True,
    ):
        return False

    try:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        tunnel_ready = is_tunnel_available(
            bridge_name=profile_name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
    except Exception:
        return False

    return not tunnel_ready


def _reconnect_session_loader(ctx: Context, tunnel_account: Optional[str]):
    def _load_session():
        if tunnel_account:
            return require_web_session(ctx, hint=WEB_AUTH_HINT, account=tunnel_account)
        return require_web_session(ctx, hint=WEB_AUTH_HINT)

    return _load_session


def _load_config_for_account(ctx: Context, account: Optional[str]):
    if account:
        return load_config(ctx, account=account)
    return load_config(ctx)


def _get_base_url_for_account(account: Optional[str]) -> str:
    if account:
        return get_base_url(account=account)
    return get_base_url()


def _run_notebook_command_with_reconnect(
    ctx: Context,
    *,
    profile_name: str,
    tunnel_account: Optional[str],
    session,
    session_loader=None,
    pubkey: Optional[str],
    command: str,
    command_timeout: Optional[int],
    debug_playwright: bool,
    setup_timeout: int,
    tunnel_retries: int,
    tunnel_retry_pause: float,
) -> None:
    from inspire.bridge.tunnel import (
        load_tunnel_config,
        run_ssh_command,
        run_ssh_command_streaming,
    )

    reconnect_limit = max(0, int(tunnel_retries))
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=tunnel_retry_pause,
    )
    timeout_s = _command_timeout_seconds(command_timeout)

    def _load_reconnect_session():
        nonlocal session
        if session is None:
            if session_loader is None:
                raise ConfigError("Cannot rebuild notebook tunnel without a web session.")
            session = session_loader()
        return session

    def _attempt_rebuild() -> bool:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        bridge = tunnel_config.get_bridge(profile_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"No cached notebook connection for '{scrub_raw_ids(profile_name)}'.",
                EXIT_CONFIG_ERROR,
                hint="Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh this notebook connection.",
            )
            return False

        attempt = reconnect_state.reconnect_attempt + 1
        logger.debug(
            "Notebook SSH tunnel rebuild scheduled after disconnect attempt=%s/%s",
            attempt,
            reconnect_limit,
        )

        reconnect_result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=profile_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=_load_reconnect_session,
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=lambda path: load_ssh_public_key(path),
            pubkey_path=pubkey,
            timeout=setup_timeout,
            headless=not debug_playwright,
        )

        if reconnect_result.status is NotebookBridgeReconnectStatus.REBUILT:
            return True

        if reconnect_result.status is NotebookBridgeReconnectStatus.RETRY_LATER:
            if reconnect_result.pause_seconds > 0:
                time.sleep(reconnect_result.pause_seconds)
            return True

        if reconnect_result.status is NotebookBridgeReconnectStatus.NOT_REBUILDABLE:
            _handle_error(
                ctx,
                "ConfigError",
                f"Cached notebook '{scrub_raw_ids(profile_name)}' is missing notebook metadata.",
                EXIT_CONFIG_ERROR,
                hint="Re-run 'inspire notebook connection refresh <notebook> --workspace <workspace>' to re-bootstrap.",
            )
            return False

        if isinstance(reconnect_result.error, (ValueError, ConfigError)):
            _handle_error(
                ctx,
                "ConfigError",
                str(reconnect_result.error),
                EXIT_CONFIG_ERROR,
            )
            return False

        _handle_error(
            ctx,
            "APIError",
            (
                f"Failed to rebuild notebook tunnel after disconnect: {reconnect_result.error}"
                if reconnect_result.error
                else "SSH connection dropped and auto-reconnect retries were exhausted."
            ),
            EXIT_API_ERROR,
            hint="Re-run 'inspire notebook connection refresh <notebook> --workspace <workspace>' to refresh the tunnel.",
        )
        return False

    while True:
        tunnel_config = load_tunnel_config(account=tunnel_account)

        try:
            if ctx.json_output:
                result = run_ssh_command(
                    command=command,
                    bridge_name=profile_name,
                    config=tunnel_config,
                    timeout=timeout_s,
                    capture_output=True,
                    pass_stdin=True,
                )
                output = f"{result.stdout or ''}{result.stderr or ''}"
                if result.returncode == 0:
                    emit_output_success(
                        ctx,
                        payload={
                            "status": "success",
                            "returncode": result.returncode,
                            "output": output,
                        },
                    )
                    return

                if _should_retry_non_interactive_disconnect(
                    returncode=result.returncode,
                    profile_name=profile_name,
                    tunnel_account=tunnel_account,
                ):
                    if _attempt_rebuild():
                        continue
                    return

                click.echo(
                    json_formatter.format_json(
                        {
                            "status": "failed",
                            "returncode": result.returncode,
                            "output": output,
                        },
                        success=False,
                    )
                )
                raise SystemExit(result.returncode)

            logger.debug("Notebook SSH command started")
            exit_code = run_ssh_command_streaming(
                command=command,
                bridge_name=profile_name,
                config=tunnel_config,
                timeout=timeout_s,
                pass_stdin=True,
            )
            if exit_code == 0:
                return

            if _should_retry_non_interactive_disconnect(
                returncode=exit_code,
                profile_name=profile_name,
                tunnel_account=tunnel_account,
            ):
                if _attempt_rebuild():
                    continue
                return

            _handle_error(
                ctx,
                "CommandFailed",
                f"Command failed with exit code {exit_code}.",
                exit_code,
                hint=_command_failure_hint(command, exit_code),
            )
            return
        except subprocess.TimeoutExpired:
            timeout_label = timeout_s if timeout_s is not None else "configured"
            _handle_error(
                ctx,
                "Timeout",
                f"Command timed out after {timeout_label}s.",
                EXIT_TIMEOUT,
                hint=(
                    "Retry with '--command-timeout <seconds>' for a longer limit, "
                    "or use '--command-timeout 0' to disable the limit."
                ),
            )
            return


def _run_interactive_notebook_ssh_with_reconnect(
    ctx: Context,
    *,
    profile_name: str,
    tunnel_account: Optional[str],
    session,
    session_loader=None,
    pubkey: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
    tunnel_retries: int,
    tunnel_retry_pause: float,
) -> None:
    from inspire.bridge.tunnel import (
        get_ssh_command_args,
        is_tunnel_available,
        load_tunnel_config,
    )

    reconnect_limit = max(0, int(tunnel_retries))
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=tunnel_retry_pause,
    )

    def _load_reconnect_session():
        nonlocal session
        if session is None:
            if session_loader is None:
                raise ConfigError("Cannot rebuild notebook tunnel without a web session.")
            session = session_loader()
        return session

    while True:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        bridge = tunnel_config.get_bridge(profile_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"No cached notebook connection for '{scrub_raw_ids(profile_name)}'.",
                EXIT_CONFIG_ERROR,
                hint="Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh this notebook connection.",
            )
            return

        args = get_ssh_command_args(bridge_name=profile_name, config=tunnel_config)
        try:
            returncode = run_interactive_pty(args)
        except KeyboardInterrupt:
            raise SystemExit(130) from None

        if returncode == 0:
            return
        if not should_attempt_ssh_reconnect(returncode, interactive=True):
            raise SystemExit(returncode if returncode is not None else 1)
        if reconnect_state.reconnect_attempt >= reconnect_limit:
            _handle_error(
                ctx,
                "APIError",
                "SSH connection dropped and auto-reconnect retries were exhausted.",
                EXIT_API_ERROR,
                hint="Re-run 'inspire notebook connection refresh <notebook> --workspace <workspace>' to refresh the tunnel.",
            )
            return

        attempt = reconnect_state.reconnect_attempt + 1
        logger.debug(
            "Interactive notebook SSH tunnel rebuild scheduled attempt=%s/%s",
            attempt,
            reconnect_limit,
        )

        reconnect_result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=profile_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=_load_reconnect_session,
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=lambda path: load_ssh_public_key(path),
            pubkey_path=pubkey,
            timeout=setup_timeout,
            headless=not debug_playwright,
        )

        if isinstance(reconnect_result.error, (ValueError, ConfigError)):
            _handle_error(
                ctx,
                "ConfigError",
                str(reconnect_result.error),
                EXIT_CONFIG_ERROR,
            )
            return

        if reconnect_result.status is NotebookBridgeReconnectStatus.RETRY_LATER:
            if reconnect_result.pause_seconds > 0:
                time.sleep(reconnect_result.pause_seconds)
            continue

        if reconnect_result.status is NotebookBridgeReconnectStatus.NOT_REBUILDABLE:
            _handle_error(
                ctx,
                "ConfigError",
                f"Cached notebook '{scrub_raw_ids(profile_name)}' is missing notebook metadata.",
                EXIT_CONFIG_ERROR,
                hint="Re-run 'inspire notebook connection refresh <notebook> --workspace <workspace>' to re-bootstrap.",
            )
            return

        if reconnect_result.status is NotebookBridgeReconnectStatus.EXHAUSTED:
            if reconnect_result.error is not None:
                _handle_error(
                    ctx,
                    "APIError",
                    f"Failed to rebuild notebook tunnel after disconnect: {reconnect_result.error}",
                    EXIT_API_ERROR,
                )
                return
            _handle_error(
                ctx,
                "APIError",
                "SSH connection dropped and auto-reconnect retries were exhausted.",
                EXIT_API_ERROR,
                hint="Re-run 'inspire notebook connection refresh <notebook> --workspace <workspace>' to refresh the tunnel.",
            )
            return

        refreshed_config = load_tunnel_config(account=tunnel_account)
        if is_tunnel_available(
            bridge_name=profile_name,
            config=refreshed_config,
            retries=3,
            retry_pause=1.0,
        ):
            continue
        if reconnect_state.reconnect_attempt >= reconnect_limit:
            _handle_error(
                ctx,
                "APIError",
                "Tunnel rebuild completed, but SSH preflight still failed.",
                EXIT_API_ERROR,
                hint=f"Run 'inspire notebook connection status {scrub_raw_ids(profile_name)}' for diagnostics.",
            )
            return

        pause_s = retry_pause_seconds(
            reconnect_state.reconnect_attempt,
            base_pause=tunnel_retry_pause,
            progressive=True,
        )
        if pause_s > 0:
            time.sleep(pause_s)


def run_notebook_ssh(
    ctx: Context,
    *,
    notebook_id: str,
    workspace: Optional[str],
    wait: bool,
    pubkey: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    command_timeout: Optional[int] = None,
    debug_playwright: bool,
    setup_timeout: int,
    setup_only: bool = False,
    account: Optional[str] = None,
    ignore_target_cache: bool = False,
    pick: int | None = None,
) -> None:
    from inspire.bridge.tunnel import (
        BridgeProfile,
        is_tunnel_available,
        load_tunnel_config,
        save_tunnel_config,
    )

    notebook_id = reject_id_at_boundary(
        ctx,
        notebook_id,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    explicit_tunnel_account = (
        str(account or "").strip()
        if str(account or "").strip()
        else None
    )

    cached_target = resolve_cached_notebook_target(
        ctx,
        notebook=notebook_id,
        workspace=workspace,
        account=account,
        ignore_target_cache=ignore_target_cache,
        verify_target_cache=True,
        allow_prompt=not ctx.json_output,
        pick=pick,
    )
    if cached_target is not None:
        config = _load_config_for_account(ctx, cached_target.account)
        cached_profile_name = cached_target.bridge.name
        tunnel_account = cached_target.account
        logger.debug("Using cached notebook SSH connection")
        if setup_only:
            return
        if command is None:
            _run_interactive_notebook_ssh_with_reconnect(
                ctx,
                profile_name=cached_profile_name,
                tunnel_account=tunnel_account,
                session=None,
                session_loader=_reconnect_session_loader(ctx, tunnel_account),
                pubkey=pubkey,
                debug_playwright=debug_playwright,
                setup_timeout=setup_timeout,
                tunnel_retries=config.tunnel_retries,
                tunnel_retry_pause=config.tunnel_retry_pause,
            )
            return
        _run_notebook_command_with_reconnect(
            ctx,
            profile_name=cached_profile_name,
            tunnel_account=tunnel_account,
            session=None,
            session_loader=_reconnect_session_loader(ctx, tunnel_account),
            pubkey=pubkey,
            command=command,
            command_timeout=command_timeout,
            debug_playwright=debug_playwright,
            setup_timeout=setup_timeout,
            tunnel_retries=config.tunnel_retries,
            tunnel_retry_pause=config.tunnel_retry_pause,
        )
        return

    if explicit_tunnel_account:
        session = require_web_session(ctx, hint=WEB_AUTH_HINT, account=explicit_tunnel_account)
    else:
        session = require_web_session(ctx, hint=WEB_AUTH_HINT)

    base_url = _get_base_url_for_account(explicit_tunnel_account)
    config = _load_config_for_account(ctx, explicit_tunnel_account)
    from inspire.config.workspaces import resolve_workspace_query_scope

    requested_identifier = notebook_id
    cached_bridge, cached_notebook_id, tunnel_account = _cached_bridge_for_identifier(
        identifier=notebook_id,
        account=explicit_tunnel_account,
        pick=pick,
    )
    explicit_workspace = str(workspace or "").strip() or None
    if explicit_workspace:
        workspace_ids, _ = resolve_workspace_query_scope(
            workspace=explicit_workspace,
            session=session,
        )
    elif cached_bridge and getattr(cached_bridge, "workspace_id", None):
        workspace_ids = [str(cached_bridge.workspace_id)]
    else:
        workspace_ids = _collect_workspace_ids_for_lookup(session)

    logger.debug("Resolving notebook target by name")

    resolved_workspace_id: str | None = (
        str(getattr(cached_bridge, "workspace_id", "") or "").strip() or None
    )
    if cached_notebook_id:
        notebook_id = cached_notebook_id
    else:
        notebook_id, resolved_workspace_id = _resolve_notebook_id(
            ctx,
            session=session,
            base_url=base_url,
            identifier=notebook_id,
            json_output=False,
            workspace_ids=workspace_ids,
            pick=pick,
        )
    if explicit_workspace and workspace_ids:
        resolved_workspace_id = workspace_ids[0]

    logger.debug("Fetching notebook state for SSH setup")
    try:
        if wait:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
        else:
            notebook_detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
    except NotebookFailedError as e:
        if str(e.status or "").upper() == "STOPPED":
            stopped_name = (
                str(e.detail.get("name") or "").strip()
                or str(requested_identifier or "").strip()
                or "the requested notebook"
            )
            stopped_workspace = _workspace_name_for_hint(
                session=session,
                explicit_workspace=explicit_workspace,
                resolved_workspace_id=resolved_workspace_id,
            )
            _handle_error(
                ctx,
                "NotebookStopped",
                f"Notebook is stopped: {stopped_name}",
                EXIT_API_ERROR,
                hint=_stopped_notebook_hint(
                    notebook_name=stopped_name,
                    workspace_name=stopped_workspace,
                    events=e.events,
                ),
            )
            return
        _handle_error(
            ctx,
            "NotebookFailed",
            f"Notebook failed to start: {e}",
            EXIT_API_ERROR,
            hint=e.events or "Check the platform Events tab for details.",
        )
        return
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    notebook_display_name = str(notebook_detail.get("name") or "").strip()
    if not notebook_display_name:
        # If the platform returns a notebook with no name, the cache would
        # have to fall back to a handle-shaped key, which would then violate
        # the name-only-at-user-boundary contract the cached-tunnel
        # commands (`shell` / `exec` / `scp`) rely on. Surface a clear
        # error instead of silently fabricating an `nb-<hex>` cache key.
        _handle_error(
            ctx,
            "ConfigError",
            "Notebook has no display name; cannot create a cached SSH connection.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Rename the notebook in the platform UI (or recreate it with "
                "`inspire notebook create -n <name> ...`) so it has a name to "
                "use as the cache key."
            ),
        )
        return
    profile_name = notebook_display_name

    current_user_detail: dict = {}
    try:
        current_user_detail = _get_current_user_detail(session, base_url=base_url)
    except Exception:
        current_user_detail = {}

    allowed, _reason = _validate_notebook_account_access(
        current_user=current_user_detail,
        notebook_detail=notebook_detail,
    )
    if not allowed:
        _handle_error(
            ctx,
            "ConfigError",
            "Notebook/account mismatch detected before tunnel setup.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Retry with `--account <name>`, or add the owning account with "
                "`inspire account add <name>`."
            ),
        )
        return

    # Last gate before the tunnel is built: callers that skip the preflight
    # (`notebook ssh` without --workspace) must not reach an H100/H200
    # notebook, and no bridge may be cached for one. The preflight's probe is
    # remembered per compute group, so this repeats no remote work when it ran.
    gpu_model = require_notebook_gpu_model(
        ctx,
        notebook=notebook_display_name,
        notebook_id=notebook_id,
        compute_group=_notebook_compute_group(notebook_detail),
        session=session,
    )
    if not gpu_model_supports_ssh(gpu_model):
        _handle_error(
            ctx,
            "PolicyBlocked",
            (
                "SSH/rtunnel access is blocked on H100/H200 notebooks: "
                f"{scrub_raw_ids(notebook_display_name)} runs "
                f"{restricted_gpu_label(gpu_model)}"
            ),
            EXIT_CONFIG_ERROR,
            hint=(
                "Use `inspire notebook exec` or `inspire notebook shell`; "
                "restricted notebooks use JupyterTerminal instead of SSH/rtunnel."
            ),
        )
        return

    try:
        ssh_public_key = load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    identity_file = _identity_file_for_pubkey(pubkey)

    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except browser_api_module.OpenSSHInternalInstallError:
        _handle_error(
            ctx,
            "SetupError",
            "SSH bootstrap 失败：OpenSSH 需要通过 SII 内部 Ubuntu apt 源安装或校正。",
            EXIT_API_ERROR,
            hint=(
                "SSH bootstrap 会在 notebook 容器内读取 `/etc/os-release` 的 Ubuntu "
                "`VERSION_CODENAME`，再临时使用对应 suite 的 SII 内部 Ubuntu apt 源安装"
                "或校正 OpenSSH。请确认 notebook 能访问 "
                f"`{browser_api_module.SII_UBUNTU_APT_MIRROR}` 后重新执行 "
                "`inspire notebook connection refresh <name> --workspace <workspace>`；"
                f"远端日志在 `{browser_api_module.OPENSSH_INSTALL_LOG}`。"
            ),
        )
        return
    except browser_api_module.RtunnelMissingInContainerError:
        # Structured failure: the offline SSH-bootstrap kit isn't reachable
        # from this container (neither /tmp/rtunnel was produced from the kit
        # cp step nor did a runnable rtunnel appear). The kit lives at a
        # fixed global_public path and is expected to be mounted in every
        # notebook; if you see this, the platform mount is missing.
        _handle_error(
            ctx,
            "SetupError",
            "SSH bootstrap 失败：在容器里没能从 global_public kit 拿到 rtunnel。",
            EXIT_API_ERROR,
            hint=(
                "检查：\n"
                "  1. `ls /inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-amd64/rtunnel`\n"
                "     在容器里应当存在且可执行。\n"
                "  2. 如果这条路径不存在 / 不可读，说明平台侧的 global_public 挂载\n"
                "     没覆盖到这台实例——这不是 InspireSkill 的问题，找 SII 运维。\n"
                "  3. 如果 kit 存在但 bootstrap 仍失败，跑 `inspire --debug notebook connection refresh ...`\n"
                "     看 trace 再开 issue。"
            ),
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to set up notebook tunnel: {e}", EXIT_API_ERROR)
        return

    resolved_workspace_name = explicit_workspace
    if not resolved_workspace_name and resolved_workspace_id:
        label = _workspace_label(session, resolved_workspace_id)
        if not label.startswith("("):
            resolved_workspace_name = label

    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        notebook_id=notebook_id,
        notebook_name=str(notebook_detail.get("name") or "").strip() or None,
        workspace_id=resolved_workspace_id,
        workspace_name=resolved_workspace_name,
        identity_file=identity_file,
        rtunnel_port=port,
    )

    tunnel_config = load_tunnel_config(account=tunnel_account)
    tunnel_config.add_bridge(bridge)
    save_tunnel_config(tunnel_config)

    if not is_tunnel_available(
        bridge_name=profile_name,
        config=tunnel_config,
        retries=6,
        retry_pause=1.5,
    ):
        proxy_status = _describe_proxy_http_status(proxy_url)
        allow_ssh = None
        start_config = notebook_detail.get("start_config")
        if isinstance(start_config, dict):
            allow_ssh = start_config.get("allow_ssh")

        ssh_capability_hint = ""
        if allow_ssh is False:
            ssh_capability_hint = (
                " Notebook runtime reports start_config.allow_ssh=false, which usually means "
                "the image does not include SSH tooling (sshd/dropbear/rtunnel)."
            )
        _handle_error(
            ctx,
            "APIError",
            "Tunnel setup completed, but SSH preflight failed.",
            EXIT_API_ERROR,
            hint=(
                f"Retry 'inspire notebook connection refresh {scrub_raw_ids(profile_name)} --workspace <workspace>' in a few seconds, "
                f"or run 'inspire notebook connection status {scrub_raw_ids(profile_name)}' to inspect connectivity. "
                f"Proxy readiness: {scrub_raw_ids(proxy_status)}."
                f"{ssh_capability_hint}"
            ),
        )
        return

    remember_notebook_target_aliases(
        requested_identifier=requested_identifier,
        workspace=explicit_workspace,
        account=tunnel_account,
        bridge=bridge,
    )

    logger.debug("Notebook SSH connection is ready")

    if setup_only:
        return

    if command is None:
        _run_interactive_notebook_ssh_with_reconnect(
            ctx,
            profile_name=profile_name,
            tunnel_account=tunnel_account,
            session=session,
            pubkey=pubkey,
            debug_playwright=debug_playwright,
            setup_timeout=setup_timeout,
            tunnel_retries=config.tunnel_retries,
            tunnel_retry_pause=config.tunnel_retry_pause,
        )
        return

    _run_notebook_command_with_reconnect(
        ctx,
        profile_name=profile_name,
        tunnel_account=tunnel_account,
        session=session,
        pubkey=pubkey,
        command=command,
        command_timeout=command_timeout,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
        tunnel_retries=config.tunnel_retries,
        tunnel_retry_pause=config.tunnel_retry_pause,
    )


__all__ = ["load_ssh_public_key", "run_notebook_ssh"]
