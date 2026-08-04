"""Config show command – display merged configuration with sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    Config,
    ConfigError,
    ConfigOption,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_ENV_FILE,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    get_categories,
    get_options_by_category,
)
from inspire.platform.web.session.proxy import (
    describe_effective_proxy_config,
    redact_proxy_url,
)

from .proxy_output import format_effective_proxy_lines

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_LABELS: dict[str, tuple[str, str]] = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_GLOBAL: ("global", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
    SOURCE_ENV_FILE: ("env-file", "magenta"),
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
    if option.category == "Paths" and is_set:
        return "<configured>", True
    if option.category == "Proxy" and value:
        return redact_proxy_url(value), is_set
    if value is None:
        return "(not set)", False
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)", is_set
    return str(value), is_set


def _get_source_for_option(sources: dict[str, str], option: ConfigOption) -> str:
    field_name = option.field_name
    return sources.get(field_name, SOURCE_DEFAULT) if field_name else SOURCE_DEFAULT


def _is_explicitly_configured(
    cfg: Config,
    sources: dict[str, str],
    option: ConfigOption,
) -> bool:
    _value, is_set = _get_field_value(cfg, option)
    return is_set and _get_source_for_option(sources, option) != SOURCE_DEFAULT


def _show_table(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
    compact: bool,
    filter_category: str | None,
    effective_proxy: dict[str, Any] | None,
    details: bool,
) -> None:
    click.echo(click.style("Configuration", bold=True))
    if details:
        click.echo(
            "Files: "
            f"global={'yes' if global_path else 'no'} "
            f"project={'yes' if project_path else 'no'}"
        )
        prefer_source = getattr(cfg, "prefer_source", "env")
        precedence = (
            "project TOML wins" if prefer_source == "toml" else "environment wins"
        )
        click.echo(f"Precedence: {precedence}")
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

        if not details:
            options = [
                opt for opt in options if _is_explicitly_configured(cfg, sources, opt)
            ]
            if not options:
                continue
        elif compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        category_items: list[tuple[ConfigOption, str, str, str]] = []
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            source = _get_source_for_option(sources, option)
            source_label, source_color = SOURCE_LABELS.get(source, ("?", "white"))
            value_display = json_formatter.sanitize_text(
                value_str or "(not set)",
                redact_paths=True,
            )
            max_value_len = max(max_value_len, len(value_display))
            category_items.append((option, value_display, source_label, source_color))

        display_data.append((category, category_items))

    for category, items in display_data:
        click.echo(click.style(category, bold=True, fg="blue"))

        for option, value_display, source_label, source_color in items:
            key_display = option.env_var.ljust(30)
            if details:
                value_padded = value_display.ljust(max_value_len)
                source_display = click.style(f"[{source_label}]", fg=source_color)
                click.echo(f"  {key_display} {value_padded} {source_display}")
            else:
                click.echo(f"  {key_display} {value_display}")

        click.echo()

    if effective_proxy is not None:
        for line in format_effective_proxy_lines(effective_proxy):
            click.echo(line)
        click.echo()

    if details:
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
    effective_proxy: dict[str, Any] | None,
    details: bool,
) -> None:
    result: dict[str, Any] = {"values": {}}
    if details:
        result.update(
            {
                "config_files": {
                    "global": str(global_path) if global_path else None,
                    "project": str(project_path) if project_path else None,
                    "project_shared": (
                        str(getattr(cfg, "_shared_project_config_path", None))
                        if getattr(cfg, "_shared_project_config_path", None)
                        else None
                    ),
                    "project_account": (
                        str(getattr(cfg, "_account_project_config_path", None))
                        if getattr(cfg, "_account_project_config_path", None)
                        else None
                    ),
                },
                "prefer_source": getattr(cfg, "prefer_source", "env"),
            }
        )
    if effective_proxy is not None:
        result["effective_proxy"] = effective_proxy
    if details:
        try:
            from inspire.cli.env_bootstrap import loaded_env_file_path

            env_file = loaded_env_file_path()
        except Exception:
            env_file = None
        result["env_file"] = str(env_file) if env_file else None

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
            if not details and not _is_explicitly_configured(cfg, sources, option):
                continue
            if details and compact and not is_set:
                continue

            source = _get_source_for_option(sources, option)
            public_value = (
                value_str
                if not option.secret
                else ("********" if value_str != "(not set)" else None)
            )
            if details:
                result["values"][option.env_var] = {
                    "value": public_value,
                    "source": source,
                    "toml_key": option.toml_key,
                    "description": option.description,
                }
            else:
                result["values"][option.env_var] = public_value

    click.echo(json_formatter.format_json(result))


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
            value_str, is_set = _get_field_value(cfg, option)
            if option.secret:
                click.echo(f"# {option.env_var}=<secret>")
            elif option.category == "Proxy" and is_set:
                click.echo(f"# {option.env_var}=<configured; redacted>")
            elif value_str and value_str != "(not set)":
                if " " in value_str or "," in value_str:
                    click.echo(f'{option.env_var}="{value_str}"')
                else:
                    click.echo(f"{option.env_var}={value_str}")
            else:
                click.echo(f"# {option.env_var}=")
        click.echo()


def _filter_includes_proxy(filter_category: str | None) -> bool:
    if filter_category is None:
        return True
    return filter_category.lower() in "proxy"


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("show")
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
@click.option(
    "--details",
    is_flag=True,
    help="Include config-file presence, precedence, value sources, and descriptions.",
)
@pass_context
def show_config(
    ctx: Context,
    output_format: str,
    compact: bool,
    filter_category: str | None,
    details: bool,
) -> None:
    """Display effective configuration.

    The default output contains only explicitly configured values. Use
    --details to include defaults, sources, descriptions, config-file
    presence, precedence, and the effective runtime proxy summary.

    \b
    Examples:
        inspire config show
        inspire --json config show
        inspire config show --format json
        inspire config show --filter API
        inspire config show --compact
    """
    effective_json = ctx.json_output

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False
        )
        global_path, project_path = Config.get_config_paths()

        if effective_json:
            output_format = "json"

        show_details = details
        effective_proxy = None
        if (
            output_format in {"table", "json"}
            and _filter_includes_proxy(filter_category)
            and (show_details or filter_category is not None)
        ):
            effective_proxy = describe_effective_proxy_config(base_url=cfg.base_url)

        if output_format == "json":
            _show_json(
                cfg,
                sources,
                global_path,
                project_path,
                compact,
                filter_category,
                effective_proxy,
                show_details,
            )
        elif output_format == "env":
            _show_env(cfg, compact, filter_category)
        else:
            _show_table(
                cfg,
                sources,
                global_path,
                project_path,
                compact,
                filter_category,
                effective_proxy,
                show_details,
            )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
