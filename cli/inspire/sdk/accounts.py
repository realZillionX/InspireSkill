"""Local account management. No prompts, network requests, or browser launches."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from inspire.accounts import storage
from inspire.config import DEFAULT_BASE_URL, Config
from inspire.services.account.account_config import render_account_config, atomic_write_text, toml_dumps
from .exceptions import ValidationError


@dataclass(frozen=True)
class InitResult:
    config_path: Path
    changed: bool
    warnings: tuple[str, ...] = ()


@contextmanager
def _account_errors() -> Iterator[None]:
    try:
        yield
    except storage.AccountError as error:
        raise ValidationError(str(error)) from error


def _create(
    name: str,
    *,
    username: str,
    password: str,
    base_url: str,
    proxy: str | None,
    overwrite: bool = False,
) -> str:
    from inspire.accounts.normalize import normalize_environment

    with _account_errors():
        name = storage.validate_name(name)
        if not username.strip():
            raise ValidationError("Username cannot be empty.")
        storage.create_account(
            name,
            render_account_config(
                username=username.strip(),
                password=password,
                base_url=base_url.strip(),
                proxy=(proxy or "").strip(),
            ),
            overwrite=overwrite,
        )
        normalize_environment(interactive=False, auto_install_playwright=False)
    return name


class Accounts:
    """Manage local aliases independently of any client."""

    @staticmethod
    def list() -> tuple[str, ...]:
        return tuple(storage.list_accounts())

    @staticmethod
    def current() -> str | None:
        """Return the saved default, ignoring any command-scoped account pin."""
        return storage.default_account()

    @staticmethod
    def exists(name: str) -> bool:
        return storage.account_exists(name)

    @staticmethod
    def config_path(name: str) -> Path:
        with _account_errors():
            return storage.account_config_path(name)

    @staticmethod
    def add(
        name: str,
        *,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        proxy: str | None = None,
        use: bool = False,
        overwrite: bool = False,
    ) -> str:
        """Create an account; the first account becomes the saved default.

        Overwrite replaces the account directory, including its cached state.
        """
        first = not Accounts.list()
        name = _create(
            name,
            username=username,
            password=password,
            base_url=base_url,
            proxy=proxy,
            overwrite=overwrite,
        )
        if use or first:
            Accounts.use(name)
        return name

    @staticmethod
    def use(name: str) -> None:
        with _account_errors():
            storage.set_current_account(name)

    @staticmethod
    def remove(name: str) -> None:
        """Delete the account and its cached state immediately, without confirmation."""
        with _account_errors():
            storage.remove_account(name)

    @staticmethod
    def rename(old: str, new: str) -> None:
        with _account_errors():
            storage.rename_account(old, new)


def ensure_credentials(
    account: str | None, *, username: str, password: str, base_url: str | None, proxy: str | None
) -> str:
    """Prepare credentials without ever writing the saved default pointer."""
    with _account_errors():
        name = storage.validate_name(account if account is not None else username)
        path = storage.account_config_path(name)
        if not storage.account_dir(name).exists():
            return _create(
                name,
                username=username,
                password=password,
                base_url=base_url or DEFAULT_BASE_URL,
                proxy=proxy,
            )
        if not username.strip():
            raise ValidationError("Username cannot be empty.")
        data = Config._load_toml(path) if path.exists() else {}
        updates: dict[str, dict[str, str]] = {
            "auth": {"username": username.strip(), "password": password},
        }
        if base_url is not None:
            updates["api"] = {"base_url": base_url.strip()}
        if proxy is not None:
            updates["proxy"] = dict.fromkeys(
                ("requests_http", "requests_https", "playwright", "rtunnel"), proxy.strip()
            )
        changed = not path.exists()
        for section, values in updates.items():
            if not isinstance(data.get(section), dict):
                data[section] = {}
            for key, value in values.items():
                if data[section].get(key) != value:
                    data[section][key] = value
                    changed = True
        if changed:
            atomic_write_text(path, toml_dumps(data), private=True)
        return name
