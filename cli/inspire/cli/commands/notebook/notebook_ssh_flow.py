"""Notebook SSH and rtunnel setup flow."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_TIMEOUT
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import get_base_url, load_config, require_web_session
from inspire.cli.utils.output import emit_success as emit_output_success
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
from inspire.config.ssh_runtime import resolve_ssh_runtime_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.browser_api.rtunnel import redact_proxy_url

from .notebook_lookup import (
    _get_current_user_detail,
    _looks_like_notebook_id,
    _resolve_notebook_id,
    _validate_notebook_account_access,
)


def _format_proxy_http_body(raw: bytes) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    compact = " ".join(text.split())
    return compact[:180]


def _describe_proxy_http_status(proxy_url: str, timeout_s: float = 4.0) -> str:
    parsed = urllib_parse.urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"}:
        return "n/a (non-http proxy URL)"

    request = urllib_request.Request(proxy_url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout_s) as response:
            body = _format_proxy_http_body(response.read(220))
            return f"{response.status} {body}".strip()
    except urllib_error.HTTPError as error:
        try:
            body = _format_proxy_http_body(error.read(220))
        except Exception:
            body = ""
        return f"{error.code} {body}".strip()
    except Exception as error:
        return str(error)


def load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    return load_ssh_public_key_material(pubkey_path)


# ---------------------------------------------------------------------------
# Notebook-name-based default alias helpers
# ---------------------------------------------------------------------------
#
# Users who run ``inspire notebook ssh <id>`` without ``--save-as`` benefit
# from an alias that matches the notebook's display name rather than the
# opaque ``nb-<id[:8]>`` prefix: both ``inspire notebook connections`` and
# ``~/.ssh/config`` become self-documenting. The sanitised name is scoped to
# alias-safe characters so shells, ssh_config Host entries, and click flag
# values all stay well-formed.


_ALIAS_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ALIAS_COLLAPSE_DASH_RE = re.compile(r"-{2,}")
_ALIAS_MIN_LENGTH = 2


def _sanitize_alias_from_name(name: str) -> str:
    """Turn a free-form notebook display name into an alias-safe token.

    Keeps ``[A-Za-z0-9._-]``; everything else (spaces, CJK, emoji) becomes
    ``-``.  Consecutive dashes collapse to one, leading/trailing
    ``.-_`` are trimmed, and the result is lower-cased so aliases are
    case-insensitive in practice.  Returns ``""`` when nothing survives
    sanitisation — the caller falls back to ``nb-<id[:8]>`` in that case.
    """
    cleaned = _ALIAS_SAFE_CHAR_RE.sub("-", str(name or ""))
    cleaned = _ALIAS_COLLAPSE_DASH_RE.sub("-", cleaned)
    cleaned = cleaned.strip(".-_").lower()
    if len(cleaned) < _ALIAS_MIN_LENGTH:
        return ""
    return cleaned


def _find_alias_for_notebook_id(cached_config, notebook_id: str) -> Optional[str]:
    """Return any existing alias bound to *notebook_id*, or ``None``.

    Preserves pre-existing aliases across CLI upgrades: users who already
    have an ``nb-<id>`` alias cached won't silently get a duplicate
    name-based alias created alongside it.
    """
    target = str(notebook_id or "").strip()
    if not target:
        return None
    for name, bridge in cached_config.bridges.items():
        bridge_notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
        if bridge_notebook_id == target:
            return name
    return None


def _unique_alias_for_notebook(
    cached_config,
    *,
    base: str,
    notebook_id: str,
) -> str:
    """Return ``<base>-sh<N>`` — the first N ≥ 0 not already taken by another
    notebook.

    If the exact ``<base>-sh<N>`` is currently bound to *this* notebook, we
    return it as-is (idempotent reconnect). Otherwise we pick the smallest
    N whose ``<base>-sh<N>`` either is free or already belongs to this
    notebook — which also means ``<base>-sh0`` is the default for a
    fresh alias, and re-bootstrapping a notebook on a host that already
    owns ``-sh0`` keeps the same name.
    """
    for i in range(1_000):  # effectively unbounded; sanity cap
        candidate = f"{base}-sh{i}"
        existing = cached_config.bridges.get(candidate)
        if existing is None:
            return candidate
        existing_notebook_id = str(getattr(existing, "notebook_id", "") or "").strip()
        if existing_notebook_id == notebook_id:
            return candidate
    # Fallback — should never happen; keep the CLI alive rather than loop
    # forever on an obviously degenerate alias table.
    suffix = str(notebook_id or "").replace("notebook-", "")[:4] or "x"
    return f"{base}-sh{suffix}"


def _default_alias_for_notebook(
    cached_config,
    *,
    notebook_id: str,
    notebook_name: Optional[str],
) -> str:
    """Compute the default alias for a freshly-bootstrapped notebook.

    v2.0.0: always ``<cleaned-name>-sh<N>`` where N is the lowest free
    index (``-sh0`` for the first bootstrap of a given name). Prior versions
    used the bare cleaned name which made the second bootstrap of a same-named
    notebook pick up an id-based suffix; the -sh<N> scheme is stable across
    reconnects and survives display-name collisions cleanly.

    Falls back to ``nb-<id[:8]>`` when the display name sanitises away
    (still suffixed with ``-sh<N>``).
    """
    derived = _sanitize_alias_from_name(notebook_name or "")
    base = derived or f"nb-{str(notebook_id or '')[:8]}"
    return _unique_alias_for_notebook(
        cached_config,
        base=base,
        notebook_id=notebook_id,
    )


def _command_timeout_seconds(command_timeout: Optional[int]) -> Optional[int]:
    if command_timeout is None:
        return 300
    if int(command_timeout) <= 0:
        return None
    return int(command_timeout)


def _cached_bridge_for_identifier(
    *,
    identifier: str,
    config,
):
    from inspire.bridge.tunnel import load_tunnel_config

    normalized = str(identifier or "").strip()
    if not normalized:
        return None, None, None
    if _looks_like_notebook_id(normalized):
        return None, None, None

    tunnel_account = str(getattr(config, "username", "") or "").strip() or None
    tunnel_config = load_tunnel_config(account=tunnel_account)
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


def _run_notebook_command_with_reconnect(
    ctx: Context,
    *,
    profile_name: str,
    tunnel_account: Optional[str],
    session,
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
    announced_command_start = False

    def _runtime_loader() -> object:
        return resolve_ssh_runtime_config()

    def _attempt_rebuild() -> bool:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        bridge = tunnel_config.get_bridge(profile_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Notebook alias '{profile_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire notebook connections' to check saved notebook aliases.",
            )
            return False

        attempt = reconnect_state.reconnect_attempt + 1
        click.echo(
            (
                "SSH connection dropped; rebuilding tunnel automatically "
                f"(attempt {attempt}/{reconnect_limit})..."
            ),
            err=True,
        )

        reconnect_result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=profile_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: session,
            runtime_loader=_runtime_loader,
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
                f"Notebook alias '{profile_name}' is missing notebook metadata.",
                EXIT_CONFIG_ERROR,
                hint="Re-run 'inspire notebook ssh <notebook-id> --save-as <name>'.",
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
            hint="Re-run 'inspire notebook ssh <notebook-id>' to refresh the tunnel.",
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
                )
                output = f"{result.stdout or ''}{result.stderr or ''}"
                if result.returncode == 0:
                    emit_output_success(
                        ctx,
                        payload={
                            "status": "success",
                            "method": "ssh_tunnel",
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
                            "method": "ssh_tunnel",
                            "returncode": result.returncode,
                            "output": output,
                        },
                        success=False,
                    )
                )
                raise SystemExit(result.returncode)

            if not announced_command_start:
                click.echo("Running remote command...", err=True)
                announced_command_start = True

            exit_code = run_ssh_command_streaming(
                command=command,
                bridge_name=profile_name,
                config=tunnel_config,
                timeout=timeout_s,
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

    def _runtime_loader() -> object:
        return resolve_ssh_runtime_config()

    def _runtime_validator(runtime: object) -> None:
        del runtime
        pass

    while True:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        bridge = tunnel_config.get_bridge(profile_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Notebook alias '{profile_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire notebook connections' to check saved notebook aliases.",
            )
            return

        args = get_ssh_command_args(bridge_name=profile_name, config=tunnel_config)
        try:
            returncode = subprocess.call(args)
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
                hint="Re-run 'inspire notebook ssh <notebook-id>' to refresh the tunnel.",
            )
            return

        attempt = reconnect_state.reconnect_attempt + 1
        click.echo(
            (
                "SSH connection dropped; rebuilding tunnel automatically "
                f"(attempt {attempt}/{reconnect_limit})..."
            ),
            err=True,
        )

        reconnect_result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=profile_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: session,
            runtime_loader=_runtime_loader,
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=lambda path: load_ssh_public_key(path),
            runtime_validator=_runtime_validator,
            pubkey_path=pubkey,
            timeout=setup_timeout,
            headless=not debug_playwright,
        )

        if isinstance(reconnect_result.error, (ValueError, ConfigError)):
            hint = None
            if "setup_script" in str(reconnect_result.error):
                hint = (
                    "Set [ssh].setup_script in config.toml or export INSPIRE_SETUP_SCRIPT "
                    "to the setup script path on the cluster."
                )
            _handle_error(
                ctx,
                "ConfigError",
                str(reconnect_result.error),
                EXIT_CONFIG_ERROR,
                hint=hint,
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
                f"Notebook alias '{profile_name}' is missing notebook metadata.",
                EXIT_CONFIG_ERROR,
                hint="Re-run 'inspire notebook ssh <notebook-id> --save-as <name>'.",
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
                hint="Re-run 'inspire notebook ssh <notebook-id>' to refresh the tunnel.",
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
                hint=f"Run 'inspire notebook test -a {profile_name}' for diagnostics.",
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
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    command_timeout: Optional[int] = None,
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    from inspire.bridge.tunnel import (
        BridgeProfile,
        get_ssh_command_args,
        has_internet_for_gpu_type,
        is_tunnel_available,
        load_tunnel_config,
        save_tunnel_config,
    )

    session = require_web_session(
        ctx,
        hint=(
            "Notebook SSH requires web authentication. "
            "Set [auth].username and configure password via INSPIRE_PASSWORD "
            'or [accounts."<username>"].password.'
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    requested_identifier = notebook_id
    cached_bridge, cached_notebook_id, tunnel_account = _cached_bridge_for_identifier(
        identifier=notebook_id,
        config=config,
    )

    if not ctx.json_output:
        click.echo("Resolving notebook target...", err=True)

    if cached_notebook_id:
        notebook_id = cached_notebook_id
    else:
        notebook_id, _ = _resolve_notebook_id(
            ctx,
            session=session,
            config=config,
            base_url=base_url,
            identifier=notebook_id,
            json_output=False,
        )

    # Alias resolution:
    #   1. ``--save-as`` wins if the user asked for a specific label.
    #   2. Else, reuse any alias already bound to this ``notebook_id`` — this
    #      keeps legacy ``nb-<id[:8]>`` bridges from users who ran earlier CLI
    #      versions working without forcing a rename.
    #   3. Else, defer: after we fetch ``notebook_detail`` below we derive an
    #      alias from the notebook's display name (and only fall back to
    #      ``nb-<id[:8]>`` when the name sanitises to something too short).
    cached_config = load_tunnel_config(account=tunnel_account)
    profile_name: Optional[str] = save_as or _find_alias_for_notebook_id(
        cached_config, notebook_id
    )

    if profile_name and profile_name in cached_config.bridges:
        cached_bridge = cached_config.bridges[profile_name]
        bridge_notebook_id = str(getattr(cached_bridge, "notebook_id", "") or "").strip()
        if bridge_notebook_id == notebook_id:
            test_args = get_ssh_command_args(
                bridge_name=profile_name,
                config=cached_config,
                remote_command="echo ok",
            )
            try:
                result = subprocess.run(
                    test_args,
                    capture_output=True,
                    timeout=10,
                    text=True,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    if not getattr(
                        cached_bridge, "notebook_name", None
                    ) and not _looks_like_notebook_id(requested_identifier):
                        cached_bridge.notebook_name = requested_identifier.strip() or None
                        try:
                            cached_config.add_bridge(cached_bridge)
                            save_tunnel_config(cached_config)
                        except Exception:
                            pass
                    click.echo("Using cached tunnel connection (fast path).", err=True)
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
                    return
            except subprocess.TimeoutExpired:
                pass
        else:
            if bridge_notebook_id:
                click.echo(
                    (
                        f"Notebook alias '{profile_name}' targets notebook '{bridge_notebook_id}'; "
                        f"refreshing tunnel for '{notebook_id}'."
                    ),
                    err=True,
                )
            else:
                click.echo(
                    (
                        f"Notebook alias '{profile_name}' has no notebook binding metadata; "
                        f"refreshing tunnel for '{notebook_id}'."
                    ),
                    err=True,
                )

    if not ctx.json_output:
        click.echo("Fetching notebook details for tunnel setup...", err=True)
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
        _handle_error(
            ctx,
            "NotebookFailed",
            f"Notebook failed to start: {e}",
            EXIT_API_ERROR,
            hint=e.events or "Check Events tab in web UI for details.",
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

    # Finalise the alias now that we know the notebook's display name.
    # Only kicks in when neither ``--save-as`` nor an existing bridge supplied
    # one earlier; see the alias-resolution comment above.
    if profile_name is None:
        notebook_display_name = str(notebook_detail.get("name") or "").strip()
        profile_name = _default_alias_for_notebook(
            cached_config,
            notebook_id=notebook_id,
            notebook_name=notebook_display_name,
        )

    current_user_detail: dict = {}
    try:
        current_user_detail = _get_current_user_detail(session, base_url=base_url)
    except Exception:
        current_user_detail = {}

    allowed, reason = _validate_notebook_account_access(
        current_user=current_user_detail,
        notebook_detail=notebook_detail,
    )
    if not allowed:
        configured_user = str(getattr(config, "username", "") or "").strip()
        user_label = configured_user or "current account"
        _handle_error(
            ctx,
            "ConfigError",
            f"Notebook/account mismatch detected before tunnel setup: {reason}.",
            EXIT_CONFIG_ERROR,
            hint=(
                f"Notebook '{notebook_id}' appears to belong to another account. "
                f"Switch [auth].username for this project (current: {user_label}) and ensure a "
                "matching password is available via INSPIRE_PASSWORD or global "
                '[accounts."<username>"].password.'
            ),
        )
        return

    gpu_info = (notebook_detail.get("resource_spec_price") or {}).get("gpu_info") or {}
    gpu_type = gpu_info.get("gpu_product_simple", "")
    has_internet = has_internet_for_gpu_type(gpu_type)

    try:
        ssh_public_key = load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        ssh_runtime = resolve_ssh_runtime_config()
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except browser_api_module.RtunnelMissingInContainerError:
        # Structured failure: rtunnel is not baked into the image and the
        # container can't reach the public internet to curl it. Give the
        # user a concrete repair path instead of a generic "bootstrap
        # failed" message.
        _handle_error(
            ctx,
            "SetupError",
            "SSH bootstrap 失败：rtunnel 在容器内找不到，且容器无公网（curl 取不到）。",
            EXIT_API_ERROR,
            hint=(
                "修复（选一个）：\n"
                "  1. 把镜像换成 unified-base:v1 或它的派生镜像，自带 rtunnel + sshd。\n"
                "  2. 在可上网区（CPU资源空间 / HPC-可上网区资源-2）开一个 notebook，\n"
                "     curl 一次 rtunnel 到 /usr/local/bin/rtunnel，然后\n"
                "     inspire image save 成自己的镜像；之后所有 notebook 都用这个镜像。"
            ),
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to set up notebook tunnel: {e}", EXIT_API_ERROR)
        return

    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        has_internet=has_internet,
        notebook_id=notebook_id,
        notebook_name=str(notebook_detail.get("name") or "").strip() or None,
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
                "Retry 'inspire notebook ssh <notebook-id>' in a few seconds, "
                f"or run 'inspire notebook test -a {profile_name}' to inspect connectivity. "
                f"Proxy readiness report: {proxy_status} ({redact_proxy_url(proxy_url)})."
                f"{ssh_capability_hint}"
            ),
        )
        return

    internet_status = "yes" if has_internet else "no"
    gpu_label = gpu_type if gpu_type else "CPU"
    click.echo(
        f"Added bridge '{profile_name}' (internet: {internet_status}, GPU: {gpu_label})",
        err=True,
    )

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
