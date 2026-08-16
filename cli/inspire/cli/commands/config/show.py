"""Config show command – this repository's workload defaults and where they came from."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.config_display import (
    echo_groups,
    echo_source_legend,
    json_values,
    matching_categories,
    select_groups,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError


@click.command("show")
@click.option(
    "--filter",
    "-F",
    "filter_category",
    metavar="CATEGORY",
    help="Filter by category (e.g., 'Job', 'Notebook').",
)
@click.option(
    "--details",
    is_flag=True,
    help="Include defaults, value sources, descriptions, and config-file presence.",
)
@pass_context
def show_config(ctx: Context, filter_category: str | None, details: bool) -> None:
    """Show this repository's workload defaults and where each one came from.

    These are the values `<workload> create` falls back to when a flag is
    omitted. They resolve across four layers — repo shared config, per-account
    repo override, dotenv, and environment — so the effective value often
    lives in none of the files you would think to open. Account identity,
    API, and proxy settings are shown by `inspire account show`.

    \b
    Examples:
        inspire config show
        inspire config show --details
        inspire --json config show
    """
    try:
        cfg, sources = Config.from_files_and_env(require_credentials=False)
        if filter_category and not matching_categories(
            scope="project", filter_category=filter_category
        ):
            _handle_error(
                ctx,
                "ValidationError",
                f"No category matching '{filter_category}'.",
                EXIT_VALIDATION_ERROR,
                hint="Categories: Job, Notebook.",
            )
            return

        _account_path, project_path = Config.get_config_paths()
        groups = select_groups(
            cfg,
            sources,
            scope="project",
            filter_category=filter_category,
            details=details,
        )

        if ctx.json_output:
            result: dict[str, Any] = {
                "values": json_values(cfg, sources, groups, details=details)
            }
            if details:
                # Only presence, never the paths: `config_files` / `env_file`
                # are on the JSON formatter's engineering-key deny list and
                # would be dropped from the payload.
                result["project_config_present"] = bool(project_path)
                result["env_file_present"] = _env_file_loaded()
                result["prefer_source"] = getattr(cfg, "prefer_source", "env")
            click.echo(json_formatter.format_json(result))
            return

        if details:
            prefer_source = getattr(cfg, "prefer_source", "env")
            precedence = (
                "project TOML wins" if prefer_source == "toml" else "environment wins"
            )
            click.echo(f"Project config: {'yes' if project_path else 'no'}")
            click.echo(f"Precedence: {precedence}")
            click.echo()

        if not groups:
            click.echo("No workload defaults configured for this repository.")
            return

        echo_groups(cfg, sources, groups, details=details)

        if details:
            echo_source_legend()

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _env_file_loaded() -> bool:
    try:
        from inspire.cli.env_bootstrap import loaded_env_file_path

        return bool(loaded_env_file_path())
    except Exception:
        return False
