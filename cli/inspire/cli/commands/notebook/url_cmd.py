"""Notebook web-entry commands.

Platform IDE links contain notebook, workspace, runtime, and short-lived
token handles.  They are still required internally to open the web UI, but
are never emitted by the CLI.  The commands below resolve the link, open it
locally, and report only the user-facing notebook name.
"""

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .public_output import public_operation
from .transport import preflight_notebook_transport_policy

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession


def _resolve_notebook(ctx: Context, notebook: str, workspace: str) -> tuple[WebSession, str, str]:
    """Resolve a notebook name to ``(session, base_url, notebook_id)``."""
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    from inspire.cli.commands.notebook import notebook_lookup as _nb
    from inspire.cli.context import EXIT_CONFIG_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error
    from inspire.cli.utils.notebook_cli import (
        WEB_AUTH_HINT,
        get_base_url,
        load_config,
        require_web_session,
    )
    from inspire.config import ConfigError
    from inspire.config.workspaces import resolve_workspace_query_scope
    from inspire.platform.web import browser_api as browser_api_module

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    base_url = get_base_url()
    config = load_config(ctx)
    try:
        workspace_ids, _ = resolve_workspace_query_scope(
            config,
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        raise  # unreachable: _handle_error exits

    _detail, notebook_id, _workspace_id = (
        _nb._run_notebook_operation_with_stale_handle_retry(
            ctx,
            session=session,
            config=config,
            base_url=base_url,
            identifier=notebook,
            json_output=ctx.json_output,
            workspace_ids=workspace_ids,
            operation=lambda resolved_id: browser_api_module.get_notebook_detail(
                notebook_id=resolved_id,
                session=session,
            ),
        )
    )
    return session, base_url, notebook_id


def _open_resolved_url(
    ctx: Context,
    *,
    notebook: str,
    url: str,
    action: str = "open",
) -> bool:
    """Open a resolved URL without exposing it on any CLI stream."""
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error

    try:
        opened = bool(webbrowser.open(url, new=2, autoraise=True))
    except Exception:
        opened = False
    if not opened:
        _handle_error(
            ctx,
            "BrowserError",
            f"Could not {action} the web interface for notebook '{scrub_raw_ids(notebook)}'.",
            EXIT_API_ERROR,
            hint="Run the command in a session with a browser available.",
        )
        return False

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(public_operation(scrub_raw_ids(notebook), "opened"))
        )
    else:
        click.echo(f"Opened notebook '{scrub_raw_ids(notebook)}'.")
    return True


@click.command("url")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@pass_context
def notebook_url(ctx: Context, notebook: str, workspace: str) -> None:
    """Open a notebook's web IDE in the system browser."""
    _session, base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    _open_resolved_url(
        ctx,
        notebook=notebook,
        url=f"{base_url}/ide?notebook_id={notebook_id}",
        action="open",
    )


@click.command("vscode-proxy-suffix")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--timeout",
    type=click.IntRange(10),
    default=60,
    show_default=True,
    help="Seconds to wait for the IDE to load.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Skip the cached IDE route and resolve a fresh route.",
)
@pass_context
def notebook_vscode_proxy_suffix(
    ctx: Context,
    notebook: str,
    workspace: str,
    timeout: int,
    refresh: bool,
) -> None:
    """Open the notebook's VS Code web IDE.

    The historical command name is retained for compatibility, but the
    runtime/token suffix is now an internal implementation detail.
    """
    from inspire.platform.web.browser_api import resolve_notebook_vscode_ide_url

    session, _base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    ide_url = resolve_notebook_vscode_ide_url(
        notebook_id,
        session=session,
        timeout=timeout,
        refresh=refresh,
    )
    if not ide_url:
        from inspire.cli.context import EXIT_API_ERROR
        from inspire.cli.utils.errors import exit_with_error as _handle_error

        _handle_error(
            ctx,
            "APIError",
            f"Could not open the VS Code interface for notebook '{scrub_raw_ids(notebook)}'.",
            EXIT_API_ERROR,
            hint="Retry once the notebook is RUNNING.",
        )
        return
    _open_resolved_url(ctx, notebook=notebook, url=ide_url, action="open")


def _check_proxy_url(session: WebSession, url: str) -> str:
    from inspire.platform.web.session import build_requests_session

    http = None
    try:
        http = build_requests_session(session, url)
        response = http.get(
            url,
            timeout=(5, 10),
            allow_redirects=False,
            stream=True,
        )
        try:
            status = int(response.status_code)
        finally:
            response.close()
    except Exception:
        return "no_service"
    finally:
        if http is not None:
            try:
                http.close()
            except Exception:
                pass

    if 200 <= status < 400:
        return "reachable"
    if status in {401, 403, 404}:
        return "blocked"
    if status in {502, 503, 504}:
        return "no_service"
    return "blocked"


@click.command("proxy-url")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--port",
    required=True,
    type=click.IntRange(1, 65535),
    help="Container HTTP port to open through the notebook proxy.",
)
@click.option(
    "--path",
    "service_path",
    default="",
    help="Optional service path to append, for example /v1.",
)
@click.option(
    "--timeout",
    type=click.IntRange(10),
    default=60,
    show_default=True,
    help="Seconds to wait for the IDE to load.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Skip the cached IDE route and resolve a fresh route.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Probe the service before opening it.",
)
@click.option(
    "--allow-restricted",
    is_flag=True,
    help="Allow opening a proxy from a notebook without public internet.",
)
@pass_context
def notebook_proxy_url(
    ctx: Context,
    notebook: str,
    workspace: str,
    port: int,
    service_path: str,
    timeout: int,
    refresh: bool,
    check: bool,
    allow_restricted: bool,
) -> None:
    """Open a notebook container service in the system browser.

    The generated proxy URL contains temporary routing information and is
    never printed or returned. Use the browser window opened by this command.
    """
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error
    from inspire.platform.web.browser_api import resolve_notebook_port_forward_url

    session, _base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    policy = preflight_notebook_transport_policy(
        ctx,
        notebook=notebook,
        workspace=workspace,
        timeout=min(timeout, 30),
    )
    if not policy.allow_proxy_url and not allow_restricted:
        _handle_error(
            ctx,
            "PolicyBlocked",
            f"proxy-url is blocked on notebooks without public internet: {scrub_raw_ids(notebook)}",
            EXIT_API_ERROR,
            hint=(
                "Use JupyterTerminal for command execution. Do not expose container "
                "HTTP services from restricted notebooks."
            ),
        )
        return

    resolved_url = resolve_notebook_port_forward_url(
        notebook_id,
        port=port,
        service_path=service_path,
        session=session,
        timeout=timeout,
        refresh=refresh,
    )
    if not resolved_url:
        _handle_error(
            ctx,
            "APIError",
            f"Could not open the proxy service for notebook '{scrub_raw_ids(notebook)}'.",
            EXIT_API_ERROR,
            hint="Retry once the notebook is RUNNING with its web IDE reachable.",
        )
        return

    check_status = _check_proxy_url(session, resolved_url) if check else None
    if not _open_resolved_url(ctx, notebook=notebook, url=resolved_url, action="open"):
        return
    if ctx.json_output:
        # `_open_resolved_url` already emitted the compact result.  The check
        # result is intentionally omitted to keep the JSON contract stable.
        return
    if check_status:
        click.echo(f"Service check: {check_status}")


__all__ = ["notebook_proxy_url", "notebook_url", "notebook_vscode_proxy_suffix"]
