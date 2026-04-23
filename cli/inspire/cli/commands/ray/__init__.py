"""Ray (弹性计算) commands for Inspire CLI.

Wraps the ``/api/v1/ray_job/*`` Browser API family surfaced by the web UI
under the "弹性计算" sidebar. Only read-only + lifecycle actions are wrapped
today (``list / status / stop / delete``); ``create`` still requires the
web UI because the elastic head+worker spec payload is proto-typed and the
schema isn't yet documented well enough to expose safely.
"""

from __future__ import annotations

import click

from .ray_commands import delete_ray, list_ray, status_ray, stop_ray


@click.group()
def ray() -> None:
    """Manage Ray (弹性计算) jobs — CPU decode + GPU inference streaming pipelines."""


ray.add_command(list_ray)
ray.add_command(status_ray)
ray.add_command(stop_ray)
ray.add_command(delete_ray)


__all__ = ["ray"]
