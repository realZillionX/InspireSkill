"""HPC commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import hpc_batch
from inspire.cli.commands.workload_profile import make_profile_command

from .hpc_commands import (
    create_hpc,
    delete_hpc,
    hpc_id,
    instances_hpc,
    list_hpc,
    status_hpc,
    stop_hpc,
)
from .hpc_events import events as events_hpc
from .hpc_metrics import hpc_metrics


@click.group()
def hpc() -> None:
    """Manage high-performance computing (HPC) jobs.

    \b
    Examples:
        inspire hpc create --name prep-a --quota 0,16,64 --command "srun python prep.py"
        inspire hpc instances prep-a --workspace CPU资源空间
        inspire hpc metrics prep-a --metric cpu,mem,disk_read,disk_write --window 2h
        inspire hpc events prep-a --tail 50
    """


hpc.add_command(list_hpc)
hpc.add_command(create_hpc)
hpc.add_command(make_profile_command("hpc"))
hpc.add_command(hpc_batch)
hpc.add_command(status_hpc)
hpc.add_command(instances_hpc)
hpc.add_command(hpc_id)
hpc.add_command(stop_hpc)
hpc.add_command(delete_hpc)
hpc.add_command(events_hpc)
hpc.add_command(hpc_metrics)  # metrics (资源视图 time-series; per-task slurm pods)


__all__ = ["hpc"]
