"""Model registry commands for Inspire CLI."""

from __future__ import annotations

import click

from .model_commands import list_model, status_model, versions_model


@click.group()
def model() -> None:
    """Browse the platform model registry.

    Read-only commands for inspecting models and their versions on the
    `/modelLibrary` + `/jobs/modelDeployment` pages. Backed entirely by the
    Browser API — no OpenAPI counterpart exists.
    """


model.add_command(list_model)
model.add_command(status_model)
model.add_command(versions_model)


__all__ = ["model"]
