"""Top-level orchestrator for layered config loading.

Layer order (later wins):

    defaults → account file → env → fallbacks

All persistent CLI configuration lives in the active account's
``~/.inspire/accounts/<name>/config.toml``. The CLI deliberately has no
repository-local configuration layer. Without an active account, identity
fields stay empty; credential-requiring callers get an ``account add`` hint.
The selected account's login name always comes from its file; environment
overrides apply to runtime settings, not to a configured account's identity.
"""

from __future__ import annotations

from inspire.config.models import Config

from .load_account_layer import _apply_account_layer
from .load_common import _default_config_values, _initialize_sources
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

    account_path = _apply_account_layer(
        config_dict=config_dict,
        sources=sources,
        account=account,
    )
    env_password = _apply_env_layer(
        config_dict=config_dict,
        sources=sources,
        allow_env_identity=account_path is None,
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

    return Config(**config_dict), sources


__all__ = ["config_from_files_and_env"]
