"""Repository-level configuration commands for Inspire CLI."""

from __future__ import annotations

import click

from .show import show_config


@click.group()
def config() -> None:
    """Inspect this repository's Inspire CLI defaults."""


config.add_command(show_config)

__all__ = ["config"]
