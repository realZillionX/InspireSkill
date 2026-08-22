"""Project TOML layers.

Project configuration is split into two repo-local layers:

* shared project config: ``./.inspire/config.toml``
* account override: ``./.inspire/accounts/<account>/config.toml``

The shared layer holds facts that normally apply to the repository regardless
of which local account is active. The account override is applied on top only
when an account-specific file exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_PROJECT, ConfigError
from inspire.config.toml import (
    _find_project_configs,
    _flatten_toml,
    _load_toml,
    _toml_key_to_field,
)

from .load_common import _ProjectLayerState, _parse_alias_map
from .path_aliases import normalize_path_alias_map
from .workload_profiles import merge_workload_profiles, normalize_workload_profiles


def _apply_project_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    account: str | None = None,
) -> _ProjectLayerState:
    shared_path, account_path = _find_project_configs(account)
    layer_state = _ProjectLayerState(
        project_defaults={},
        project_context={},
        shared_project_config_path=shared_path,
        account_project_config_path=account_path,
        project_config_paths=[path for path in (shared_path, account_path) if path is not None],
    )
    if not layer_state.project_config_paths:
        return layer_state

    for path in layer_state.project_config_paths:
        _apply_one_project_file(
            config_dict=config_dict,
            sources=sources,
            layer_state=layer_state,
            project_config_path=path,
        )

    return layer_state


def _apply_one_project_file(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    layer_state: _ProjectLayerState,
    project_config_path: Path,
) -> None:
    project_raw = _load_toml(project_config_path)
    cli_section = project_raw.pop("cli", {})
    prefer_source = cli_section.get("prefer_source")
    if prefer_source is not None:
        if prefer_source not in ("env", "toml"):
            raise ConfigError(
                f"Invalid prefer_source value: '{prefer_source}'\n"
                "Must be 'env' or 'toml' in [cli] section of project config."
            )
        layer_state.prefer_source = prefer_source

    project_compute_groups = project_raw.pop("compute_groups", [])
    project_remote_env = {str(k): str(v) for k, v in project_raw.pop("remote_env", {}).items()}
    project_path_aliases = normalize_path_alias_map(project_raw.pop("path_aliases", {}))
    project_profiles = normalize_workload_profiles(project_raw.pop("profiles", {}))
    project_aliases = _parse_alias_map(project_raw.pop("projects", {}))

    raw_defaults = project_raw.pop("defaults", {})
    if isinstance(raw_defaults, dict):
        layer_state.project_defaults.update(raw_defaults)
    raw_context = project_raw.pop("context", {})
    if isinstance(raw_context, dict):
        layer_state.project_context.update(raw_context)

    flat_project = _flatten_toml(project_raw)

    # Enforce ConfigOption.scope at the loader: a per-repo account-scoped
    # project config may only carry project-scope keys. Account-scope identity
    # / API / proxy keys must live in the active account's
    # `~/.inspire/accounts/<n>/config.toml`, because one account is shared
    # across many repos and silently overriding auth from a repo file would let
    # one repo poison another.
    from inspire.config.schema import get_option_by_toml

    misplaced: list[str] = []
    for toml_key in flat_project:
        opt = get_option_by_toml(toml_key)
        if opt is not None and opt.scope == "global":
            misplaced.append(toml_key)
    if misplaced:
        raise ConfigError(
            "Project config carries account-scope keys or unsupported keys: "
            f"{', '.join(misplaced)}. Refresh the file with `inspire init` "
            "or remove those keys. The project file should only contain "
            "[paths] / [context] / [defaults] / [projects] / "
            "[compute_groups] / [remote_env] / [path_aliases] / [profiles] / [cli]."
        )

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
    if project_path_aliases:
        merged_path_aliases = dict(config_dict.get("path_aliases", {}))
        merged_path_aliases.update(project_path_aliases)
        config_dict["path_aliases"] = merged_path_aliases
        sources["path_aliases"] = SOURCE_PROJECT
    if project_profiles:
        config_dict["profiles"] = merge_workload_profiles(
            config_dict.get("profiles", {}),
            project_profiles,
        )
        sources["profiles"] = SOURCE_PROJECT

    # Merge aliases from the repo's shared and account-specific project files.
    if project_aliases:
        merged_projects = dict(config_dict.get("projects", {}))
        merged_projects.update(project_aliases)
        config_dict["projects"] = merged_projects
        sources["projects"] = SOURCE_PROJECT


__all__ = ["_apply_project_layer"]
