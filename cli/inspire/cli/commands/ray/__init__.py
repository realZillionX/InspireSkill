"""Ray (弹性计算) commands for Inspire CLI.

The command group covers the user-visible Ray lifecycle: list, status,
events, instances, create, stop, and delete. It intentionally presents the
platform object as a named elastic cluster instead of exposing transport or
form details.
"""

from __future__ import annotations

import click

from inspire.cli.commands.batch import ray_batch
from inspire.cli.commands.workload_profile import make_profile_command

from .ray_commands import (
    create_ray,
    delete_ray,
    events_ray,
    instances_ray,
    list_ray,
    status_ray,
    stop_ray,
)


@click.group()
def ray() -> None:
    """Manage Ray (弹性计算) jobs — elastic clusters with auto-scaling workers."""


ray.add_command(list_ray)
ray.add_command(status_ray)
ray.add_command(stop_ray)
ray.add_command(delete_ray)
ray.add_command(create_ray)
ray.add_command(make_profile_command("ray"))
ray.add_command(ray_batch)
ray.add_command(events_ray)
ray.add_command(instances_ray)


__all__ = ["ray"]
