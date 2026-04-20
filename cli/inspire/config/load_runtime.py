"""Environment and validation helpers for config loading."""

from __future__ import annotations

import os
from typing import Any

from inspire.config.models import SOURCE_ENV, SOURCE_GLOBAL, SOURCE_PROJECT, ConfigError
from inspire.config.schema import CONFIG_OPTIONS


def _apply_env_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    prefer_source: str,
) -> str | None:
    env_password = os.getenv("INSPIRE_PASSWORD")

    for option in CONFIG_OPTIONS:
        if option.env_var == "INSPIRE_PASSWORD":
            continue

        value = os.getenv(option.env_var)
        if value is None and option.env_var == "INSP_LOG_CACHE_DIR":
            value = os.getenv("INSPIRE_LOG_CACHE_DIR")
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

        if prefer_source == "toml" and sources.get(field_name) == SOURCE_PROJECT:
            continue

        config_dict[field_name] = new_value
        sources[field_name] = SOURCE_ENV

    return env_password


def _apply_password_and_token_fallbacks(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_accounts: dict[str, str],
    env_password: str | None,
) -> None:
    resolved_username = str(config_dict.get("username") or "").strip()
    account_password = config_dict.get("accounts", {}).get(resolved_username)
    if account_password:
        config_dict["password"] = account_password
        sources["password"] = (
            SOURCE_PROJECT if resolved_username in project_accounts else SOURCE_GLOBAL
        )

    if not config_dict.get("password") and env_password:
        config_dict["password"] = env_password
        sources["password"] = SOURCE_ENV

    if not config_dict.get("github_token"):
        github_token_fallback = os.getenv("GITHUB_TOKEN")
        if github_token_fallback:
            config_dict["github_token"] = github_token_fallback
            sources["github_token"] = SOURCE_ENV


def _validate_required_config(
    *,
    config_dict: dict[str, Any],
    require_credentials: bool,
    require_target_dir: bool,
) -> None:
    if require_credentials:
        if not config_dict["username"]:
            raise ConfigError(
                "Missing username configuration.\n"
                "Set INSPIRE_USERNAME env var or add to config.toml:\n"
                "  [auth]\n"
                "  username = 'your_username'"
            )
        if not config_dict["password"]:
            raise ConfigError(
                "Missing password configuration.\n"
                "Set INSPIRE_PASSWORD env var or add an account password in config.toml:\n"
                '  [accounts."your_username"]\n'
                "  password = 'your_password'"
            )

    if require_target_dir and not config_dict["target_dir"]:
        raise ConfigError(
            "Missing target directory configuration.\n"
            "Set INSPIRE_TARGET_DIR env var or add to config.toml:\n"
            "  [paths]\n"
            "  target_dir = '/path/to/shared/directory'"
        )


__all__ = [
    "_apply_env_layer",
    "_apply_password_and_token_fallbacks",
    "_validate_required_config",
]
