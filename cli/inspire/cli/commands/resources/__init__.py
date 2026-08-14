"""Resource commands for Inspire CLI."""

from __future__ import annotations

import click

from .resources_list import availability_resources
from .resources_nodes import list_nodes
from .resources_quota import quota_resources


@click.group()
def resources() -> None:
    """Inspect live compute availability and the workspace quota ceiling.

    Use `resources availability` for current free / used capacity,
    `resources nodes` before multi-node GPU jobs that need whole 8-GPU nodes,
    and `resources quota` to check the workspace is still allowed to take that
    capacity. Valid `--quota gpu,cpu,mem` triples live under each workload
    group: `notebook quota`, `job quota`, `hpc quota`, `ray quota`, and
    `serving quota`.

    \b
    Examples:
        inspire job quota --workspace 分布式训练空间 --group H200
        inspire resources availability --workspace all --include-cpu
        inspire resources nodes --workspace 分布式训练空间 --min-nodes 2 --group H200
        inspire resources quota --workspace 分布式训练空间
    """
    pass


resources.add_command(availability_resources)
resources.add_command(list_nodes)
resources.add_command(quota_resources)
