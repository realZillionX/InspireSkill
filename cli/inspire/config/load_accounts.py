"""Account catalog parsing and merge helpers for config loading."""

from __future__ import annotations

import os
from typing import Any

from inspire.config.models import SOURCE_GLOBAL, SOURCE_PROJECT

from .load_common import (
    _ACCOUNT_OVERRIDE_FIELDS,
    _ACCOUNT_SECTION_KEY_MAP,
    _CONTEXT_WORKSPACE_FIELD_MAP,
    _apply_defaults_overrides,
    _normalize_compute_groups,
    _normalize_project_catalog,
    _parse_alias_map,
    _resolve_alias,
)


def _parse_global_accounts(raw_accounts: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse [accounts."<username>"] entries from a TOML layer."""
    if not isinstance(raw_accounts, dict):
        return {}, {}

    passwords: dict[str, str] = {}
    catalogs: dict[str, dict[str, Any]] = {}
    for raw_username, raw_value in raw_accounts.items():
        username = str(raw_username).strip()
        if not username or not isinstance(raw_value, dict):
            continue

        account_data: dict[str, Any] = {
            "projects": _parse_alias_map(raw_value.get("projects", {})),
            "workspaces": _parse_alias_map(raw_value.get("workspaces", {})),
            "compute_groups": _normalize_compute_groups(raw_value.get("compute_groups", [])),
            "project_catalog": _normalize_project_catalog(raw_value.get("project_catalog", {})),
            "shared_path_group": str(raw_value.get("shared_path_group") or "").strip() or None,
            "train_job_workdir": str(raw_value.get("train_job_workdir") or "").strip() or None,
            "overrides": {},
        }

        password = raw_value.get("password")
        if password is not None:
            password_str = str(password)
            if password_str:
                passwords[username] = password_str

        for field_name in _ACCOUNT_OVERRIDE_FIELDS:
            value = raw_value.get(field_name)
            if value is None or value == "":
                continue
            account_data["overrides"][field_name] = value

        for section_name, key_map in _ACCOUNT_SECTION_KEY_MAP.items():
            section = raw_value.get(section_name)
            if not isinstance(section, dict):
                continue
            for key, field_name in key_map.items():
                value = section.get(key)
                if value is None or value == "":
                    continue
                account_data["overrides"][field_name] = value

        catalogs[username] = account_data

    return passwords, catalogs


def _merge_account_catalog(
    global_catalog: dict[str, Any],
    project_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Merge one account catalog entry with project values overriding global values."""
    merged_projects = dict(global_catalog.get("projects", {}))
    merged_projects.update(project_catalog.get("projects", {}))

    merged_workspaces = dict(global_catalog.get("workspaces", {}))
    merged_workspaces.update(project_catalog.get("workspaces", {}))

    merged_project_catalog = dict(global_catalog.get("project_catalog", {}))
    merged_project_catalog.update(project_catalog.get("project_catalog", {}))

    global_overrides = global_catalog.get("overrides", {})
    project_overrides = project_catalog.get("overrides", {})
    merged_overrides = dict(global_overrides)
    merged_overrides.update(project_overrides)

    global_compute_groups = global_catalog.get("compute_groups", [])
    project_compute_groups = project_catalog.get("compute_groups", [])

    shared_path_group = project_catalog.get("shared_path_group")
    if not shared_path_group:
        shared_path_group = global_catalog.get("shared_path_group")

    train_job_workdir = project_catalog.get("train_job_workdir")
    if not train_job_workdir:
        train_job_workdir = global_catalog.get("train_job_workdir")

    return {
        "projects": merged_projects,
        "workspaces": merged_workspaces,
        "compute_groups": (
            project_compute_groups if project_compute_groups else list(global_compute_groups)
        ),
        "project_catalog": merged_project_catalog,
        "shared_path_group": shared_path_group,
        "train_job_workdir": train_job_workdir,
        "overrides": merged_overrides,
    }


def _merge_account_catalogs(
    global_catalogs: dict[str, dict[str, Any]],
    project_catalogs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge account catalogs keyed by username with project values overriding global values."""
    merged_catalogs: dict[str, dict[str, Any]] = {}
    usernames = set(global_catalogs.keys()) | set(project_catalogs.keys())

    for username in usernames:
        global_catalog = global_catalogs.get(username, {})
        project_catalog = project_catalogs.get(username, {})

        if global_catalog and project_catalog:
            merged_catalogs[username] = _merge_account_catalog(global_catalog, project_catalog)
        elif project_catalog:
            merged_catalogs[username] = project_catalog
        elif global_catalog:
            merged_catalogs[username] = global_catalog

    return merged_catalogs


def _apply_account_catalog_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    context_account: str,
    project_projects: dict[str, str],
    global_account_catalogs: dict[str, dict[str, Any]],
    project_account_catalogs: dict[str, dict[str, Any]],
) -> None:
    selected_account = (
        context_account
        or str(config_dict.get("username") or "").strip()
        or str(os.getenv("INSPIRE_USERNAME") or "").strip()
    )
    merged_account_catalogs = _merge_account_catalogs(
        global_account_catalogs, project_account_catalogs
    )
    account_catalog = merged_account_catalogs.get(selected_account, {})
    account_catalog_source = (
        SOURCE_PROJECT
        if selected_account and selected_account in project_account_catalogs
        else SOURCE_GLOBAL
    )

    account_projects = account_catalog.get("projects", {}) if account_catalog else {}
    account_workspaces = account_catalog.get("workspaces", {}) if account_catalog else {}
    account_compute_groups = account_catalog.get("compute_groups", []) if account_catalog else []
    account_project_catalog = account_catalog.get("project_catalog", {}) if account_catalog else {}
    account_shared_path_group = (
        account_catalog.get("shared_path_group") if account_catalog else None
    )
    account_train_job_workdir = (
        account_catalog.get("train_job_workdir") if account_catalog else None
    )
    account_overrides = account_catalog.get("overrides", {}) if account_catalog else {}

    if account_overrides:
        for field_name, value in account_overrides.items():
            if field_name not in config_dict:
                continue
            if sources.get(field_name) == SOURCE_PROJECT:
                continue
            config_dict[field_name] = value
            sources[field_name] = account_catalog_source

    if account_workspaces:
        merged_workspaces = dict(account_workspaces)
        merged_workspaces.update(config_dict.get("workspaces", {}))
        config_dict["workspaces"] = merged_workspaces
        if sources.get("workspaces") not in {SOURCE_PROJECT, SOURCE_GLOBAL}:
            sources["workspaces"] = account_catalog_source

        if not config_dict.get("workspace_cpu_id") and merged_workspaces.get("cpu"):
            config_dict["workspace_cpu_id"] = merged_workspaces["cpu"]
            sources["workspace_cpu_id"] = account_catalog_source
        if not config_dict.get("workspace_gpu_id") and merged_workspaces.get("gpu"):
            config_dict["workspace_gpu_id"] = merged_workspaces["gpu"]
            sources["workspace_gpu_id"] = account_catalog_source
        if not config_dict.get("workspace_internet_id") and merged_workspaces.get("internet"):
            config_dict["workspace_internet_id"] = merged_workspaces["internet"]
            sources["workspace_internet_id"] = account_catalog_source

    merged_projects = dict(account_projects)
    merged_projects.update(project_projects)
    if merged_projects:
        config_dict["projects"] = merged_projects
        sources["projects"] = SOURCE_PROJECT if project_projects else account_catalog_source

    if isinstance(account_project_catalog, dict) and account_project_catalog:
        config_dict["project_catalog"] = account_project_catalog
        sources["project_catalog"] = account_catalog_source

        shared_groups: dict[str, str] = {}
        workdirs: dict[str, str] = {}
        for project_id, entry in account_project_catalog.items():
            if not isinstance(entry, dict):
                continue

            shared = str(entry.get("shared_path_group") or "").strip()
            if shared:
                shared_groups[str(project_id)] = shared

            workdir = str(entry.get("workdir") or "").strip()
            if workdir:
                workdirs[str(project_id)] = workdir

        if shared_groups:
            config_dict["project_shared_path_groups"] = shared_groups
            sources["project_shared_path_groups"] = account_catalog_source
        if workdirs:
            config_dict["project_workdirs"] = workdirs
            sources["project_workdirs"] = account_catalog_source

    if account_shared_path_group:
        config_dict["account_shared_path_group"] = str(account_shared_path_group)
        sources["account_shared_path_group"] = account_catalog_source
    if account_train_job_workdir:
        config_dict["account_train_job_workdir"] = str(account_train_job_workdir)
        sources["account_train_job_workdir"] = account_catalog_source

    if account_compute_groups and not config_dict.get("compute_groups"):
        config_dict["compute_groups"] = account_compute_groups
        sources["compute_groups"] = account_catalog_source


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    context_account: str,
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    if context_account and not config_dict.get("username"):
        config_dict["username"] = context_account
        sources["username"] = SOURCE_PROJECT

    project_ref = _resolve_alias(
        project_context.get("project"),
        config_dict.get("projects", {}),
        id_prefix="project-",
    )
    if project_ref:
        config_dict["job_project_id"] = project_ref
        sources["job_project_id"] = SOURCE_PROJECT

    for context_key, field_name in _CONTEXT_WORKSPACE_FIELD_MAP.items():
        workspace_ref = _resolve_alias(
            project_context.get(context_key),
            config_dict.get("workspaces", {}),
            id_prefix="ws-",
        )
        if not workspace_ref:
            continue
        config_dict[field_name] = workspace_ref
        sources[field_name] = SOURCE_PROJECT

    _apply_defaults_overrides(
        defaults=project_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )


__all__ = [
    "_apply_account_catalog_layer",
    "_apply_project_context_and_defaults",
    "_merge_account_catalog",
    "_merge_account_catalogs",
    "_parse_global_accounts",
]
