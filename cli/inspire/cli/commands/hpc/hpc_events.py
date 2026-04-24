"""`inspire hpc events <id>` — events for an HPC (Slurm) job.

Payload comes from Browser API `POST /api/v1/hpc_jobs/events/list` via
`browser_api.hpc_jobs.list_hpc_job_events`. Output is cached to
`~/.inspire/events/<job_id>.events.json` on every successful fetch.

**Only job-level events are exposed** — confirmed empirically that the
platform does not populate per-pod events for HPC tasks (``object_type=
"instance"`` with real pod names returns 0 across launcher / slurmctld /
slurmd). Hence no ``--instance`` flag; train jobs have richer per-pod
events available via ``inspire job events --instance``.

HPC events lack the K8s-native ``type`` field (Normal / Warning) that
train events carry — the column renders blank for HPC, so no ``--type``
filter either. Useful fields are ``reason`` (e.g. ``CreatedSlurmCluster``,
``DeletedSlurmJobSubmitter``), ``message``, and
``first_timestamp`` / ``last_timestamp``.
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
    "--from-cache",
    is_flag=True,
    help="Read from `~/.inspire/events/<id>.events.json` and skip the live fetch.",
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
    from_cache: bool,
    reason_filter: Optional[str],
    tail: Optional[int],
) -> None:
    """Show platform events for an HPC job (job-level only; platform doesn't expose per-pod).

    \b
    Examples:
      inspire hpc events <name>
      inspire --json hpc events <name>
      inspire hpc events <name> --reason Deleted
      inspire hpc events <name> --from-cache
    """
    job_id = _resolve_hpc_name(ctx, name)
    run_events_command(
        ctx,
        job_id=job_id,
        fetch=lambda: list_hpc_job_events(job_id),
        json_output_local=json_output_local,
        from_cache=from_cache,
        type_filter=None,  # HPC events lack `type`; filter not applicable
        reason_filter=reason_filter,
        tail=tail,
    )
