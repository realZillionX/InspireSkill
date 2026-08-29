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
) -> str | None:
    env_password = os.getenv("INSPIRE_PASSWORD")

    for option in CONFIG_OPTIONS:
        if option.env_var == "INSPIRE_PASSWORD":
            continue

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

    return env_password


def _apply_password_fallback(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    env_password: str | None,
) -> None:
    """Apply the environment fallback for the password.

    The account-layer file is the primary source of ``password``; this
    stage only handles the env-var overrides that CI / scripts rely on.
    """
    if not config_dict.get("password") and env_password:
        config_dict["password"] = env_password
        sources["password"] = _env_source_for("INSPIRE_PASSWORD")


def _validate_required_config(
    *,
    config_dict: dict[str, Any],
    require_credentials: bool,
) -> None:
    if require_credentials:
        if not config_dict["username"] or not config_dict["password"]:
            raise ConfigError(
                "Missing platform credentials for the active account. Run "
                "`inspire account add <name>` to (re)configure them."
            )


__all__ = [
    "_apply_env_layer",
    "_apply_password_fallback",
    "_validate_required_config",
]
