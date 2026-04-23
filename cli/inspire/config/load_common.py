"""Shared helpers for layered config loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_DEFAULT
from inspire.config.rtunnel_defaults import default_rtunnel_download_url

_DEFAULTS_FIELD_MAP = {
    "image": "job_image",
    "notebook_image": "notebook_image",
    "notebook_resource": "notebook_resource",
    "notebook_post_start": "notebook_post_start",
    "priority": "job_priority",
    "shm_size": "shm_size",
    "target_dir": "target_dir",
    "log_pattern": "log_pattern",
    "project_order": "project_order",
}

_CONTEXT_WORKSPACE_FIELD_MAP = {
    "workspace": "job_workspace_id",
    "workspace_cpu": "workspace_cpu_id",
    "workspace_gpu": "workspace_gpu_id",
}


@dataclass
class _ProjectLayerState:
    project_config_path: Path | None
    project_projects: dict[str, str]
    project_defaults: dict[str, Any]
    project_context: dict[str, Any]
    prefer_source: str = "env"


def _default_config_values() -> dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "base_url": "https://api.example.com",
        "target_dir": None,
        "log_pattern": "training_master_*.log",
        "job_cache_path": "~/.inspire/jobs.json",
        "timeout": 30,
        "max_retries": 3,
        "retry_delay": 1.0,
        "github_repo": None,
        "github_token": None,
        "github_server": "https://github.com",
        "github_log_workflow": "retrieve_job_log.yml",
        "github_sync_workflow": "sync_code.yml",
        "github_bridge_workflow": "run_bridge_action.yml",
        "log_cache_dir": "~/.inspire/logs",
        "remote_timeout": 90,
        "default_remote": "origin",
        "bridge_action_timeout": 600,
        "bridge_action_denylist": [],
        "skip_ssl_verify": False,
        "force_proxy": False,
        "openapi_prefix": None,
        "browser_api_prefix": None,
        "auth_endpoint": None,
        "docker_registry": None,
        "requests_http_proxy": None,
        "requests_https_proxy": None,
        "playwright_proxy": None,
        "rtunnel_proxy": None,
        "job_priority": 10,
        "job_image": None,
        "job_project_id": None,
        "job_workspace_id": None,
        "workspace_cpu_id": None,
        "workspace_gpu_id": None,
        "workspaces": {},
        "projects": {},
        "project_catalog": {},
        "project_shared_path_groups": {},
        "project_workdirs": {},
        "account_shared_path_group": None,
        "account_train_job_workdir": None,
        "notebook_resource": "1xH200",
        "notebook_image": None,
        "notebook_post_start": None,
        "sshd_deb_dir": None,
        "dropbear_deb_dir": None,
        "setup_script": None,
        "rtunnel_download_url": default_rtunnel_download_url(),
        "apt_mirror_url": None,
        "tunnel_retries": 3,
        "tunnel_retry_pause": 2.0,
        "shm_size": None,
        "project_order": [],
        "compute_groups": [],
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


def _normalize_compute_groups(raw_value: Any) -> list[dict]:
    if not isinstance(raw_value, list):
        return []

    normalized: list[dict] = []
    for raw_item in raw_value:
        if not isinstance(raw_item, dict):
            continue

        raw_ws = raw_item.get("workspace_ids", [])
        if isinstance(raw_ws, str):
            workspace_ids = [raw_ws] if raw_ws else []
        elif isinstance(raw_ws, list):
            workspace_ids = [str(w) for w in raw_ws if isinstance(w, str) and w]
        else:
            workspace_ids = []

        normalized.append(
            {
                "id": str(raw_item.get("id", "")).strip(),
                "name": str(raw_item.get("name", "")).strip(),
                "gpu_type": str(raw_item.get("gpu_type", "")).strip(),
                "location": str(raw_item.get("location", "")).strip(),
                "workspace_ids": workspace_ids,
            }
        )
    return [item for item in normalized if item["id"] or item["name"]]


def _normalize_project_catalog(raw_value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for raw_project_id, raw_entry in raw_value.items():
        project_id = str(raw_project_id).strip()
        if not project_id or not isinstance(raw_entry, dict):
            continue

        entry: dict[str, Any] = {}
        for key in ("shared_path_group", "workdir"):
            value = raw_entry.get(key)
            if isinstance(value, str):
                value = value.strip()
            if not value:
                continue
            entry[key] = value
        normalized[project_id] = entry
    return normalized


def _resolve_alias(value: Any, mapping: dict[str, str], *, id_prefix: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in mapping:
        return mapping[text]
    for key, mapped in mapping.items():
        if key.lower() == text.lower():
            return mapped
    if text.startswith(id_prefix):
        return text
    return text


def _coerce_project_default(field_name: str, raw_value: Any) -> Any:
    if field_name in {"job_priority", "shm_size"}:
        return int(raw_value)
    if field_name in {
        "target_dir",
        "job_image",
        "notebook_image",
        "notebook_resource",
        "notebook_post_start",
        "log_pattern",
    }:
        return str(raw_value)
    if field_name == "project_order":
        if isinstance(raw_value, list):
            return [str(v) for v in raw_value]
        return raw_value
    return raw_value


__all__ = [
    "_CONTEXT_WORKSPACE_FIELD_MAP",
    "_DEFAULTS_FIELD_MAP",
    "_ProjectLayerState",
    "_apply_defaults_overrides",
    "_coerce_project_default",
    "_default_config_values",
    "_initialize_sources",
    "_normalize_compute_groups",
    "_normalize_project_catalog",
    "_parse_alias_map",
    "_resolve_alias",
]
