"""Top-level orchestrator for layered config loading.

Layer order (later wins):

    defaults → account file → env → fallbacks

All persistent CLI configuration lives in the active account's
``~/.inspire/accounts/<name>/config.toml``. The CLI deliberately has no
repository-local configuration layer. Identity and OAuth tokens live only in
web_session.json; legacy auth fields and credential environment variables
are ignored. require_credentials remains an accepted compatibility argument.
"""

from __future__ import annotations

from inspire.config.models import Config

from .load_account_layer import _apply_account_layer
from .load_common import _default_config_values, _initialize_sources
from .load_runtime import _apply_env_layer


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
    _apply_env_layer(config_dict=config_dict, sources=sources)

    return Config(**config_dict), sources


__all__ = ["config_from_files_and_env"]
