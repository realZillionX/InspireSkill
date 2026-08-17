"""Configuration schema for Inspire CLI.

Maps every settable key across its three spellings — environment variable,
TOML key, and ``Config`` field — so the loader and ``inspire init`` agree on
what exists. Defaults live in ``load_common._default_config_values()``.

The option list is split across smaller per-area modules for readability.
"""

from __future__ import annotations

from inspire.config.schema_models import (  # noqa: F401
    ConfigOption,
    _parse_bool,
    _parse_float,
    _parse_int,
    parse_value,
)
from inspire.config.options.api import API_OPTIONS, AUTH_OPTIONS, PROXY_OPTIONS
from inspire.config.options.infra import TUNNEL_OPTIONS
from inspire.config.options.project import (
    JOB_OPTIONS,
    NOTEBOOK_OPTIONS,
)

CONFIG_OPTIONS: list[ConfigOption] = [
    *AUTH_OPTIONS,
    *API_OPTIONS,
    *PROXY_OPTIONS,
    *JOB_OPTIONS,
    *NOTEBOOK_OPTIONS,
    *TUNNEL_OPTIONS,
]


def get_option_by_toml(toml_key: str) -> ConfigOption | None:
    """Get configuration option by TOML key."""
    for opt in CONFIG_OPTIONS:
        if opt.toml_key == toml_key:
            return opt
    return None
