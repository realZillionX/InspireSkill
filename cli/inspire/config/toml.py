"""TOML parsing and config file discovery for Inspire CLI config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

from inspire.config.models import (
    CONFIG_FILENAME,
    PROJECT_ACCOUNT_CONFIG_DIR,
    PROJECT_CONFIG_DIR,
)
from inspire.config.schema import get_option_by_toml


def _active_project_config_account(account: str | None = None) -> str | None:
    """Return the account name that scopes this repo's project config."""
    explicit = str(account or "").strip()
    if explicit:
        return explicit
    try:
        from inspire.accounts import current_account
    except ImportError:  # pragma: no cover - accounts module ships with the CLI
        return None
    return current_account()


def _project_config_path_for_root(root: Path, account: str | None = None) -> Path:
    if account:
        return root / PROJECT_CONFIG_DIR / PROJECT_ACCOUNT_CONFIG_DIR / account / CONFIG_FILENAME
    return root / PROJECT_CONFIG_DIR / CONFIG_FILENAME


def _home_search_boundary() -> Path | None:
    try:
        return Path.home().resolve()
    except OSError:
        return None


def _find_project_config(account: str | None = None) -> Path | None:
    account = _active_project_config_account(account)
    current = Path.cwd()
    home = _home_search_boundary()
    while current != current.parent:
        if home is not None and current.resolve() == home:
            break
        config_path = _project_config_path_for_root(current, account)
        if config_path.exists():
            return config_path
        current = current.parent
    return None


def _project_config_write_path() -> Path:
    """Return the project config path to write for the active account.

    Reads only use the active account's config path when an account is
    selected. For writes, if the repo already has a ``.inspire`` directory in
    an ancestor, place the account-scoped config there instead of creating a
    nested ``.inspire`` directory from a subdirectory command.
    """
    existing = _find_project_config()
    if existing:
        return existing

    account = _active_project_config_account()
    current = Path.cwd()
    home = _home_search_boundary()
    while current != current.parent:
        if home is not None and current.resolve() == home:
            break
        if (current / PROJECT_CONFIG_DIR).exists():
            return _project_config_path_for_root(current, account)
        current = current.parent
    return _project_config_path_for_root(Path.cwd(), account)


def _load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten_toml(value, full_key))
        else:
            result[full_key] = value
    return result


def _toml_key_to_field(toml_key: str) -> str | None:
    option = get_option_by_toml(toml_key)
    return option.field_name if option else None
