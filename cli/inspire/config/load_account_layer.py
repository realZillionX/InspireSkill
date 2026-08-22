"""Per-account config layer — the sole source of identity for the CLI.

Identity and account-wide settings live at::

    ~/.inspire/accounts/<current>/config.toml

Sections: ``[auth]``, ``[api]``, ``[proxy]``, ``[tunnel]``, ``[defaults]``
and ``[remote_env]``. Project names, compute groups and paths are live or
repository-scoped state and are deliberately ignored here.

One account = one file. Without an active account
(``~/.inspire/current`` absent or pointing at a missing directory),
this layer is a no-op and the caller is free to continue — callers that
require credentials will get a clear "run 'inspire account add'" error
from :func:`inspire.config.load_runtime._validate_required_config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_ACCOUNT, ConfigError
from inspire.config.toml import _flatten_toml, _load_toml, _toml_key_to_field

from .load_common import _apply_defaults_overrides

# Keys whose *value* must differ per repository — a single account is used
# across many repos, each with its own Inspire project and workload profiles.
# Putting these at account level silently shadows the correct project-level
# value, so we reject them outright.
ACCOUNT_LAYER_DISALLOWED_KEYS = frozenset(
    {
        "profiles",
        "notebook.quota",
        "notebook.post_start",
    }
)


def _resolve_account_config_path(account: str | None = None) -> Path | None:
    """Return an account's ``config.toml`` path, or ``None``.

    Without an explicit account, this uses the active account. ``None`` means
    either no active account (``~/.inspire/current`` missing) or the selected
    account has no config file yet (fresh ``account add``
    without running ``init``).
    """
    try:
        from inspire.accounts import account_config_path, current_account
    except ImportError:  # pragma: no cover - accounts module ships with the CLI
        return None
    name = str(account or "").strip() or current_account()
    if not name:
        return None
    path = account_config_path(name)
    return path if path.exists() else None


def _apply_account_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    account: str | None = None,
) -> Path | None:
    """Apply the selected account's ``config.toml``.

    Returns the path that was read, or ``None`` if no account config applies.
    The source label is ``SOURCE_ACCOUNT`` because this is the account-wide
    configuration layer.
    """
    account_path = _resolve_account_config_path(account)
    if account_path is None:
        return None

    raw = _load_toml(account_path)
    # Reject per-repository keys anywhere in the file. A single account
    # spans many repos; these values must live in the repo's account-scoped
    # project config or come from env vars.
    _reject_per_repo_keys(raw, account_path)

    # Older discovery releases wrote live/project-derived catalogs into every
    # account config. Ignore them immediately, even before the next ``init``
    # rewrite physically removes them from disk.
    for legacy_key in (
        "compute_groups",
        "path_aliases",
        "project_catalog",
        "projects",
        "paths",
    ):
        raw.pop(legacy_key, None)

    remote_env = {str(k): str(v) for k, v in raw.pop("remote_env", {}).items()}

    defaults: dict[str, Any] = {}
    raw_defaults = raw.pop("defaults", {})
    if isinstance(raw_defaults, dict):
        defaults = raw_defaults

    flat = _flatten_toml(raw)
    for toml_key, value in flat.items():
        field_name = _toml_key_to_field(toml_key)
        if field_name and field_name in config_dict:
            config_dict[field_name] = value
            sources[field_name] = SOURCE_ACCOUNT

    if remote_env:
        config_dict["remote_env"] = remote_env
        sources["remote_env"] = SOURCE_ACCOUNT

    _apply_defaults_overrides(
        defaults=defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_ACCOUNT,
    )
    return account_path


def _reject_per_repo_keys(raw: dict[str, Any], account_path: Path) -> None:
    flat = _flatten_toml(raw)
    offending = sorted(
        k
        for k in flat
        if k in ACCOUNT_LAYER_DISALLOWED_KEYS or k.startswith("profiles.")
    )
    if not offending:
        return
    raise ConfigError(
        f"Account config at {account_path} contains per-repository keys: "
        f"{', '.join(offending)}. These must live in this repo's "
        "account-scoped project config (run 'inspire init --scope project' "
        "from inside the repo), not at the account level — a single account usually has "
        "many repos with different Inspire projects / workload profiles, "
        "and placing per-repo values here silently shadows "
        "the correct ones. Remote path aliases belong in the repo config."
    )


__all__ = [
    "ACCOUNT_LAYER_DISALLOWED_KEYS",
    "_apply_account_layer",
    "_resolve_account_config_path",
]
