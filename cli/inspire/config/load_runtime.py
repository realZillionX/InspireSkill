"""Environment and validation helpers for config loading."""

from __future__ import annotations

import os
from typing import Any

from inspire.config.models import SOURCE_ENV, SOURCE_ENV_FILE, ConfigError
from inspire.config.schema import CONFIG_OPTIONS

def _env_source_for(key: str) -> str:
    try:
        from inspire.cli.env_bootstrap import is_env_file_key

        return SOURCE_ENV_FILE if is_env_file_key(key) else SOURCE_ENV
    except Exception:
        return SOURCE_ENV


def _apply_env_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> None:

    for option in CONFIG_OPTIONS:
        source_key = option.env_var
        value = os.getenv(option.env_var)
        if value is None:
            continue

        field_name = option.field_name
        if field_name not in config_dict:
            continue

        if option.parser:
            try:
                parsed_value = option.parser(value)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"Invalid {option.env_var} value: {value}") from e
            new_value = parsed_value
        else:
            new_value = value

        config_dict[field_name] = new_value
        sources[field_name] = _env_source_for(source_key)
