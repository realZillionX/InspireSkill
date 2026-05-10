"""`inspire hpc events <name>` — job-level platform events for an HPC job.

Use `inspire hpc instances <name> --workspace <workspace>` for the
pod/component inventory. Events remain scoped to the HPC job object.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.commands.hpc.hpc_commands import _resolve_hpc_name
from inspire.cli.utils.events import run_events_command
from inspire.platform.web.browser_api.hpc_jobs import list_hpc_job_events


@click.command("events")
@click.argument("name")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON. Equivalent to top-level `--json`.",
)
@click.option(
    "--reason",
    "reason_filter",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --reason).",
)
@pass_context
def events(
    ctx: Context,
    name: str,
    json_output_local: bool,
    reason_filter: Optional[str],
    tail: Optional[int],
) -> None:
    """Show job-level platform events for an HPC job.

    \b
    Examples:
      inspire hpc events <name>
      inspire --json hpc events <name>
      inspire hpc events <name> --reason Deleted
    """
    job_id = _resolve_hpc_name(ctx, name)
    run_events_command(
        ctx,
        resource_id=job_id,
        resource_type="hpc",
        resource_name=name,
        fetch=lambda: list_hpc_job_events(job_id),
        json_output_local=json_output_local,
        type_filter=None,  # HPC events lack `type`; filter not applicable
        reason_filter=reason_filter,
        tail=tail,
    )
