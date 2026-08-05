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


def _find_project_config_by_account(account: str | None) -> Path | None:
    """Return ``./.inspire/accounts/<account>/config.toml`` if it exists."""
    if not account:
        return None
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


def _find_shared_project_config() -> Path | None:
    """Return ``./.inspire/config.toml`` if it exists."""
    current = Path.cwd()
    home = _home_search_boundary()
    while current != current.parent:
        if home is not None and current.resolve() == home:
            break
        config_path = _project_config_path_for_root(current, None)
        if config_path.exists():
            return config_path
        current = current.parent
    return None


def _find_project_configs(account: str | None = None) -> tuple[Path | None, Path | None]:
    """Return ``(shared_project_config, account_project_config)`` for this repo."""
    account_name = _active_project_config_account(account)
    return _find_shared_project_config(), _find_project_config_by_account(account_name)


def _find_project_config(account: str | None = None) -> Path | None:
    """Return the most specific project config path, falling back to shared.

    The loader reads ``./.inspire/config.toml`` first and then applies
    ``./.inspire/accounts/<account>/config.toml`` as an override.
    """
    shared_path, account_path = _find_project_configs(account)
    return account_path or shared_path


def _project_root_for_existing_inspire_dir() -> Path | None:
    current = Path.cwd()
    home = _home_search_boundary()
    while current != current.parent:
        if home is not None and current.resolve() == home:
            break
        if (current / PROJECT_CONFIG_DIR).exists():
            return current
        current = current.parent
    return None


def _project_config_write_path(
    account: str | None = None,
    *,
    shared: bool = False,
) -> Path:
    """Return the project config path to write.

    By default this returns the account override path for the active or
    explicit account. With ``shared=True`` it returns the repo-wide
    ``./.inspire/config.toml`` path. If the command is run from a subdirectory
    under an existing ``.inspire`` root, writes land at that root instead of
    creating a nested config.
    """
    if shared:
        existing_shared = _find_shared_project_config()
        if existing_shared:
            return existing_shared
        root = _project_root_for_existing_inspire_dir() or Path.cwd()
        return _project_config_path_for_root(root, None)

    account_name = _active_project_config_account(account)
    existing_account = _find_project_config_by_account(account_name)
    if existing_account:
        return existing_account
    if account_name is None:
        return _project_config_write_path(shared=True)

    root = _project_root_for_existing_inspire_dir() or Path.cwd()
    return _project_config_path_for_root(root, account_name)


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
