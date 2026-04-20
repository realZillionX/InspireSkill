"""Configuration schema for Inspire CLI.

Defines all environment variables and TOML configuration keys with metadata for documentation,
validation, and config file generation.

The option list is split across smaller per-category modules for readability.
"""

from __future__ import annotations

from inspire.config.schema_categories import CATEGORY_ORDER  # noqa: F401
from inspire.config.schema_models import (  # noqa: F401
    ConfigOption,
    _parse_bool,
    _parse_float,
    _parse_int,
    _parse_list,
    parse_value,
)
from inspire.config.options.api import API_OPTIONS, AUTH_OPTIONS, PROXY_OPTIONS
from inspire.config.options.forge import GITHUB_OPTIONS
from inspire.config.options.infra import SSH_OPTIONS, TUNNEL_OPTIONS, BRIDGE_OPTIONS, PATHS_OPTIONS
from inspire.config.options.project import (
    JOB_OPTIONS,
    NOTEBOOK_OPTIONS,
    SYNC_OPTIONS,
    WORKSPACES_OPTIONS,
)

# All configuration options organized by category.
CONFIG_OPTIONS: list[ConfigOption] = [
    *AUTH_OPTIONS,
    *API_OPTIONS,
    *PROXY_OPTIONS,
    *PATHS_OPTIONS,
    *GITHUB_OPTIONS,
    *SYNC_OPTIONS,
    *BRIDGE_OPTIONS,
    *WORKSPACES_OPTIONS,
    *JOB_OPTIONS,
    *NOTEBOOK_OPTIONS,
    *SSH_OPTIONS,
    *TUNNEL_OPTIONS,
]


def get_options_by_category(category: str) -> list[ConfigOption]:
    """Get all configuration options for a category."""
    return [opt for opt in CONFIG_OPTIONS if opt.category == category]


def get_option_by_env(env_var: str) -> ConfigOption | None:
    """Get configuration option by environment variable name."""
    for opt in CONFIG_OPTIONS:
        if opt.env_var == env_var:
            return opt
    return None


def get_option_by_toml(toml_key: str) -> ConfigOption | None:
    """Get configuration option by TOML key."""
    for opt in CONFIG_OPTIONS:
        if opt.toml_key == toml_key:
            return opt
    return None


def get_categories() -> list[str]:
    """Get all unique categories in order."""
    return [cat for cat in CATEGORY_ORDER if any(opt.category == cat for opt in CONFIG_OPTIONS)]


def get_required_options() -> list[ConfigOption]:
    """Get all required configuration options (no default)."""
    return [opt for opt in CONFIG_OPTIONS if opt.default is None]


def get_secret_options() -> list[ConfigOption]:
    """Get all secret configuration options."""
    return [opt for opt in CONFIG_OPTIONS if opt.secret]


def get_options_by_scope(scope: str) -> list[ConfigOption]:
    """Get all configuration options for a given scope.

    Args:
        scope: Either "global" or "project"

    Returns:
        List of ConfigOption with matching scope
    """
    return [opt for opt in CONFIG_OPTIONS if opt.scope == scope]
