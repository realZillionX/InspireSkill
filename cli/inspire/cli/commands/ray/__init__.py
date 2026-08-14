"""Ray (弹性计算) commands for Inspire CLI.

The command group covers the user-visible Ray lifecycle: list, status,
events, instances, metrics, create, stop, and delete. It intentionally presents the
platform object as a named elastic cluster instead of exposing transport or
form details.
"""

from __future__ import annotations

import click

from inspire.cli.commands.batch import ray_batch
from inspire.cli.commands.workload_quota import make_quota_command
from inspire.cli.commands.workload_profile import make_profile_command

from .ray_commands import (
    create_ray,
    delete_ray,
    events_ray,
    instances_ray,
    list_ray,
    start_ray,
    status_ray,
    stop_ray,
)
from .ray_metrics import ray_metrics


@click.group()
def ray() -> None:
    """Manage Ray (弹性计算) jobs with one head and elastic workers.

    A stopped job keeps its cluster spec: `stop` then `start` reuses it.

    Use Ray only when the workload needs a long-running driver, elastic
    worker groups, streaming processing, or heterogeneous CPU/GPU workers.
    Fixed GPU training normally belongs in `job`; fixed CPU batch work
    normally belongs in `hpc`.

    \b
    Examples:
        inspire ray quota --workspace CPU资源空间
        inspire ray create -n pipeline -c "python driver.py" --workspace CPU资源空间 --project CI-情境智能 --image ray-base:v1 --group HPC-可上网区资源-2 --quota 0,4,16 --worker "name=workers;image=ray-base:v1;group=HPC-可上网区资源-2;quota=0,20,80;min=1;max=4"
        inspire ray events pipeline --workspace CPU资源空间 --tail 50
        inspire ray instances pipeline --workspace CPU资源空间
        inspire ray metrics pipeline --workspace CPU资源空间 --window 30m
    """


ray.add_command(list_ray)
ray.add_command(status_ray)
ray.add_command(start_ray)
ray.add_command(stop_ray)
ray.add_command(delete_ray)
ray.add_command(create_ray)
ray.add_command(make_quota_command("ray"))
ray.add_command(make_profile_command("ray"))
ray.add_command(ray_batch)
ray.add_command(events_ray)
ray.add_command(instances_ray)
ray.add_command(ray_metrics)


__all__ = ["ray"]
