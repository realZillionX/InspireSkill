"""Top-level orchestrator for layered config loading.

All the pieces live in sibling modules:
    - load_common         — shared helpers (defaults dict, alias maps, dataclass)
    - load_account_layer  — per-account ``~/.inspire/accounts/<current>/config.toml``
    - load_layers         — legacy global + project TOML layers
    - load_accounts       — legacy ``[accounts."<user>"]`` catalog (inactive for new users)
    - load_runtime        — env layer + password/token fallbacks + required-field validation

When an InspireSkill account is active (``inspire.accounts.current_account()``),
the per-account layer is used and the legacy global layer is skipped entirely.
Both paths share the same merge contract, so the later layers (project, env,
fallbacks) do not need to branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_PROJECT, Config
from inspire.config.toml import _find_project_config

from .load_account_layer import _apply_account_layer
from .load_accounts import _apply_account_catalog_layer, _apply_project_context_and_defaults
from .load_common import _default_config_values, _initialize_sources
from .load_layers import _apply_global_layer, _apply_project_layer
from .load_runtime import (
    _apply_env_layer,
    _apply_password_and_token_fallbacks,
    _validate_required_config,
)


def config_from_files_and_env(
    *,
    require_target_dir: bool = False,
    require_credentials: bool = True,
) -> tuple[Config, dict[str, str]]:
    """Load config from files + env vars with layered precedence."""
    config_dict = _default_config_values()
    sources = _initialize_sources(config_dict)

    # Primary identity layer: the active account's own config.toml, when set.
    # Falls back to the legacy global path only when no account is active —
    # the two are mutually exclusive, never merged.
    account_config_path = _apply_account_layer(
        config_dict=config_dict,
        sources=sources,
    )
    if account_config_path is not None:
        global_config_path: Path | None = account_config_path
        global_account_catalogs: dict[str, dict[str, Any]] = {}
    else:
        global_config_path, global_account_catalogs = _apply_global_layer(
            config_dict=config_dict,
            sources=sources,
        )

    project_layer_state = _apply_project_layer(config_dict=config_dict, sources=sources)

    context_account = str(project_layer_state.project_context.get("account") or "").strip()
    if context_account:
        config_dict["context_account"] = context_account
        sources["context_account"] = SOURCE_PROJECT

    _apply_account_catalog_layer(
        config_dict=config_dict,
        sources=sources,
        context_account=context_account,
        project_projects=project_layer_state.project_projects,
        global_account_catalogs=global_account_catalogs,
        project_account_catalogs=project_layer_state.project_account_catalogs,
    )
    _apply_project_context_and_defaults(
        config_dict=config_dict,
        sources=sources,
        context_account=context_account,
        project_context=project_layer_state.project_context,
        project_defaults=project_layer_state.project_defaults,
    )

    env_password = _apply_env_layer(
        config_dict=config_dict,
        sources=sources,
        prefer_source=project_layer_state.prefer_source,
    )
    _apply_password_and_token_fallbacks(
        config_dict=config_dict,
        sources=sources,
        project_accounts=project_layer_state.project_accounts,
        env_password=env_password,
    )
    _validate_required_config(
        config_dict=config_dict,
        require_credentials=require_credentials,
        require_target_dir=require_target_dir,
    )

    config_dict["prefer_source"] = project_layer_state.prefer_source
    config = Config(**config_dict)
    config._global_config_path = global_config_path  # type: ignore[attr-defined]
    config._project_config_path = project_layer_state.project_config_path  # type: ignore[attr-defined]
    config._sources = sources  # type: ignore[attr-defined]

    return config, sources


def get_config_paths() -> tuple[Path | None, Path | None]:
    resolved_global_path = Config.resolve_global_config_path()
    global_path = resolved_global_path if resolved_global_path.exists() else None
    project_path = _find_project_config()
    return global_path, project_path


__all__ = ["config_from_files_and_env", "get_config_paths"]
