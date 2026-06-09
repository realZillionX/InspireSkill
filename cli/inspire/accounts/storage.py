"""File-based account storage.

One account = one isolated directory under ``~/.inspire/accounts/<name>/``.
The active account is named in a single line at ``~/.inspire/current``. No
layered merge, no ``[accounts."<name>"]`` sections, no env-var precedence
chains — every account's state (config.toml, bridges.json, web_session.json,
rtunnel-proxy-state.json) lives inside its own directory and never leaks
into another.

All callers must resolve per-account paths through helpers here rather than
hard-coding ``~/.inspire/accounts/<name>/...`` strings, so there is only one
place to change when the on-disk layout evolves.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

CONFIG_FILENAME = "config.toml"
NOTEBOOK_TARGET_CACHE_FILENAME = "notebook-targets.json"


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


def _clear_process_account_caches() -> None:
    """Best-effort reset for account-sensitive in-process caches.

    Normal CLI invocations are one process per command, but tests, embedded
    callers, and Click runners can switch accounts several times in one
    process. Keep low-level account storage responsible for invalidating any
    process-local state that would otherwise keep using the previous account.
    """
    try:
        from inspire.cli.utils.auth import AuthManager

        AuthManager.clear_cache()
    except Exception:
        pass

    try:
        from inspire.platform.web.browser_api.core import clear_browser_api_runtime_cache

        clear_browser_api_runtime_cache()
    except Exception:
        pass

    try:
        from inspire.platform.web.resources import clear_availability_cache

        clear_availability_cache()
    except Exception:
        pass


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
    """Return the active account, or ``None`` if there is no usable one.

    Sanitizes ``~/.inspire/current`` against three failure modes:
      * file missing or empty → no active account (return ``None``)
      * file contains an illegal name (e.g. half-truncated, garbage)
        → treat as no active account (return ``None``)
      * file points at an account directory that no longer exists
        (`account remove` ran but didn't clean ``current``, or the user
        deleted the directory by hand) → also return ``None``

    Centralizing this read makes downstream code (``writable_config_path``,
    ``inspire account current``, every ``Config.from_files_and_env`` caller)
    fail on the same boundary, instead of one path treating the pointer as
    truth while another silently fixes it up.
    """
    try:
        raw = current_file().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        validated = validate_name(raw)
    except AccountError:
        return None
    if not account_dir(validated).is_dir():
        return None
    return validated


def set_current_account(name: str) -> None:
    validated = validate_name(name)
    if not account_exists(validated):
        raise AccountError(f"Account not found: {validated}")
    ensure_inspire_home()
    _atomic_write_text(current_file(), validated + "\n")
    _clear_process_account_caches()


def clear_current_account() -> None:
    try:
        current_file().unlink()
    except FileNotFoundError:
        pass
    _clear_process_account_caches()


def create_account(name: str, config_content: str, *, overwrite: bool = False) -> Path:
    validated = validate_name(name)
    target = accounts_dir() / validated
    if target.exists() and not overwrite:
        raise AccountError(f"Account already exists: {validated}")
    ensure_inspire_home()
    if target.exists() and overwrite:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target / CONFIG_FILENAME, config_content)
    if current_account() == validated:
        _clear_process_account_caches()
    return target


def remove_account(name: str) -> None:
    validated = validate_name(name)
    target = accounts_dir() / validated
    if not target.exists():
        raise AccountError(f"Account not found: {validated}")
    was_active = current_account() == validated
    shutil.rmtree(target)
    if was_active:
        clear_current_account()


def rename_account(old_name: str, new_name: str) -> None:
    """Rename a local account alias without changing platform credentials."""
    old = validate_name(old_name)
    new = validate_name(new_name)
    if old == new:
        raise AccountError("Source and target account names are the same.")
    if not account_exists(old):
        raise AccountError(f"Account not found: {old}")

    ensure_inspire_home()
    source = accounts_dir() / old
    target = accounts_dir() / new
    if target.exists():
        raise AccountError(f"Account already exists: {new}")

    was_active = current_account() == old
    source.rename(target)
    _rewrite_notebook_target_cache_account(old, new)
    if was_active:
        _atomic_write_text(current_file(), new + "\n")
    _clear_process_account_caches()


def _rewrite_notebook_target_cache_account(old: str, new: str) -> None:
    """Rewrite remembered cross-account notebook target selections."""
    path = inspire_home() / NOTEBOOK_TARGET_CACHE_FILENAME
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    targets = data.get("targets")
    if not isinstance(targets, dict):
        return

    changed = False
    updated_at = int(time.time())
    for entry in targets.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("account") != old:
            continue
        entry["account"] = new
        entry["updated_at"] = updated_at
        changed = True
    if not changed:
        return

    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
