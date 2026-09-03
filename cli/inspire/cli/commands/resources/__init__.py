"""Resource commands for Inspire CLI."""

from __future__ import annotations

import click

from .resources_list import availability_resources
from .resources_node_events import node_events
from .resources_nodes import list_nodes
from .resources_policy import policy_resources
from .resources_usage import usage_resources


@click.group()
def resources() -> None:
    """Inspect live compute availability, placement and scheduling policy.

    Use `resources availability` for guarantee-level capacity and whole-node
    placement,
    `resources policy` for how long the workspace lets a workload keep what it
    took before reclaiming, `resources usage` for who is holding what is
    already taken, and `resources node-events` for what a specific machine did
    to the workload that landed on it. Valid `--quota gpu,cpu,mem` triples live
    under each workload group: `notebook quota`, `job quota`, `hpc quota`,
    `ray quota`, and `serving quota`.

    Availability, policy and usage read facts declared per workspace,
    so each takes one workspace name and comparing two means running it twice.
    Node events are cluster facts keyed directly by one or more node names and
    therefore take no workspace option.

    \b
    Examples:
        inspire job quota --workspace 分布式训练空间 --group H200
        inspire resources availability --workspace 分布式训练空间 --include-cpu
        inspire resources availability --workspace 分布式训练空间 --group H200
        inspire resources policy --workspace 分布式训练空间
        inspire resources usage --workspace 分布式训练空间 --by user
        inspire resources node-events qb-prod-4090-gpu040 --type Warning
    """
    pass


resources.add_command(availability_resources)
resources.add_command(list_nodes)
resources.add_command(node_events)
resources.add_command(policy_resources)
resources.add_command(usage_resources)
