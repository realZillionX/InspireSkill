"""Local account management. No prompts, network requests, or browser launches."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import warnings
from typing import Iterator

from inspire.accounts import storage
from inspire.config import DEFAULT_BASE_URL
from inspire.services.account.account_config import render_account_config
from inspire.services.account.lifecycle import delete_local_account
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
    base_url: str,
    proxy: str | None,
    overwrite: bool = False,
) -> str:
    from inspire.accounts.normalize import normalize_environment

    with _account_errors():
        name = storage.validate_name(name)
        storage.create_account(
            name,
            render_account_config(
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
        username: str | None = None,
        password: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        proxy: str | None = None,
        use: bool | None = None,
        overwrite: bool = False,
    ) -> str:
        """Create a profile; activate if none is active unless use=False.

        Authorize separately with inspire login --account NAME.
        Overwrite replaces the account directory, including its cached state.
        """
        warn_deprecated_credentials(username, password)
        first = storage.default_account() is None
        name = _create(
            name,
            base_url=base_url,
            proxy=proxy,
            overwrite=overwrite,
        )
        if use or (use is None and first):
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
            delete_local_account(name)

    @staticmethod
    def rename(old: str, new: str) -> None:
        with _account_errors():
            storage.rename_account(old, new)


def warn_deprecated_credentials(username: str | None, password: str | None) -> None:
    """Accept old SDK kwargs without using or persisting their values."""
    if username is not None or password is not None:
        warnings.warn(
            "username and password are ignored; authorize with inspire login --account NAME.",
            DeprecationWarning,
            stacklevel=3,
        )
