"""Config show command – display merged configuration with sources."""

from __future__ import annotations

import json
from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    Config,
    ConfigError,
    ConfigOption,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    get_categories,
    get_options_by_category,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_LABELS: dict[str, tuple[str, str]] = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_GLOBAL: ("global", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_field_value(cfg: Config, option: ConfigOption) -> tuple[str | None, bool]:
    field_name = option.field_name
    if not field_name or not hasattr(cfg, field_name):
        return None, False

    value = getattr(cfg, field_name)

    is_set = value is not None and value != "" and value != []

    if option.secret and value:
        return "********", is_set
    if value is None:
        return "(not set)", False
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)", is_set
    return str(value), is_set


def _get_source_for_option(sources: dict[str, str], option: ConfigOption) -> str:
    field_name = option.field_name
    return sources.get(field_name, SOURCE_DEFAULT) if field_name else SOURCE_DEFAULT


def _show_table(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
    compact: bool,
    filter_category: str | None,
) -> None:
    click.echo(click.style("Configuration Overview", bold=True))
    click.echo()

    click.echo("Config files:")
    if global_path:
        click.echo(f"  Global:  {global_path} " + click.style("(found)", fg="green"))
    else:
        click.echo(
            "  Global:  ~/.config/inspire/config.toml " + click.style("(not found)", fg="white")
        )
    if project_path:
        click.echo(f"  Project: {project_path} " + click.style("(found)", fg="green"))
    else:
        click.echo("  Project: ./inspire/config.toml " + click.style("(not found)", fg="white"))

    prefer_source = getattr(cfg, "prefer_source", "env")
    if prefer_source == "toml":
        click.echo("  Precedence: " + click.style("project TOML wins", fg="green") + " on conflict")
    else:
        click.echo(
            "  Precedence: " + click.style("env vars win", fg="yellow") + " on conflict (default)"
        )

    click.echo()

    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]
        if not categories:
            click.echo(click.style(f"No category matching '{filter_category}'", fg="red"))
            return

    display_data: list[tuple[str, list[tuple[ConfigOption, str, str, str]]]] = []
    max_value_len = 40

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        if compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        category_items: list[tuple[ConfigOption, str, str, str]] = []
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            source = _get_source_for_option(sources, option)
            source_label, source_color = SOURCE_LABELS.get(source, ("?", "white"))
            value_display = value_str or "(not set)"
            max_value_len = max(max_value_len, len(value_display))
            category_items.append((option, value_display, source_label, source_color))

        display_data.append((category, category_items))

    for category, items in display_data:
        click.echo(click.style(category, bold=True, fg="blue"))

        for option, value_display, source_label, source_color in items:
            key_display = option.env_var.ljust(30)
            value_padded = value_display.ljust(max_value_len)
            source_display = click.style(f"[{source_label}]", fg=source_color)

            click.echo(f"  {key_display} {value_padded} {source_display}")

        click.echo()

    click.echo(click.style("Legend:", dim=True))
    legend_parts = []
    for _source, (label, color) in SOURCE_LABELS.items():
        legend_parts.append(click.style(f"[{label}]", fg=color))
    click.echo("  " + " ".join(legend_parts))


def _show_json(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
    compact: bool,
    filter_category: str | None,
) -> None:
    result = {
        "config_files": {
            "global": str(global_path) if global_path else None,
            "project": str(project_path) if project_path else None,
        },
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "values": {},
    }

    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        for option in options:
            value_str, is_set = _get_field_value(cfg, option)
            if compact and not is_set:
                continue

            source = _get_source_for_option(sources, option)
            result["values"][option.env_var] = {
                "value": (
                    value_str
                    if not option.secret
                    else ("********" if value_str != "(not set)" else None)
                ),
                "source": source,
                "toml_key": option.toml_key,
                "description": option.description,
            }

    click.echo(json.dumps(result, indent=2))


def _show_env(cfg: Config, compact: bool, filter_category: str | None) -> None:
    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        if compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        click.echo(f"# {category}")
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            if option.secret:
                click.echo(f"# {option.env_var}=<secret>")
            elif value_str and value_str != "(not set)":
                if " " in value_str or "," in value_str:
                    click.echo(f'{option.env_var}="{value_str}"')
                else:
                    click.echo(f"{option.env_var}={value_str}")
            else:
                click.echo(f"# {option.env_var}=")
        click.echo()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("show")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to top-level --json.",
)
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["table", "json", "env"]),
    default="table",
    help="Output format (table, json, env)",
)
@click.option(
    "--compact",
    "-c",
    is_flag=True,
    help="Hide unset options",
)
@click.option(
    "--filter",
    "-F",
    "filter_category",
    help="Filter by category (e.g., 'API', 'GitHub')",
)
@pass_context
def show_config(
    ctx: Context,
    json_output_local: bool,
    output_format: str,
    compact: bool,
    filter_category: str | None,
) -> None:
    """Display merged configuration with value sources.

    Shows configuration values from all sources (defaults, global config,
    project config, environment variables) with clear indication of where
    each value comes from.

    By default, all options are shown including unset ones. Use --compact
    to hide unset options.

    \b
    Examples:
        inspire config show
        inspire config show --format json
        inspire config show --json
        inspire config show --filter API
        inspire config show --compact
    """
    ctx.json_output = bool(ctx.json_output or json_output_local)
    effective_json = ctx.json_output

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False, require_target_dir=False
        )
        global_path, project_path = Config.get_config_paths()

        if effective_json:
            output_format = "json"

        if output_format == "json":
            _show_json(cfg, sources, global_path, project_path, compact, filter_category)
        elif output_format == "env":
            _show_env(cfg, compact, filter_category)
        else:
            _show_table(cfg, sources, global_path, project_path, compact, filter_category)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
