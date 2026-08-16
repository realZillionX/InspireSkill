"""Rendering for ``inspire account show``.

Only account-scope options surface here. Repository-scope keys resolve across
four layers and are read by the workload commands themselves; they have no
inspection view.
"""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.formatters import json_formatter
from inspire.config import (
    Config,
    ConfigOption,
    SOURCE_ACCOUNT,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_ENV_FILE,
    SOURCE_PROJECT,
    get_categories,
    get_options_by_scope,
)

SOURCE_LABELS: dict[str, tuple[str, str]] = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_ACCOUNT: ("account", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
    SOURCE_ENV_FILE: ("env-file", "magenta"),
}

# Categories whose values identify a person or a route into the platform. The
# view reports whether they are set, never what they are set to.
_PRESENCE_ONLY_CATEGORIES = {"API", "Authentication", "Proxy"}

# The schema's name for keys that live in ~/.inspire/accounts/<name>/config.toml.
_ACCOUNT_SCOPE = "global"

_CategoryGroup = tuple[str, list[ConfigOption]]


def option_value(cfg: Config, option: ConfigOption) -> tuple[str | None, bool]:
    """Return the display string for *option* and whether it is set."""
    field_name = option.field_name
    if not field_name or not hasattr(cfg, field_name):
        return None, False

    value = getattr(cfg, field_name)
    is_set = value is not None and value != "" and value != []

    if option.secret and value:
        return "********", is_set
    if value is None:
        return "(not set)", False
    if option.category in _PRESENCE_ONLY_CATEGORIES and is_set:
        return "<configured>", True
    if isinstance(value, list):
        raw_value = ", ".join(str(item) for item in value) if value else "(empty)"
    else:
        raw_value = str(value)
    return (
        json_formatter.sanitize_text(
            raw_value,
            redact_paths=True,
            redact_urls=True,
            redact_platform_paths=True,
        ),
        is_set,
    )


def option_source(sources: dict[str, str], option: ConfigOption) -> str:
    field_name = option.field_name
    return sources.get(field_name, SOURCE_DEFAULT) if field_name else SOURCE_DEFAULT


def is_explicitly_configured(
    cfg: Config,
    sources: dict[str, str],
    option: ConfigOption,
) -> bool:
    _value, is_set = option_value(cfg, option)
    return is_set and option_source(sources, option) != SOURCE_DEFAULT


def matching_categories(filter_category: str | None) -> list[str]:
    """Account-scope categories that ``--filter`` selects, before any value check.

    Kept separate from :func:`select_groups` so callers can tell "that category
    does not exist" apart from "nothing in it is configured".
    """
    scoped = get_options_by_scope(_ACCOUNT_SCOPE)
    categories = [c for c in get_categories() if any(opt.category == c for opt in scoped)]
    if not filter_category:
        return categories
    needle = filter_category.lower()
    return [c for c in categories if needle in c.lower()]


def select_groups(
    cfg: Config,
    sources: dict[str, str],
    *,
    filter_category: str | None,
    details: bool,
) -> list[_CategoryGroup]:
    """Group the account-scope schema into the categories to render.

    Without ``--details`` only explicitly configured options survive, so the
    default view answers "what did I actually set" rather than reprinting the
    schema.
    """
    scoped = get_options_by_scope(_ACCOUNT_SCOPE)
    categories = matching_categories(filter_category)

    groups: list[_CategoryGroup] = []
    for category in categories:
        options = [opt for opt in scoped if opt.category == category]
        if not details:
            options = [opt for opt in options if is_explicitly_configured(cfg, sources, opt)]
        if options:
            groups.append((category, options))
    return groups


def echo_groups(
    cfg: Config,
    sources: dict[str, str],
    groups: list[_CategoryGroup],
    *,
    details: bool,
) -> None:
    """Print *groups* as an aligned table, optionally with source labels."""
    rendered = [
        (
            category,
            [(option, option_value(cfg, option)[0] or "(not set)") for option in options],
        )
        for category, options in groups
    ]
    value_width = max(
        (len(value) for _category, items in rendered for _option, value in items),
        default=0,
    )
    key_width = max(
        (len(option.env_var) for _category, items in rendered for option, _value in items),
        default=0,
    )

    for category, items in rendered:
        click.echo(click.style(category, bold=True, fg="blue"))
        for option, value in items:
            key = option.env_var.ljust(key_width)
            if details:
                label, color = SOURCE_LABELS.get(
                    option_source(sources, option), ("?", "white")
                )
                source = click.style(f"[{label}]", fg=color)
                click.echo(f"  {key} {value.ljust(value_width)} {source}")
            else:
                click.echo(f"  {key} {value}")
        click.echo()


def echo_source_legend() -> None:
    click.echo(click.style("Legend:", dim=True))
    parts = [click.style(f"[{label}]", fg=color) for label, color in SOURCE_LABELS.values()]
    click.echo("  " + " ".join(parts))


def json_values(
    cfg: Config,
    sources: dict[str, str],
    groups: list[_CategoryGroup],
    *,
    details: bool,
) -> dict[str, Any]:
    """Build the ``values`` payload for ``--json``."""
    values: dict[str, Any] = {}
    for _category, options in groups:
        for option in options:
            value, is_set = option_value(cfg, option)
            public_value = value if is_set else None
            if details:
                values[option.env_var] = {
                    "value": public_value,
                    "source": option_source(sources, option),
                    "toml_key": option.toml_key,
                    "description": option.description,
                }
            else:
                values[option.env_var] = public_value
    return values


__all__ = [
    "SOURCE_LABELS",
    "echo_groups",
    "echo_source_legend",
    "is_explicitly_configured",
    "json_values",
    "matching_categories",
    "option_source",
    "option_value",
    "select_groups",
]
