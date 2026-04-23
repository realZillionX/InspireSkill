"""HPC commands for Inspire CLI."""

from __future__ import annotations

import click

from .hpc_commands import create_hpc, delete_hpc, list_hpc, status_hpc, stop_hpc
from .hpc_events import events as events_hpc
from .hpc_metrics import hpc_metrics


@click.group()
def hpc() -> None:
    """Manage high-performance computing (HPC) jobs."""


hpc.add_command(list_hpc)
hpc.add_command(create_hpc)
hpc.add_command(status_hpc)
hpc.add_command(stop_hpc)
hpc.add_command(delete_hpc)
hpc.add_command(events_hpc)
hpc.add_command(hpc_metrics)  # metrics (资源视图 time-series; per-task slurm pods)


__all__ = ["hpc"]
