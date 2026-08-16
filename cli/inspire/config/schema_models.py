"""Models and parsers for the config schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ConfigOption:
    """One settable key, in all three of the spellings the CLI accepts.

    Defaults are not recorded here. They live in
    ``load_common._default_config_values()``, which is what the loader actually
    reads; a copy on this dataclass was a second source of truth that silently
    drifted.

    Attributes:
        env_var: Environment variable name
        toml_key: TOML configuration key (e.g., "auth.username")
        field_name: Config dataclass field name (e.g., "username")
        secret: If True, never write the value into a generated config file
        parser: Optional function to parse string value to correct type
        scope: Configuration scope - "global" for account-wide settings,
               "project" for per-codebase settings
    """

    env_var: str
    toml_key: str
    field_name: str
    secret: bool = False
    parser: Callable[[str], Any] | None = None
    scope: str = "project"


def _parse_int(value: str) -> int:
    """Parse string to integer."""
    return int(value)


def _parse_float(value: str) -> float:
    """Parse string to float."""
    return float(value)


def _parse_bool(value: str) -> bool:
    """Parse string to boolean."""
    return value.lower() in ("1", "true", "yes", "on")


def parse_value(option: ConfigOption, value: str) -> Any:
    """Parse a string value based on the option's parser."""
    if option.parser:
        try:
            return option.parser(value)
        except (ValueError, TypeError):
            return value
    return value
