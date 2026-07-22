"""Job commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import job_batch
from inspire.cli.commands.workload_quota import make_quota_command
from inspire.cli.commands.workload_profile import make_profile_command

from .job_commands import (
    delete,
    instances,
    list_jobs,
    shell,
    show_command,
    show_id,
    status,
    stop,
    wait,
)
from .job_create import create
from .job_events import events
from .job_logs import logs
from .job_metrics import job_metrics


@click.group()
def job() -> None:
    """Manage GPU batch jobs and distributed-training workloads.

    Use `job create` for non-interactive GPU work: distributed training,
    multi-node training, batch inference, or a fixed pool of GPU workers.
    Prepare code, data, and dependencies on shared storage before submitting.
    For 分布式训练空间 or another GPU area without internet egress, public
    downloads should be prepared first; SII internal mirrors may still work.

    \b
    Examples:
        inspire job quota --workspace 分布式训练空间 --group H200
        inspire job create --name train-a --workspace 分布式训练空间 --project CI-情境智能 --group H200-2号机房 -q 8,160,1800 --image train-base:v1 --nodes 2 --command "bash repo/train.sh" --priority 4
        inspire job logs train-a --workspace 分布式训练空间 --follow
        inspire job metrics train-a --workspace 分布式训练空间 --window 30m
        inspire job events train-a --workspace 分布式训练空间 --tail 50
    """


job.add_command(create)
job.add_command(make_quota_command("job"))
job.add_command(make_profile_command("job"))
job.add_command(job_batch)
job.add_command(status)
job.add_command(show_id)
job.add_command(logs)
job.add_command(events)
job.add_command(instances)
job.add_command(list_jobs)
job.add_command(shell)
job.add_command(stop)
job.add_command(delete)
job.add_command(wait)
job.add_command(show_command)
job.add_command(job_metrics)  # metrics (资源视图 time-series; per-pod for distributed training)


__all__ = ["job"]
