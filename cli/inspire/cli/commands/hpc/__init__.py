"""HPC commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import hpc_batch
from inspire.cli.commands.workload_quota import make_quota_command
from inspire.cli.commands.workload_profile import make_profile_command

from .hpc_commands import (
    create_hpc,
    delete_hpc,
    instances_hpc,
    list_hpc,
    status_hpc,
    stop_hpc,
)
from .hpc_events import events as events_hpc
from .hpc_metrics import hpc_metrics


@click.group()
def hpc() -> None:
    """Manage CPU Slurm / HPC batch jobs.

    Use `hpc create` for fixed-size CPU preprocessing, evaluation, and data
    pipelines. Choose a CPU-capable spec with `hpc quota --workspace <name>`,
    write only the Slurm script body in `-c`, and launch your program with
    `srun`.

    \b
    Examples:
        inspire hpc quota --workspace CPU资源空间
        inspire hpc create --name prep-a --workspace CPU资源空间 --project CI-情境智能 --group HPC-可上网区资源-2 -q 0,16,64 --image hpc-base:v1 -c "srun python prep.py"
        inspire hpc instances prep-a --workspace CPU资源空间
        inspire hpc metrics prep-a --workspace CPU资源空间 --metric cpu,mem,disk_read,disk_write --window 2h
        inspire hpc events prep-a --workspace CPU资源空间 --tail 50
    """


hpc.add_command(list_hpc)
hpc.add_command(create_hpc)
hpc.add_command(make_quota_command("hpc"))
hpc.add_command(make_profile_command("hpc"))
hpc.add_command(hpc_batch)
hpc.add_command(status_hpc)
hpc.add_command(instances_hpc)
hpc.add_command(stop_hpc)
hpc.add_command(delete_hpc)
hpc.add_command(events_hpc)
hpc.add_command(hpc_metrics)


__all__ = ["hpc"]
