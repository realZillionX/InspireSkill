"""Top-level orchestrator for layered config loading.

Layer order (later wins):

    defaults → account file → shared project file → account project file
    → project context → env → fallbacks

Identity (username / password / base_url / proxy) lives in the active
account's ``~/.inspire/accounts/<name>/config.toml``. Repo-wide project
state lives in ``./.inspire/config.toml`` and account-specific project
overrides live in ``./.inspire/accounts/<name>/config.toml``.
Without an active account, identity fields
stay empty; callers that pass ``require_credentials=True`` will get a
``ConfigError`` pointing at ``inspire account add``.
"""

from __future__ import annotations

from pathlib import Path

from inspire.config.models import Config
from inspire.config.toml import _find_project_config

from .load_account_layer import _apply_account_layer
from .load_context import _apply_project_context_and_defaults
from .load_common import _default_config_values, _initialize_sources
from .load_layers import _apply_project_layer
from .load_runtime import (
    _apply_env_layer,
    _apply_password_fallback,
    _validate_required_config,
)


def config_from_files_and_env(
    *,
    require_credentials: bool = True,
    account: str | None = None,
) -> tuple[Config, dict[str, str]]:
    """Load config from files + env vars with layered precedence."""
    config_dict = _default_config_values()
    sources = _initialize_sources(config_dict)

    _apply_account_layer(
        config_dict=config_dict,
        sources=sources,
        account=account,
    )
    project_layer_state = _apply_project_layer(
        config_dict=config_dict,
        sources=sources,
        account=account,
    )

    _apply_project_context_and_defaults(
        config_dict=config_dict,
        sources=sources,
        project_context=project_layer_state.project_context,
        project_defaults=project_layer_state.project_defaults,
    )

    env_password = _apply_env_layer(
        config_dict=config_dict,
        sources=sources,
        prefer_source=project_layer_state.prefer_source,
    )
    _apply_password_fallback(
        config_dict=config_dict,
        sources=sources,
        env_password=env_password,
    )
    _validate_required_config(
        config_dict=config_dict,
        require_credentials=require_credentials,
    )

    config_dict["prefer_source"] = project_layer_state.prefer_source
    config = Config(**config_dict)
    config._shared_project_config_path = project_layer_state.shared_project_config_path  # type: ignore[attr-defined]
    config._account_project_config_path = project_layer_state.account_project_config_path  # type: ignore[attr-defined]

    return config, sources


def get_config_paths(account: str | None = None) -> tuple[Path | None, Path | None]:
    """Return (account_config_path_if_any, project_config_path_if_any).

    The first slot is the active account's config path and the second is the
    repository project path.
    """
    from .load_account_layer import _resolve_account_config_path

    account_path = _resolve_account_config_path(account)
    project_path = _find_project_config(account)
    return account_path, project_path


__all__ = ["config_from_files_and_env", "get_config_paths"]
