"""Tunnel remove command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("remove")
@click.argument("notebook", metavar="NOTEBOOK")
@pass_context
def tunnel_remove(ctx: Context, notebook: str) -> None:
    """Drop a cached notebook connection.

    NOTEBOOK is the cached notebook name.

    \b
    Example:
        inspire notebook forget my-notebook
    """
    name = notebook
    config = load_tunnel_config()

    if name not in config.bridges:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "NotFound",
                    f"No cached notebook connection for '{name}'",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(
                human_formatter.format_error(
                    f"No cached notebook connection for '{name}'"
                ),
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)

    was_default = name == config.default_bridge
    config.remove_bridge(name)
    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "removed",
                    "notebook": name,
                    "new_default": config.default_bridge,
                }
            )
        )
        return

    click.echo(f"Removed cached notebook: {name}")
    if was_default and config.default_bridge:
        click.echo(f"New default: {config.default_bridge}")
