"""Shared account configuration rendering and persistence, without CLI dependencies."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from inspire.local_files import atomic_write_text as atomic_write_text
from inspire.config import DEFAULT_BASE_URL

_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_escape_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", lambda match: f"\\u{ord(match[0]):04x}", escaped
    )


def _toml_format_key(key: str) -> str:
    if _TOML_BARE_KEY_RE.match(key):
        return key
    return '"' + _toml_escape_string(key) + '"'


def _toml_format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return '"' + _toml_escape_string(value) + '"'
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        items = (f"{_toml_format_key(k)} = {_toml_format_value(v)}" for k, v in value.items())
        return "{ " + ", ".join(items) + " }"
    if isinstance(value, list):
        formatted = ", ".join(_toml_format_value(item) for item in value)
        return f"[{formatted}]"
    return '"' + _toml_escape_string(str(value)) + '"'


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


def _toml_dump_table(
    path: list[str], table: dict[str, Any], lines: list[str], *, array_item: bool = False
) -> None:
    scalar_items: list[tuple[str, Any]] = []
    nested_tables: list[tuple[str, dict[str, Any]]] = []
    array_tables: list[tuple[str, list[dict[str, Any]]]] = []

    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, dict):
            nested_tables.append((key, value))
            continue
        if _is_array_of_tables(value):
            array_tables.append((key, value))
            continue
        scalar_items.append((key, value))

    if path:
        header = ".".join(_toml_format_key(part) for part in path)
        lines.append(f"[[{header}]]" if array_item else f"[{header}]")

    for key, value in sorted(scalar_items, key=lambda item: item[0]):
        lines.append(f"{_toml_format_key(key)} = {_toml_format_value(value)}")

    if scalar_items and (nested_tables or array_tables):
        lines.append("")

    for key, subtable in sorted(nested_tables, key=lambda item: item[0]):
        _toml_dump_table([*path, key], subtable, lines)
        lines.append("")

    for key, items in sorted(array_tables, key=lambda item: item[0]):
        for item in items:
            _toml_dump_table([*path, key], item, lines, array_item=True)
            lines.append("")


def toml_dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _toml_dump_table([], data, lines)

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines) + "\n"


_USERNAME_PLACEHOLDERS = frozenset({"your_username"})
_BASE_URL_PLACEHOLDER = "https://api.example.com"
_OBSOLETE_ACCOUNT_TABLES = frozenset(
    {
        "compute_groups",
        "context",
        "path_aliases",
        "profiles",
        "project_catalog",
        "projects",
        "paths",
    }
)
_OBSOLETE_ACCOUNT_TABLE_FIELDS: dict[str, frozenset[str]] = {
    "api": frozenset({"docker_registry"}),
}


def sanitize_account_config(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Drop retired repository-derived and unused account fields."""
    cleaned: dict[str, Any] = {}
    for key, raw_value in raw_data.items():
        if key in _OBSOLETE_ACCOUNT_TABLES:
            continue
        if not isinstance(raw_value, dict):
            cleaned[key] = raw_value
            continue
        table = dict(raw_value)
        for field in _OBSOLETE_ACCOUNT_TABLE_FIELDS.get(key, frozenset()):
            table.pop(field, None)
        if table:
            cleaned[key] = table
    return cleaned


ACCOUNT_CONFIG_TEMPLATE = f"""# Inspire CLI Account Configuration
# Account-level values are independent of the current repository.
# Live project/resource catalogs are never copied here.
#
# Values here are overridden by environment variables.
# Sensitive values (passwords, tokens) should use env vars.

[auth]
username = "your_username"
# password - use INSPIRE_PASSWORD env var

[api]
base_url = "{DEFAULT_BASE_URL}"

[proxy]
# Proxy is OPTIONAL. Leave commented if your network can reach *.sii.edu.cn directly.
# Replace 7897 with your local Clash mixed port when needed.
# requests_http = "http://127.0.0.1:7897"
# requests_https = "http://127.0.0.1:7897"
# playwright = "http://127.0.0.1:7897"
# rtunnel = "http://127.0.0.1:7897"

[tunnel]
retries = 3
retry_pause = 2.0

[job]
# shm_size = 32
# auto_fault_tolerance = false
# fault_tolerance_max_retry = 10
# enable_notification = false

[notebook]
# post_start = "bash /workspace/setup.sh"

[remote_env]
# Environment variables exported before notebook commands and jobs run.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


def render_account_config(*, username: str, password: str, base_url: str, proxy: str) -> str:
    """Write a minimal account config.toml using the real schema section names.

    Keys must live under [auth]/[api]/[proxy] — the loader resolves
    ``auth.username`` / ``api.base_url`` etc. via the flattened TOML path,
    and a bare top-level ``username = "..."`` silently fails to bind.
    """
    lines = [
        "[auth]",
        f'username = "{_toml_escape_string(username)}"',
        f'password = "{_toml_escape_string(password)}"',
        "",
        "[api]",
        f'base_url = "{_toml_escape_string(base_url)}"',
    ]
    if proxy:
        escaped = _toml_escape_string(proxy)
        lines.extend(
            [
                "",
                "[proxy]",
                f'requests_http = "{escaped}"',
                f'requests_https = "{escaped}"',
                f'playwright = "{escaped}"',
                f'rtunnel = "{escaped}"',
            ]
        )
    return "\n".join(lines) + "\n"
