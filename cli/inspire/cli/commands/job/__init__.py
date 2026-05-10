"""Job commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import job_batch
from inspire.cli.commands.workload_profile import make_profile_command

from .job_commands import (
    delete,
    instances,
    list_jobs,
    shell,
    show_command,
    show_id,
    status,
    status_catalog,
    stop,
    wait,
)
from .job_create import create
from .job_events import events
from .job_logs import logs
from .job_metrics import job_metrics


@click.group()
def job() -> None:
    """Manage distributed training jobs.

    \b
    Examples:
        inspire job create --name train-a --quota 8,160,1800 --command "bash train.sh"
        inspire job logs train-a --follow
        inspire job metrics train-a --window 30m
        inspire job events train-a --tail 50
    """


job.add_command(create)
job.add_command(make_profile_command("job"))
job.add_command(job_batch)
job.add_command(status)
job.add_command(show_id)
job.add_command(logs)
job.add_command(events)
job.add_command(instances)
job.add_command(list_jobs)
job.add_command(status_catalog)
job.add_command(shell)
job.add_command(stop)
job.add_command(delete)
job.add_command(wait)
job.add_command(show_command)
job.add_command(job_metrics)  # metrics (资源视图 time-series; per-pod for distributed training)


__all__ = ["job"]
