"""Resource commands for Inspire CLI."""

from __future__ import annotations

import click

from .resources_list import availability_resources
from .resources_node_events import node_events
from .resources_nodes import list_nodes
from .resources_policy import policy_resources
from .resources_quota import quota_resources
from .resources_usage import usage_resources


@click.group()
def resources() -> None:
    """Inspect live compute availability and the workspace quota ceiling.

    Use `resources availability` for current free / used capacity,
    `resources nodes` before multi-node GPU jobs that need whole 8-GPU nodes,
    `resources quota` to check the workspace is still allowed to take that
    capacity, `resources policy` for how long the workspace lets a workload
    keep it before reclaiming, `resources usage` for who is holding what is
    already taken, and `resources node-events` for what a specific machine did
    to the workload that landed on it. Valid `--quota gpu,cpu,mem` triples live
    under each workload group: `notebook quota`, `job quota`, `hpc quota`,
    `ray quota`, and `serving quota`.

    \b
    Examples:
        inspire job quota --workspace 分布式训练空间 --group H200
        inspire resources availability --workspace all --include-cpu
        inspire resources nodes --workspace 分布式训练空间 --min-nodes 2 --group H200
        inspire resources quota --workspace 分布式训练空间
        inspire resources policy --workspace 分布式训练空间
        inspire resources usage --workspace 分布式训练空间 --by user
        inspire resources node-events qb-prod-4090-gpu040 --type Warning
    """
    pass


resources.add_command(availability_resources)
resources.add_command(list_nodes)
resources.add_command(node_events)
resources.add_command(policy_resources)
resources.add_command(quota_resources)
resources.add_command(usage_resources)
