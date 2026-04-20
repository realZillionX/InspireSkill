"""Minimal TOML serializer for writing config files without third-party deps."""

from __future__ import annotations

import re
from typing import Any

_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_escape_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
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
    if isinstance(value, list):
        formatted = ", ".join(_toml_format_value(item) for item in value)
        return f"[{formatted}]"
    return '"' + _toml_escape_string(str(value)) + '"'


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


def _toml_dump_table(path: list[str], table: dict[str, Any], lines: list[str]) -> None:
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
        lines.append(f"[{header}]")

    for key, value in sorted(scalar_items, key=lambda item: item[0]):
        lines.append(f"{_toml_format_key(key)} = {_toml_format_value(value)}")

    if scalar_items and (nested_tables or array_tables):
        lines.append("")

    for key, subtable in sorted(nested_tables, key=lambda item: item[0]):
        _toml_dump_table([*path, key], subtable, lines)
        lines.append("")

    for key, items in sorted(array_tables, key=lambda item: item[0]):
        table_path = ".".join(_toml_format_key(part) for part in [*path, key])
        for item in items:
            lines.append(f"[[{table_path}]]")
            for item_key, item_value in sorted(item.items(), key=lambda kv: kv[0]):
                if item_value is None:
                    continue
                if isinstance(item_value, dict) or _is_array_of_tables(item_value):
                    continue
                lines.append(f"{_toml_format_key(item_key)} = {_toml_format_value(item_value)}")
            lines.append("")


def _toml_dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _toml_dump_table([], data, lines)

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines) + "\n"
