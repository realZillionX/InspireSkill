"""Project-level remote path alias commands."""

from __future__ import annotations

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_mutation_success
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
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
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum aliases to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every path alias.")
@pass_context
def list_path_aliases(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List project-level remote path aliases."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        _config_path, aliases = load_project_path_aliases()
        rows = [{"name": scrub_raw_ids(alias)} for alias in sorted(aliases)]
        page = bound_collection(
            rows,
            limit=effective_limit,
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return
        if not page.items:
            click.echo("No project path aliases found.")
            return
        for item in page.items:
            click.echo(item["name"])
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("show")
@click.argument("alias", metavar="ALIAS")
@pass_context
def show_path_alias(ctx: Context, alias: str) -> None:
    """Reveal the remote path stored for one alias."""
    try:
        _config_path, aliases = load_project_path_aliases()
        resolved_alias, remote_path = _resolve_alias(alias, aliases)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(resolved_alias), "path": scrub_raw_ids(remote_path)}
                )
            )
            return
        click.echo(f"Path alias: {scrub_raw_ids(resolved_alias)}")
        click.echo(f"  path: {scrub_raw_ids(remote_path)}")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("set")
@click.argument("alias", metavar="ALIAS")
@click.argument("remote_path", metavar="REMOTE_PATH")
@pass_context
def set_path_alias(ctx: Context, alias: str, remote_path: str) -> None:
    """Create or replace a project-level remote path alias."""
    try:
        write_project_path_alias(alias=alias, remote_path=remote_path)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(alias),
                        "status": "saved",
                    }
                )
            )
            return
        click.echo(format_mutation_success("Path alias", "saved", alias))
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


@click.command("delete")
@click.argument("alias", metavar="ALIAS")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def delete_path_alias(ctx: Context, alias: str, yes: bool) -> None:
    """Delete one project-level remote path alias."""
    try:
        require_confirmation(
            ctx,
            yes=yes,
            prompt=f"Delete path alias '{scrub_raw_ids(alias)}'?",
            message="Path alias deletion requires confirmation.",
        )
        delete_project_path_alias(alias)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(alias), "status": "deleted"}
                )
            )
            return
        click.echo(format_mutation_success("Path alias", "deleted", alias))
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)


path_aliases_cmd.add_command(list_path_aliases)
path_aliases_cmd.add_command(show_path_alias)
path_aliases_cmd.add_command(set_path_alias)
path_aliases_cmd.add_command(delete_path_alias)


__all__ = ["path_aliases_cmd"]
