"""`inspire notebook events <name>` — lifecycle timeline for a notebook instance.

Notebook events are a platform lifecycle timeline: scheduling, image pulls,
container start, stop, save, and related messages. The platform may return an
empty list for long-terminated notebooks; that is a normal steady state, not
an error. Notebooks run as one instance, so there is no ``--instance`` flag.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, run_events_command
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.platform.web.browser_api.notebooks import list_notebook_events


@click.command("events")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--keyword",
    "keyword_filter",
    metavar="KEYWORD",
    help="Filter lifecycle messages by substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help=(
        "Follow the event timeline and print new events. Runs until interrupted; it never exits on its own, "
        "not even once the notebook reaches a terminal state."
    ),
)
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
    pick: int | None,
    keyword_filter: Optional[str],
    tail: int,
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
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    from inspire.cli.commands.notebook import notebook_lookup as _nb
    from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, get_base_url, require_web_session
    from inspire.config import ConfigError
    from inspire.config.workspaces import resolve_workspace_operation_scope
    from inspire.cli.context import EXIT_CONFIG_ERROR
    from inspire.cli.utils.errors import exit_with_error as _handle_error

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    base_url = get_base_url()

    def fetch_raw() -> list[dict]:
        raw_events, _notebook_id, _workspace_id = (
            _nb._run_notebook_operation_with_stale_handle_retry(
                ctx,
                session=session,
                base_url=base_url,
                identifier=name,
                json_output=getattr(ctx, "json_output", False),
                workspace_ids=[workspace_id],
                pick=pick,
                operation=lambda notebook_id: list_notebook_events(
                    notebook_id,
                    session=session,
                ),
            )
        )
        return raw_events

    run_events_command(
        ctx,
        fetch=fetch_raw,
        type_filter=None,
        reason_filter=None,
        keyword_filter=keyword_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
