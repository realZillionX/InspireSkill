"""`inspire hpc events <name>` — job-level platform events for an HPC job.

Use `inspire hpc instances <name> --workspace <workspace>` for the
pod/component inventory. Events remain scoped to the HPC job object.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.commands.hpc.hpc_commands import (
    _reject_hpc_name_at_boundary,
    _run_readonly_hpc_operation,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, run_events_command
from inspire.config import Config, ConfigError
from inspire.platform.web.browser_api.hpc_jobs import list_hpc_job_events
from inspire.platform.web.session import get_web_session


@click.command("events")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--reason",
    "reason_filter",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    help=f"Show the latest {DEFAULT_EVENT_TAIL} events by default; use --tail N to change the limit.",
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
    reason_filter: Optional[str],
    tail: Optional[int],
    follow: bool,
    interval: int,
) -> None:
    """Show job-level platform events for an HPC job.

    \b
    Examples:
      inspire hpc events <name> --workspace CPU资源空间
      inspire --json hpc events <name> --workspace CPU资源空间
      inspire hpc events <name> --workspace CPU资源空间 --reason Deleted
      inspire hpc events <name> --workspace CPU资源空间 --follow
    """
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    run_events_command(
        ctx,
        resource_id=name,
        resource_type="hpc",
        resource_name=name,
        fetch=lambda: _run_readonly_hpc_operation(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            operation=lambda resolved_id, live_session: list_hpc_job_events(
                resolved_id,
                session=live_session,
            ),
        ),
        json_output_local=False,
        type_filter=None,  # HPC events lack `type`; filter not applicable
        reason_filter=reason_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
