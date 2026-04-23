"""Models and parsers for the config schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ConfigOption:
    """A single configuration option with metadata.

    Attributes:
        env_var: Environment variable name
        toml_key: TOML configuration key (e.g., "auth.username")
        field_name: Config dataclass field name (e.g., "username")
        description: Human-readable description
        default: Default value (None if required)
        category: Configuration category for grouping
        secret: If True, value should be hidden in output
        parser: Optional function to parse string value to correct type
        validator: Optional function to validate the value
        scope: Configuration scope - "global" for user/machine-specific settings,
               "project" for per-codebase settings
    """

    env_var: str
    toml_key: str
    field_name: str
    description: str
    default: Any | None
    category: str
    secret: bool = False
    parser: Callable[[str], Any] | None = None
    validator: Callable[[Any], bool] | None = None
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


def _parse_list(value: str) -> list[str]:
    """Parse comma or newline separated list."""
    if not value:
        return []
    parts = []
    for raw in value.replace("\r", "").split("\n"):
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


def _parse_upload_policy(value: str) -> str:
    """Parse rtunnel upload policy."""
    normalized = str(value).strip().lower()
    if normalized in {"auto", "never", "always"}:
        return normalized
    raise ValueError(f"Invalid rtunnel upload policy: {value}")


def _normalize_rtunnel_bin(value: object) -> str | None:
    """Accept ``None``, a ``str``, or a ``list[str]`` and return a single
    ``:``-joined string (``$PATH``-style) or ``None``.

    This lets users configure multiple pre-cached rtunnel binaries that
    live in different storage partitions; the bootstrap script walks the
    list in order and uses the first candidate that exists. Empty /
    whitespace-only entries are dropped.

    Examples::

        _normalize_rtunnel_bin(None)                         # None
        _normalize_rtunnel_bin("/a/rtunnel")                 # "/a/rtunnel"
        _normalize_rtunnel_bin("/a/rtunnel:/b/rtunnel")      # "/a/rtunnel:/b/rtunnel"
        _normalize_rtunnel_bin(["/a/rtunnel", "/b/rtunnel"]) # "/a/rtunnel:/b/rtunnel"
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value if str(p).strip()]
        return ":".join(parts) or None
    s = str(value).strip()
    return s or None


def parse_value(option: ConfigOption, value: str) -> Any:
    """Parse a string value based on the option's parser."""
    if option.parser:
        try:
            return option.parser(value)
        except (ValueError, TypeError):
            return value
    return value
