"""Project-level remote path alias commands."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.path_aliases import (
    delete_project_path_alias,
    load_project_path_aliases,
    write_project_path_alias,
)


def _resolve_alias(name: str, aliases: dict[str, str]) -> tuple[str, str]:
    value = aliases.get(name)
    if value is not None:
        return name, value
    available = ", ".join(sorted(aliases)) or "(none)"
    raise ConfigError(f"Unknown path alias: {name!r}. Available: {available}")


@click.group("path")
def path_aliases_cmd() -> None:
    """Manage project-level remote path aliases.

    Path aliases live in this repository's account-scoped project config
    under [path_aliases]. They are shared by notebook exec/shell/scp and job
    log paths for the active account; they are not bound to any one notebook
    instance.
    """


@click.command("list")
@pass_context
def list_path_aliases(ctx: Context) -> None:
    """List project-level remote path aliases."""
    try:
        _config_path, aliases = load_project_path_aliases()
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"aliases": dict(sorted(aliases.items()))}
                )
            )
            return
        if not aliases:
            click.echo("No project path aliases found.")
            return
        for alias, remote_path in sorted(aliases.items()):
            click.echo(f"  {scrub_raw_ids(alias)}  {scrub_raw_ids(remote_path)}")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("show")
@click.argument("alias")
@pass_context
def show_path_alias(ctx: Context, alias: str) -> None:
    """Show one project-level remote path alias."""
    try:
        _config_path, aliases = load_project_path_aliases()
        resolved_alias, remote_path = _resolve_alias(alias, aliases)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"alias": resolved_alias, "path": remote_path}
                )
            )
            return
        click.echo(f"Path alias: {scrub_raw_ids(resolved_alias)}")
        click.echo(f"  path: {scrub_raw_ids(remote_path)}")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("set")
@click.argument("alias")
@click.argument("remote_path")
@pass_context
def set_path_alias(ctx: Context, alias: str, remote_path: str) -> None:
    """Create or replace a project-level remote path alias."""
    try:
        write_project_path_alias(alias=alias, remote_path=remote_path)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"alias": alias, "path": remote_path, "status": "saved"}
                )
            )
            return
        click.echo(f"Path alias '{scrub_raw_ids(alias)}' = {scrub_raw_ids(remote_path)}")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("delete")
@click.argument("alias")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@pass_context
def delete_path_alias(ctx: Context, alias: str, yes: bool) -> None:
    """Delete one project-level remote path alias."""
    try:
        if not yes and not ctx.json_output:
            click.confirm(f"Delete path alias '{scrub_raw_ids(alias)}'?", abort=True)
        delete_project_path_alias(alias)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"alias": alias, "status": "deleted"}
                )
            )
            return
        click.echo(f"Deleted path alias: {scrub_raw_ids(alias)}")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


path_aliases_cmd.add_command(list_path_aliases)
path_aliases_cmd.add_command(show_path_alias)
path_aliases_cmd.add_command(set_path_alias)
path_aliases_cmd.add_command(delete_path_alias)


__all__ = ["path_aliases_cmd"]
