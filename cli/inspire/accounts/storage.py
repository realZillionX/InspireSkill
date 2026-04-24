"""File-based account storage.

One account = one isolated directory under ``~/.inspire/accounts/<name>/``.
The active account is named in a single line at ``~/.inspire/current``. No
layered merge, no ``[accounts."<name>"]`` sections, no env-var precedence
chains — every account's state (config.toml, bridges.json, web_session.json,
rtunnel cache) lives inside its own directory and never leaks into another.

All callers must resolve per-account paths through helpers here rather than
hard-coding ``~/.inspire/accounts/<name>/...`` strings, so there is only one
place to change when the on-disk layout evolves.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

CONFIG_FILENAME = "config.toml"


def _atomic_write_text(target: Path, content: str) -> None:
    """Write *content* to *target* atomically (temp file + ``os.replace``).

    Matches the pattern already used by
    ``inspire.platform.web.session.models.WebSession.save`` — keep partial
    writes out of the target path so concurrent ``account use`` or a crash
    mid-write never leaves a half-written ``current`` / ``config.toml``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AccountError(Exception):
    """Raised for account-related failures (not found, already exists, bad name)."""


def validate_name(name: str) -> str:
    candidate = (name or "").strip()
    if not _NAME_PATTERN.match(candidate):
        raise AccountError(
            f"Invalid account name: {name!r}. Allowed: letters, digits, '.', '_', '-'; "
            "must start with a letter or digit; 1-64 chars."
        )
    return candidate


def inspire_home() -> Path:
    return Path.home() / ".inspire"


def accounts_dir() -> Path:
    return inspire_home() / "accounts"


def current_file() -> Path:
    return inspire_home() / "current"


def account_dir(name: str) -> Path:
    return accounts_dir() / validate_name(name)


def account_config_path(name: str) -> Path:
    return account_dir(name) / CONFIG_FILENAME


def ensure_inspire_home() -> None:
    inspire_home().mkdir(parents=True, exist_ok=True)
    accounts_dir().mkdir(parents=True, exist_ok=True)


def list_accounts() -> list[str]:
    root = accounts_dir()
    if not root.exists():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and (p / CONFIG_FILENAME).exists()
    )


def account_exists(name: str) -> bool:
    try:
        return validate_name(name) in list_accounts()
    except AccountError:
        return False


def current_account() -> str | None:
    try:
        raw = current_file().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return raw or None


def set_current_account(name: str) -> None:
    validated = validate_name(name)
    if not account_exists(validated):
        raise AccountError(f"Account not found: {validated}")
    ensure_inspire_home()
    _atomic_write_text(current_file(), validated + "\n")


def clear_current_account() -> None:
    try:
        current_file().unlink()
    except FileNotFoundError:
        pass


def create_account(name: str, config_content: str, *, overwrite: bool = False) -> Path:
    validated = validate_name(name)
    target = accounts_dir() / validated
    if target.exists() and not overwrite:
        raise AccountError(f"Account already exists: {validated}")
    ensure_inspire_home()
    target.mkdir(parents=True, exist_ok=overwrite)
    _atomic_write_text(target / CONFIG_FILENAME, config_content)
    return target


def remove_account(name: str) -> None:
    validated = validate_name(name)
    target = accounts_dir() / validated
    if not target.exists():
        raise AccountError(f"Account not found: {validated}")
    shutil.rmtree(target)
    if current_account() == validated:
        clear_current_account()
