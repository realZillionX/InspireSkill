from __future__ import annotations

import click

from inspire.cli.context import EXIT_API_ERROR, EXIT_CONFIG_ERROR, Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    require_web_session,
)
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module

def _resolve_notebook_for_net_test(
    ctx: Context,
    *,
    notebook: str,
    workspace: str,
    timeout: int,
    pick: int | None = None,
):
    from inspire.config.workspaces import resolve_workspace_operation_scope

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    workspace_id = resolve_workspace_operation_scope(
        workspace=workspace,
        session=session,
    )
    from .notebook_lookup import _run_notebook_operation_with_stale_handle_retry

    result, _notebook_id, _workspace_id = (
        _run_notebook_operation_with_stale_handle_retry(
            ctx,
            session=session,
            base_url=get_base_url(),
            identifier=notebook,
            json_output=ctx.json_output,
            workspace_ids=[workspace_id],
            pick=pick,
            operation=lambda notebook_id: browser_api_module.probe_notebook_network(
                notebook_id=notebook_id,
                session=session,
                timeout=timeout,
            ),
        )
    )
    return result, notebook


def _yes_no_unknown(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


@click.command("net-test")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option("--timeout", type=click.IntRange(5), default=30, show_default=True)
@pass_context
def notebook_net_test(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: int | None,
    timeout: int,
) -> None:
    """Probe notebook egress through JupyterTerminal, without SSH or rtunnel."""
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    try:
        result, notebook_name = _resolve_notebook_for_net_test(
            ctx,
            notebook=notebook,
            workspace=workspace,
            timeout=timeout,
            pick=pick,
        )
    except ConfigError as exc:
        exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        return
    except Exception as exc:
        exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    payload = {
        "notebook": notebook_name,
        "public_internet": result.public_internet,
        "public_successes": result.public_successes,
        "public_failures": result.public_failures,
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Notebook: {notebook_name}")
    click.echo(f"Public internet: {_yes_no_unknown(result.public_internet)}")
    if result.public_successes:
        click.echo("Public successes: " + ", ".join(result.public_successes))
    if result.public_failures:
        click.echo("Public failures: " + ", ".join(result.public_failures))
