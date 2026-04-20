"""Inference serving (model deployment) commands for Inspire CLI."""

from __future__ import annotations

import click

from .serving_commands import configs_serving, list_serving, status_serving, stop_serving


@click.group()
def serving() -> None:
    """Manage inference servings (model deployment).

    Covers the observability + lifecycle surface of `/jobs/modelDeployment`:
    `list` / `status` / `configs` use the Browser API (SSO session) and
    `status` / `stop` the OpenAPI (Bearer token) for parity with `job` / `hpc`.

    `create` is intentionally not wrapped — deployment configuration is
    platform-specific (model id, port, replicas, custom domain, ...). Use the
    Web UI at `/jobs/modelDeployment` or drive the OpenAPI directly.
    """


serving.add_command(list_serving)
serving.add_command(status_serving)
serving.add_command(stop_serving)
serving.add_command(configs_serving)


__all__ = ["serving"]
