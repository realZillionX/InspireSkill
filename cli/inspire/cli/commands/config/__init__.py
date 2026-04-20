"""Configuration commands for Inspire CLI."""

from __future__ import annotations

import click

from .check import check_config
from .context import show_context
from .env_cmd import generate_env
from .show import show_config


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""


config.add_command(show_config)
config.add_command(show_context)
config.add_command(generate_env)
config.add_command(check_config)

__all__ = ["config"]
