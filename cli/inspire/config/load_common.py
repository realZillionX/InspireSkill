"""Shared helpers for layered config loading."""

from __future__ import annotations

from typing import Any

from inspire.config.models import DEFAULT_BASE_URL, SOURCE_DEFAULT

_ACCOUNT_DEFAULTS_FIELD_MAP = {
    "notebook_post_start": "notebook_post_start",
    "shm_size": "shm_size",
    "project_order": "project_order",
}


def _default_config_values() -> dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "base_url": DEFAULT_BASE_URL,
        "requests_http_proxy": None,
        "requests_https_proxy": None,
        "playwright_proxy": None,
        "rtunnel_proxy": None,
        "job_auto_fault_tolerance": False,
        "job_fault_tolerance_max_retry": 10,
        "job_enable_notification": False,
        "notebook_post_start": None,
        "tunnel_retries": 3,
        "tunnel_retry_pause": 2.0,
        "shm_size": None,
        "project_order": [],
        "remote_env": {},
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
    for key, field_name in _ACCOUNT_DEFAULTS_FIELD_MAP.items():
        if key not in defaults:
            continue
        raw_value = defaults.get(key)
        if raw_value is None or raw_value == "":
            continue
        try:
            coerced = _coerce_account_default(field_name, raw_value)
        except (ValueError, TypeError):
            continue
        config_dict[field_name] = coerced
        sources[field_name] = source_name


def _coerce_account_default(field_name: str, raw_value: Any) -> Any:
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
    "_ACCOUNT_DEFAULTS_FIELD_MAP",
    "_apply_defaults_overrides",
    "_coerce_account_default",
    "_default_config_values",
    "_initialize_sources",
]
