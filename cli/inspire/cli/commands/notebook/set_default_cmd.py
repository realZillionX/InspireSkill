"""Tunnel set-default command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("set-default")
@click.argument("name")
@pass_context
def tunnel_set_default(ctx: Context, name: str) -> None:
    """Set a bridge as the default.

    \b
    Example:
        inspire notebook set-default mybridge
    """
    config = load_tunnel_config()

    if name not in config.bridges:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "NotFound",
                    f"Bridge '{name}' not found",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Bridge '{name}' not found"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    config.default_bridge = name
    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "updated",
                    "default": name,
                }
            )
        )
        return

    click.echo(human_formatter.format_success(f"Default bridge set to: {name}"))
