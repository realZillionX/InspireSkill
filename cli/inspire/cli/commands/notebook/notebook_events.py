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
from inspire.cli.utils.events import run_events_command
from inspire.platform.web.browser_api.notebooks import list_notebook_events


@click.command("events")
@click.argument("name")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON. Equivalent to top-level `--json`.",
)
@click.option(
    "--type",
    "type_filter",
    help="Filter events by `type` (Normal / Warning; case-insensitive prefix match).",
)
@click.option(
    "--reason",
    "reason_filter",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --type/--reason).",
)
@pass_context
def events(
    ctx: Context,
    name: str,
    json_output_local: bool,
    type_filter: Optional[str],
    reason_filter: Optional[str],
    tail: Optional[int],
) -> None:
    """Show platform events for a notebook instance.

    \b
    Examples:
      inspire notebook events <name>
      inspire --json notebook events <name>
      inspire notebook events <name> --type Warning
      inspire notebook events <name> --reason FailedScheduling
    """
    from inspire.cli.commands.notebook.notebook_metrics import _notebook_name_to_id

    notebook_id = _notebook_name_to_id(ctx, name)
    run_events_command(
        ctx,
        resource_id=notebook_id,
        resource_type="notebook",
        resource_name=name,
        fetch=lambda: list_notebook_events(notebook_id),
        json_output_local=json_output_local,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )
