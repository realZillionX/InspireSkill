"""`notebook proxy-url` — the HTTP address of a container port.

This CLI is driven by agents, which have no browser to open, so the web IDE
entrances (`url`, `vscode`) were removed; only reaching a *service* deployed in
the notebook is still useful, and that needs the address in hand.

So this one command deliberately breaks the notebook output boundary that the
rest of the group keeps: it prints the resolved proxy URL, token segment and
all. There is no token-free form — the platform route on the console domain
404s, and only the gateway URL with its token actually proxies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids

from .transport import preflight_notebook_transport_policy

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession


def _resolve_notebook(
    ctx: Context,
    notebook: str,
    workspace: str,
    *,
    pick: int | None = None,
) -> tuple[WebSession, str, str]:
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
        require_web_session,
    )
    from inspire.config import ConfigError
    from inspire.config.workspaces import resolve_workspace_operation_scope
    from inspire.platform.web import browser_api as browser_api_module

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    base_url = get_base_url()
    try:
        workspace_id = resolve_workspace_operation_scope(
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
            base_url=base_url,
            identifier=notebook,
            json_output=ctx.json_output,
            workspace_ids=[workspace_id],
            pick=pick,
            operation=lambda resolved_id: browser_api_module.get_notebook_detail(
                notebook_id=resolved_id,
                session=session,
            ),
        )
    )
    return session, base_url, notebook_id


def _check_proxy_url(session: WebSession, url: str) -> str:
    from inspire.platform.web.session import build_requests_session

    http = None
    body = ""
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
            if status >= 500:
                # Bounded: only enough to read the gateway's upstream verdict.
                body = str(response.raw.read(256, decode_content=True) or b"")
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
    # The gateway answers 500 `connect ECONNREFUSED <addr>` when the port has
    # nothing listening. That is "start your service", not "you lack access" —
    # reporting it as blocked sends the caller looking for a permission problem.
    if status >= 500 and "ECONNREFUSED" in body:
        return "no_service"
    return "blocked"


@click.command("proxy-url")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--port",
    required=True,
    type=click.IntRange(1, 65535),
    help="Container HTTP port to reach through the notebook proxy.",
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
    help="Seconds to wait for the notebook gateway route to resolve.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Skip the cached IDE route and resolve a fresh route.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Probe the service and report whether it answers.",
)
@click.option(
    "--allow-restricted",
    is_flag=True,
    help="Allow a proxy URL for a restricted H100/H200 notebook.",
)
@pass_context
def notebook_proxy_url(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: int | None,
    port: int,
    service_path: str,
    timeout: int,
    refresh: bool,
    check: bool,
    allow_restricted: bool,
) -> None:
    """Print the HTTP URL that reaches a container port from outside.

    Use this to fetch a service deployed inside the notebook — an inference
    endpoint, a dev server, TensorBoard. The URL is the command's whole output;
    nothing is opened locally.

    The URL embeds a short-lived access token, so it grants whoever holds it the
    same reach into the notebook that you have. Treat it as a credential.
    """
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error
    from inspire.platform.web.browser_api import resolve_notebook_port_forward_url

    session, _base_url, notebook_id = _resolve_notebook(
        ctx,
        notebook,
        workspace,
        pick=pick,
    )
    policy = preflight_notebook_transport_policy(
        ctx,
        notebook=notebook,
        workspace=workspace,
        pick=pick,
    )
    if not policy.allow_proxy_url and not allow_restricted:
        _handle_error(
            ctx,
            "PolicyBlocked",
            f"proxy-url is blocked on H100/H200 notebooks: {scrub_raw_ids(notebook)}",
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
            f"Could not resolve the proxy service for notebook '{scrub_raw_ids(notebook)}'.",
            EXIT_API_ERROR,
            hint="Retry once the notebook is RUNNING with its web IDE reachable.",
        )
        return

    check_status = _check_proxy_url(session, resolved_url) if check else None

    if ctx.json_output:
        payload: dict[str, object] = {
            "name": scrub_raw_ids(notebook),
            "url": resolved_url,
        }
        if check_status:
            payload["service_check"] = check_status
        # `preserve_raw` because the default scrub rewrites every handle in the
        # URL to `<redacted>`, which is correct everywhere else and leaves this
        # command emitting an address that reaches nothing.
        click.echo(json_formatter.format_json(payload, preserve_raw={"url"}))
        return

    click.echo(resolved_url)
    if check_status:
        click.echo(f"Service check: {check_status}")


__all__ = ["notebook_proxy_url"]
