"""Global and project TOML layer helpers for config loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_GLOBAL, SOURCE_PROJECT, Config, ConfigError
from inspire.config.toml import (
    _find_project_config,
    _flatten_toml,
    _load_toml,
    _toml_key_to_field,
)

from .load_accounts import _parse_global_accounts
from .load_common import (
    _ProjectLayerState,
    _apply_defaults_overrides,
    _parse_alias_map,
)


def _apply_global_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    global_config_path: Path | None = None
    global_account_catalogs: dict[str, dict[str, Any]] = {}
    resolved_global_path = Config.resolve_global_config_path()
    if not resolved_global_path.exists():
        return global_config_path, global_account_catalogs

    global_config_path = resolved_global_path
    global_raw = _load_toml(resolved_global_path)
    global_compute_groups = global_raw.pop("compute_groups", [])
    global_remote_env = {str(k): str(v) for k, v in global_raw.pop("remote_env", {}).items()}
    global_accounts, global_account_catalogs = _parse_global_accounts(
        global_raw.pop("accounts", {})
    )

    global_defaults: dict[str, Any] = {}
    raw_global_defaults = global_raw.pop("defaults", {})
    if isinstance(raw_global_defaults, dict):
        global_defaults = raw_global_defaults

    global_workspaces: dict[str, str] = {}
    raw_workspaces = global_raw.get("workspaces") or {}
    if isinstance(raw_workspaces, dict):
        global_workspaces = {str(k): str(v) for k, v in raw_workspaces.items()}

    flat_global = _flatten_toml(global_raw)
    for toml_key, value in flat_global.items():
        field_name = _toml_key_to_field(toml_key)
        if field_name and field_name in config_dict:
            config_dict[field_name] = value
            sources[field_name] = SOURCE_GLOBAL

    if global_compute_groups:
        config_dict["compute_groups"] = global_compute_groups
        sources["compute_groups"] = SOURCE_GLOBAL
    if global_remote_env:
        config_dict["remote_env"] = global_remote_env
        sources["remote_env"] = SOURCE_GLOBAL
    if global_workspaces:
        config_dict["workspaces"] = global_workspaces
        sources["workspaces"] = SOURCE_GLOBAL
    if global_accounts:
        config_dict["accounts"] = global_accounts
        sources["accounts"] = SOURCE_GLOBAL

    _apply_defaults_overrides(
        defaults=global_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
    )
    return global_config_path, global_account_catalogs


def _apply_project_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> _ProjectLayerState:
    project_config_path = _find_project_config()
    layer_state = _ProjectLayerState(
        project_config_path=project_config_path,
        project_projects={},
        project_defaults={},
        project_context={},
        project_account_catalogs={},
        project_accounts={},
    )
    if not project_config_path:
        return layer_state

    project_raw = _load_toml(project_config_path)
    cli_section = project_raw.pop("cli", {})
    prefer_source = cli_section.get("prefer_source", "env")
    if prefer_source not in ("env", "toml"):
        raise ConfigError(
            f"Invalid prefer_source value: '{prefer_source}'\n"
            "Must be 'env' or 'toml' in [cli] section of project config."
        )
    layer_state.prefer_source = prefer_source

    project_compute_groups = project_raw.pop("compute_groups", [])
    project_remote_env = {str(k): str(v) for k, v in project_raw.pop("remote_env", {}).items()}
    project_projects = _parse_alias_map(project_raw.pop("projects", {}))
    layer_state.project_projects = project_projects

    raw_defaults = project_raw.pop("defaults", {})
    if isinstance(raw_defaults, dict):
        layer_state.project_defaults = raw_defaults
    raw_context = project_raw.pop("context", {})
    if isinstance(raw_context, dict):
        layer_state.project_context = raw_context

    project_accounts, project_account_catalogs = _parse_global_accounts(
        project_raw.pop("accounts", {})
    )
    layer_state.project_accounts = project_accounts
    layer_state.project_account_catalogs = project_account_catalogs

    project_workspaces: dict[str, str] = {}
    raw_workspaces = project_raw.get("workspaces") or {}
    if isinstance(raw_workspaces, dict):
        project_workspaces = {str(k): str(v) for k, v in raw_workspaces.items()}

    flat_project = _flatten_toml(project_raw)
    for toml_key, value in flat_project.items():
        field_name = _toml_key_to_field(toml_key)
        if field_name and field_name in config_dict:
            config_dict[field_name] = value
            sources[field_name] = SOURCE_PROJECT

    if project_compute_groups:
        config_dict["compute_groups"] = project_compute_groups
        sources["compute_groups"] = SOURCE_PROJECT
    if project_remote_env:
        merged_remote_env = dict(config_dict.get("remote_env", {}))
        merged_remote_env.update(project_remote_env)
        config_dict["remote_env"] = merged_remote_env
        sources["remote_env"] = SOURCE_PROJECT
    if project_workspaces:
        merged_workspaces = dict(config_dict.get("workspaces", {}))
        merged_workspaces.update(project_workspaces)
        config_dict["workspaces"] = merged_workspaces
        sources["workspaces"] = SOURCE_PROJECT
    if project_accounts:
        merged_accounts = dict(config_dict.get("accounts", {}))
        merged_accounts.update(project_accounts)
        config_dict["accounts"] = merged_accounts
        sources["accounts"] = SOURCE_PROJECT

    return layer_state


__all__ = ["_apply_global_layer", "_apply_project_layer"]
