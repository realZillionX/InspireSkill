"""Ray (弹性计算) commands for Inspire CLI.

Wraps the ``/api/v1/ray_job/*`` Browser API family surfaced by the web UI
under the "弹性计算" sidebar. The full lifecycle is covered: ``list / status /
stop / delete / create``. The create body shape was reverse-engineered from
the SPA's own submit handler (``constant.BP_zw-df.js`` on the ``/jobs/ray``
route) — see ``browser_api/ray_jobs.create_ray_job`` for the wire contract.
"""

from __future__ import annotations

import click

from .ray_commands import create_ray, delete_ray, list_ray, status_ray, stop_ray


@click.group()
def ray() -> None:
    """Manage Ray (弹性计算) jobs — CPU decode + GPU inference streaming pipelines."""


ray.add_command(list_ray)
ray.add_command(status_ray)
ray.add_command(stop_ray)
ray.add_command(delete_ray)
ray.add_command(create_ray)


__all__ = ["ray"]
