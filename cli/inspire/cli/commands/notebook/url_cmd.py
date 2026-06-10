"""`inspire notebook url` / `proxy-url` / `vscode-proxy-suffix`.

Two separate ways to address a notebook's web IDE:

- ``url`` prints the notebook url — the stable entrance link
  ``{base}/ide?notebook_id=<id>``. Pure string from the resolved id, no
  browser; opening it redirects into the IDE.
- ``vscode-proxy-suffix`` prints the host-less VSCode proxy suffix
  ``/ws-.../project-.../user-.../vscode/<runtime>/<token>`` (starts with ``/``).
  This drives a headless browser to read the live gateway URL, so the notebook
  must be RUNNING and the embedded token is ephemeral.
- ``proxy-url`` resolves the web IDE gateway and returns the same full
  port-forward URL shape the IDE uses for container HTTP services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import click

from inspire.cli.context import Context, pass_context

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession


def _resolve_notebook(ctx: Context, notebook: str, workspace: str) -> tuple[WebSession, str, str]:
    """Resolve a notebook name to ``(session, base_url, notebook_id)``.

    Exits via the shared error formatter on a workspace/config error.
    """
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

    notebook_id, _ = _nb._resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=workspace_ids,
    )
    return session, base_url, notebook_id


@click.command("url")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@pass_context
def notebook_url(ctx: Context, notebook: str, workspace: str) -> None:
    """Print the notebook url (the web IDE entrance link).

    \b
    Examples:
      inspire notebook url my-notebook --workspace CPU资源空间
      inspire --json notebook url my-notebook --workspace CPU资源空间
    """
    from inspire.cli.formatters import json_formatter

    _session, base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    url = f"{base_url}/ide?notebook_id={notebook_id}"

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": notebook, "url": url},
                allow_ids=True,
            )
        )
    else:
        click.echo(url)


@click.command("vscode-proxy-suffix")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--timeout",
    type=click.IntRange(10),
    default=60,
    show_default=True,
    help="Seconds to wait for the IDE to load (when the browser runs).",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Skip the cache and re-derive via the browser (use after a container restart).",
)
@pass_context
def notebook_vscode_proxy_suffix(
    ctx: Context,
    notebook: str,
    workspace: str,
    timeout: int,
    refresh: bool,
) -> None:
    """Print the host-less VSCode proxy suffix for a notebook.

    Returns the /ws-.../vscode/<runtime>/<token> path (starts with /, no host).
    The resolved URL is cached per account and revalidated with a quick HTTP
    probe, so repeat calls are instant; the headless browser only runs on a cold
    cache or after the container restarted (which rotates the token). The
    notebook must be RUNNING. Pass --refresh to force a fresh derivation.

    \b
    Examples:
      inspire notebook vscode-proxy-suffix my-notebook --workspace CPU资源空间
      inspire notebook vscode-proxy-suffix my-notebook --workspace CPU资源空间 --refresh
      inspire --json notebook vscode-proxy-suffix my-notebook --workspace CPU资源空间
    """
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.cli.formatters import json_formatter
    from inspire.cli.utils.errors import exit_with_error as _handle_error
    from inspire.platform.web.browser_api import resolve_notebook_vscode_proxy_suffix

    session, _base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    suffix = resolve_notebook_vscode_proxy_suffix(
        notebook_id,
        session=session,
        timeout=timeout,
        refresh=refresh,
    )
    if not suffix:
        _handle_error(
            ctx,
            "APIError",
            f"Could not resolve the VSCode proxy suffix for '{notebook}'. The notebook must be "
            "RUNNING with its web IDE reachable.",
            EXIT_API_ERROR,
            hint="Retry once it is RUNNING.",
        )
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": notebook, "vscode_proxy_suffix": suffix},
                allow_ids=True,
            )
        )
    else:
        click.echo(suffix)


def _build_proxy_url(ide_url: str, *, port: int, service_path: str) -> str:
    from inspire.platform.web.browser_api.playwright_notebooks import (
        build_notebook_port_forward_url,
    )

    url = build_notebook_port_forward_url(
        ide_url,
        port=port,
        service_path=service_path,
    )
    if not url:
        raise ValueError("Could not build a port-forward URL from the IDE gateway URL.")
    return url


@click.command("proxy-url")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--port",
    required=True,
    type=click.IntRange(1, 65535),
    help="Container HTTP port to expose through the notebook proxy.",
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
    help="Seconds to wait for the IDE to load (when the browser runs).",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Skip the cache and re-derive via the browser (use after a container restart).",
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
) -> None:
    """Print a full proxy URL for a notebook container HTTP service.

    The notebook must be RUNNING. Use --path /v1 for OpenAI-compatible APIs or
    omit --path for browser apps such as Gradio/FastAPI root pages.

    \b
    Examples:
      inspire notebook proxy-url my-notebook --workspace CPU资源空间 --port 7860
      inspire notebook proxy-url my-notebook --workspace CPU资源空间 --port 30000 --path /v1
      inspire --json notebook proxy-url my-notebook --workspace CPU资源空间 --port 30000 --path /v1
    """
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.cli.formatters import json_formatter
    from inspire.cli.utils.errors import exit_with_error as _handle_error
    from inspire.platform.web.browser_api import resolve_notebook_port_forward_url

    session, _base_url, notebook_id = _resolve_notebook(ctx, notebook, workspace)
    url = resolve_notebook_port_forward_url(
        notebook_id,
        port=port,
        service_path=service_path,
        session=session,
        timeout=timeout,
        refresh=refresh,
    )
    if not url:
        _handle_error(
            ctx,
            "APIError",
            f"Could not resolve the notebook proxy URL for '{notebook}'. The notebook must be "
            "RUNNING with its web IDE reachable.",
            EXIT_API_ERROR,
            hint="Retry once it is RUNNING.",
        )
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "name": notebook,
                    "port": port,
                    "path": service_path,
                    "url": url,
                },
                allow_ids=True,
            )
        )
    else:
        click.echo(url)
