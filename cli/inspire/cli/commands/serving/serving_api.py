"""Serving API instructions without fetching or displaying credentials."""

from __future__ import annotations

import click

from inspire.cli.context import (
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    Context,
    pass_context,
)
from inspire.services.utils import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.config import ConfigError
from inspire.platform.web import browser_api
from inspire.platform.web.session import SessionExpiredError, get_web_session

from . import serving_commands
from inspire.services.serving.serving_access import invocation_info


def _affinity(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and (
        not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise click.BadParameter("Use 1-256 characters without control characters.")
    return value


@click.command("api")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "curl"]),
    default="text",
    help="Display instructions or a curl example. --json always returns structured data.",
)
@click.option(
    "--affinity-key",
    callback=_affinity,
    help="Optional request hash key for node affinity, not authentication.",
)
@pass_context
def serving_api(
    ctx: Context,
    name: str,
    workspace: str,
    pick: int | None,
    output_format: str,
    affinity_key: str | None,
) -> None:
    """Show the endpoint and API Key invocation instructions.

    Authorization uses Bearer $INF_API_KEY. x-inspire-inference-key is an
    optional node-affinity hash key. CUSTOM routes and bodies belong to your
    container; OpenAI compatibility is not assumed. No request is sent to the
    serving and no API key is fetched. Manage keys with account api-key.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        session = get_web_session()
        workspace_id = serving_commands._resolve_workspace_id(workspace, session=session)
        data = serving_commands._run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=lambda serving_id, live_session: browser_api.get_serving_detail(
                serving_id, session=live_session
            ),
        )
        info = invocation_info(data, name, affinity_key)
        if ctx.json_output:
            # Only validated origins and generated, shell-quoted templates
            # bypass scrubbing. No raw API response or secret is preserved.
            click.echo(
                json_formatter.format_json(info, preserve_raw={"endpoint", "base_url", "example"})
            )
        elif output_format == "curl":
            if not info.get("example"):
                raise ValueError("No supported endpoint is available; retry serving status later.")
            click.echo(info["example"])
        else:
            for key, value in info.items():
                if value:
                    click.echo(f"{key.replace('_', ' ').title()}: {value}")
    except ConfigError as e:
        exit_with_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        exit_with_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        exit_with_error(ctx, "APIError", str(e), EXIT_API_ERROR)
