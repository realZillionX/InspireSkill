"""`inspire notebook events <name>` — lifecycle timeline for a notebook instance.

Notebook events are a platform lifecycle timeline: scheduling, image pulls,
container start, stop, save, and related messages. The platform may return an
empty list for long-terminated notebooks; that is a normal steady state, not
an error. Notebooks run as one instance, so there is no ``--instance`` flag.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.events import filter_events, run_events_command
from inspire.platform.web.browser_api.notebooks import list_notebook_events

from .public_output import public_events


@click.command("events")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--keyword",
    "keyword_filter",
    help="Filter lifecycle messages by substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    help="Show only the last N events (applied after --keyword).",
)
@click.option("--follow", "-f", is_flag=True, help="Follow the event timeline and print new events.")
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=5,
    show_default=True,
    help="Polling interval in seconds for --follow.",
)
@pass_context
def events(
    ctx: Context,
    name: str,
    workspace: str,
    keyword_filter: Optional[str],
    tail: Optional[int],
    follow: bool,
    interval: int,
) -> None:
    """Show platform events for a notebook instance.

    \b
    Examples:
      inspire notebook events <name> --workspace 分布式训练空间
      inspire --json notebook events <name> --workspace 分布式训练空间
      inspire notebook events <name> --workspace 分布式训练空间 --keyword FailedScheduling
      inspire notebook events <name> --workspace 分布式训练空间 --follow
    """
    from inspire.cli.commands.notebook import notebook_lookup as _nb
    from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, get_base_url, load_config, require_web_session
    from inspire.config import ConfigError
    from inspire.config.workspaces import resolve_workspace_query_scope
    from inspire.cli.context import EXIT_CONFIG_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    config = load_config(ctx)
    try:
        workspace_ids, _ = resolve_workspace_query_scope(
            config,
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    notebook_id, _ = _nb._resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=get_base_url(),
        identifier=name,
        json_output=getattr(ctx, "json_output", False),
        workspace_ids=workspace_ids,
    )
    def fetch_raw() -> list[dict]:
        return list_notebook_events(notebook_id, session=session)

    if ctx.json_output:
        if follow:
            raise click.UsageError(
                "--json --follow is not supported for notebook events."
            )
        try:
            filtered_events = filter_events(
                fetch_raw(),
                keyword_filter=keyword_filter,
                tail=tail,
            )
        except Exception as e:  # noqa: BLE001
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return
        click.echo(
            json_formatter.format_json(
                {
                    "name": name,
                    "events": public_events(filtered_events),
                }
            )
        )
        return

    run_events_command(
        ctx,
        resource_id=notebook_id,
        resource_type="notebook",
        resource_name=name,
        fetch=lambda: public_events(fetch_raw()),
        json_output_local=False,
        type_filter=None,
        reason_filter=None,
        keyword_filter=keyword_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
