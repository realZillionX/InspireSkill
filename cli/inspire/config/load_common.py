"""Shared helpers for layered config loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_DEFAULT

_DEFAULTS_FIELD_MAP = {
    "notebook_post_start": "notebook_post_start",
    "shm_size": "shm_size",
    "project_order": "project_order",
}


@dataclass
class _ProjectLayerState:
    project_defaults: dict[str, Any]
    project_context: dict[str, Any]
    prefer_source: str = "env"
    shared_project_config_path: Path | None = None
    account_project_config_path: Path | None = None
    project_config_paths: list[Path] = field(default_factory=list)


def _default_config_values() -> dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "base_url": "https://api.example.com",
        "browser_api_prefix": None,
        "requests_http_proxy": None,
        "requests_https_proxy": None,
        "playwright_proxy": None,
        "rtunnel_proxy": None,
        "job_auto_fault_tolerance": False,
        "job_fault_tolerance_max_retry": 10,
        "job_enable_notification": False,
        "projects": {},
        "project_catalog": {},
        "notebook_post_start": None,
        "tunnel_retries": 3,
        "tunnel_retry_pause": 2.0,
        "shm_size": None,
        "project_order": [],
        "compute_groups": [],
        "remote_env": {},
        "path_aliases": {},
        "profiles": {},
        "context_project": None,
        "context_workspace": None,
    }


def _initialize_sources(config_dict: dict[str, Any]) -> dict[str, str]:
    return {key: SOURCE_DEFAULT for key in config_dict}


def _apply_defaults_overrides(
    *,
    defaults: dict[str, Any],
    config_dict: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
) -> None:
    for key, field_name in _DEFAULTS_FIELD_MAP.items():
        if key not in defaults:
            continue
        raw_value = defaults.get(key)
        if raw_value is None or raw_value == "":
            continue
        try:
            coerced = _coerce_project_default(field_name, raw_value)
        except (ValueError, TypeError):
            continue
        config_dict[field_name] = coerced
        sources[field_name] = source_name


def _parse_alias_map(raw_value: Any) -> dict[str, str]:
    if not isinstance(raw_value, dict):
        return {}

    result: dict[str, str] = {}
    for raw_key, raw_item in raw_value.items():
        key = str(raw_key).strip()
        value = str(raw_item).strip()
        if not key or not value:
            continue
        result[key] = value
    return result


def _normalize_project_catalog(raw_value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for raw_alias, raw_entry in raw_value.items():
        alias = str(raw_alias).strip()
        if not alias or not isinstance(raw_entry, dict):
            continue

        entry: dict[str, Any] = {}
        # ``name``, ``path`` and ``path_user`` are the metadata consumed by
        # name-only commands and remote path helpers.
        for key in ("name", "path", "path_user"):
            value = raw_entry.get(key)
            if isinstance(value, str):
                value = value.strip()
            if not value:
                continue
            entry[key] = value
        normalized[alias] = entry
    return normalized


def _coerce_project_default(field_name: str, raw_value: Any) -> Any:
    if field_name == "shm_size":
        return int(raw_value)
    if field_name == "notebook_post_start":
        return str(raw_value)
    if field_name == "project_order":
        if isinstance(raw_value, list):
            return [str(v) for v in raw_value]
        return raw_value
    return raw_value


__all__ = [
    "_DEFAULTS_FIELD_MAP",
    "_ProjectLayerState",
    "_apply_defaults_overrides",
    "_coerce_project_default",
    "_default_config_values",
    "_initialize_sources",
    "_normalize_project_catalog",
    "_parse_alias_map",
]
