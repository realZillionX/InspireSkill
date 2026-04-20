"""Resource commands for Inspire CLI."""

from __future__ import annotations

import click

from .resources_list import list_resources
from .resources_nodes import list_nodes
from .resources_specs import list_specs


@click.group()
def resources() -> None:
    """View available compute resources."""
    pass


resources.add_command(list_resources)
resources.add_command(list_nodes)
resources.add_command(list_specs)
