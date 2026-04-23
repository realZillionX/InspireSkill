"""Job commands for Inspire CLI."""

from __future__ import annotations

import click

from .job_commands import delete, list_jobs, show_command, status, stop, update_jobs, wait
from .job_create import create
from .job_events import events
from .job_logs import logs
from .job_metrics import job_metrics


@click.group()
def job() -> None:
    """Manage training jobs."""


job.add_command(create)
job.add_command(status)
job.add_command(logs)
job.add_command(events)
job.add_command(list_jobs)
job.add_command(update_jobs)
job.add_command(stop)
job.add_command(delete)
job.add_command(wait)
job.add_command(show_command)
job.add_command(job_metrics)  # metrics (资源视图 time-series; per-pod for distributed training)


__all__ = ["job"]
