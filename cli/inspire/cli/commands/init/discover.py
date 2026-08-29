"""Account discovery and normalization for ``inspire init``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import click

from inspire.config import Config
from inspire.platform.web.browser_api.core import _set_base_url
from inspire.platform.web.session import AuthenticationError
from inspire.platform.web.session.browser_launch import is_playwright_browser_runtime_error

from .toml_helpers import _toml_dumps


logger = logging.getLogger(__name__)

_USERNAME_PLACEHOLDERS = frozenset({"your_username"})
_BASE_URL_PLACEHOLDER = "https://api.example.com"
_OBSOLETE_ACCOUNT_TABLES = frozenset(
    {
        "compute_groups",
        "context",
        "path_aliases",
        "profiles",
        "project_catalog",
        "projects",
        "paths",
    }
)
_OBSOLETE_ACCOUNT_TABLE_FIELDS: dict[str, frozenset[str]] = {
    "api": frozenset({"docker_registry"}),
}


def _progress(verbose: bool, message: str) -> None:
    if verbose:
        logger.debug("%s", message)


def _ensure_playwright_browser(*, non_interactive: bool = False) -> None:
    """Check that the local browser runtime is installed; offer to install it."""
    import subprocess
    import sys

    try:
        from playwright.sync_api import sync_playwright
        from inspire.platform.web.session.browser_launch import (
            chromium_launch_kwargs,
            playwright_install_args,
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**chromium_launch_kwargs(headless=True))
            browser.close()
        return
    except Exception:
        pass

    install_args = playwright_install_args()
    if not non_interactive:
        click.echo()
        if "--with-deps" in install_args:
            click.echo(
                "A local browser runtime and Linux system dependencies are required for "
                "platform login (one-time setup)."
            )
        else:
            click.echo(
                "A local browser runtime is required for platform login "
                "(one-time ~150 MB download)."
            )
        if not click.confirm("Install Chromium now?", default=True):
            click.echo("Cannot proceed without a browser for platform login.")
            raise SystemExit(1)

    result = subprocess.run(
        [sys.executable, "-m", "playwright", *install_args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        click.echo(
            click.style(
                "Chromium installation failed. Run `python -m playwright install chromium` "
                "and retry.",
                fg="red",
            ),
            err=True,
        )
        raise SystemExit(1)


def _resolve_credentials_interactive(
    config: object,
    *,
    cli_username: str | None,
    cli_base_url: str | None,
    allow_config_password: bool = False,
    confirm_config_username: bool = False,
    non_interactive: bool = False,
) -> tuple[str, str, str]:
    """Resolve base URL, username and password, prompting when missing."""
    base_url = (cli_base_url or "").strip() or _usable_base_url(
        getattr(config, "base_url", "")
    )
    if not base_url:
        if non_interactive:
            raise ValueError("Platform URL is required for non-interactive init.")
        base_url = click.prompt("Platform URL", type=str).strip()
    if not base_url:
        raise ValueError("Platform URL is required.")

    username = (cli_username or "").strip()
    if not username:
        configured = _usable_username(getattr(config, "username", ""))
        if configured and confirm_config_username and not non_interactive:
            username = click.prompt(
                "Platform login name (not display name)",
                default=configured,
                type=str,
            ).strip()
        else:
            username = configured
    if not username:
        if non_interactive:
            raise ValueError("Username is required for non-interactive init.")
        username = click.prompt("Platform login name (not display name)", type=str).strip()
    if not username:
        raise ValueError("Username is required.")

    password = ""
    if allow_config_password or non_interactive:
        password = str(getattr(config, "password", "") or "").strip()
    if not password:
        if non_interactive:
            raise ValueError("Password is required for non-interactive init.")
        password = click.prompt("Password", type=str, hide_input=True)
    if not password:
        raise ValueError("Password is required.")

    return username, password, base_url


def _ensure_ssh_key(*, non_interactive: bool = False) -> None:
    """Check for an SSH key; offer to generate one if missing."""
    import subprocess

    ssh_dir = Path.home() / ".ssh"
    candidates = [ssh_dir / "id_ed25519.pub", ssh_dir / "id_rsa.pub"]
    if any(path.exists() for path in candidates) or non_interactive:
        return

    click.echo()
    click.echo("No SSH key found. SSH keys are needed for bridge/tunnel/notebook SSH features.")
    stdin = click.get_text_stream("stdin")
    if not getattr(stdin, "isatty", lambda: False)():
        click.echo("Skipping SSH key generation in non-interactive mode.")
        return
    if not click.confirm("Generate a new ed25519 SSH key?", default=True):
        return

    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-f",
            str(ssh_dir / "id_ed25519"),
            "-N",
            "",
            "-C",
            "inspire-skill",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    click.echo("SSH key generated." if result.returncode == 0 else "SSH key generation failed.")


def _usable_username(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in _USERNAME_PLACEHOLDERS:
        return ""
    return text


def _usable_base_url(value: object) -> str:
    text = str(value or "").strip()
    if not text or text == _BASE_URL_PLACEHOLDER:
        return ""
    return text


def _resolve_discover_runtime(
    *,
    config: Config,
    web_session_module,  # noqa: ANN001
    default_workspace_id: str,
    cli_username: str | None,
    cli_base_url: str | None,
    non_interactive: bool = False,
    verbose: bool = False,
) -> tuple[object, tuple[str, str, str] | None, str]:
    session = None
    prompted_credentials: tuple[str, str, str] | None = None
    if cli_username or cli_base_url:
        _ensure_playwright_browser(non_interactive=non_interactive)
        username, password, base_url = _resolve_credentials_interactive(
            config,
            cli_username=cli_username,
            cli_base_url=cli_base_url,
            allow_config_password=True,
            non_interactive=non_interactive,
        )
        prompted_credentials = (username, password, base_url)
        _progress(verbose, "Logging in...")
        session = web_session_module.login_with_playwright(
            username,
            password,
            base_url=base_url,
        )
    else:
        try:
            session = web_session_module.get_web_session(require_workspace=True)
        except (ValueError, RuntimeError) as exc:
            _ensure_playwright_browser(non_interactive=non_interactive)
            if is_playwright_browser_runtime_error(exc):
                try:
                    session = web_session_module.get_web_session(
                        force_refresh=True,
                        require_workspace=True,
                    )
                except (ValueError, RuntimeError) as retry_exc:
                    if is_playwright_browser_runtime_error(retry_exc):
                        raise
            if session is None:
                if isinstance(exc, AuthenticationError):
                    click.echo(click.style(str(exc), fg="yellow"), err=True)
                username, password, base_url = _resolve_credentials_interactive(
                    config,
                    cli_username=cli_username,
                    cli_base_url=cli_base_url,
                    confirm_config_username=True,
                    non_interactive=non_interactive,
                )
                prompted_credentials = (username, password, base_url)
                _progress(verbose, "Logging in...")
                session = web_session_module.login_with_playwright(
                    username,
                    password,
                    base_url=base_url,
                )

    account_key = (
        _usable_username(prompted_credentials[0])
        if prompted_credentials
        else _usable_username(getattr(session, "login_username", ""))
        or _usable_username(getattr(config, "username", ""))
    )
    if not account_key:
        raise ValueError("Could not resolve account login name.")

    if prompted_credentials:
        _set_base_url(prompted_credentials[2])
    else:
        base_url = _usable_base_url(getattr(config, "base_url", "")) or _usable_base_url(
            getattr(session, "base_url", "")
        )
        if base_url:
            _set_base_url(base_url)

    workspace_id = str(getattr(session, "workspace_id", "") or "").strip()
    if not workspace_id or workspace_id == default_workspace_id:
        raise ValueError(
            "Could not detect an accessible workspace from the authenticated session. "
            "Re-run `inspire init` with an account that can see at least one workspace."
        )

    return session, prompted_credentials, account_key


def _sanitize_account_config(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Drop retired repository-derived and unused account fields."""
    cleaned: dict[str, Any] = {}
    for key, raw_value in raw_data.items():
        if key in _OBSOLETE_ACCOUNT_TABLES:
            continue
        if not isinstance(raw_value, dict):
            cleaned[key] = raw_value
            continue
        table = dict(raw_value)
        for field in _OBSOLETE_ACCOUNT_TABLE_FIELDS.get(key, frozenset()):
            table.pop(field, None)
        if table:
            cleaned[key] = table
    return cleaned


def _persist_api_base_url(
    *,
    account_data: dict[str, Any],
    config: Config,
    session: Any | None = None,
) -> None:
    base_url = _usable_base_url(getattr(config, "base_url", ""))
    if not base_url and session is not None:
        base_url = _usable_base_url(getattr(session, "base_url", ""))
    if not base_url:
        return
    api = account_data.get("api")
    if not isinstance(api, dict):
        api = {}
        account_data["api"] = api
    if not _usable_base_url(api.get("base_url")):
        api["base_url"] = base_url


def _persist_prompted_credentials(
    *,
    account_data: dict[str, Any],
    prompted_credentials: tuple[str, str, str] | None,
) -> None:
    if not prompted_credentials:
        return
    username, password, base_url = prompted_credentials
    auth = account_data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        account_data["auth"] = auth
    auth["username"] = username
    auth["password"] = password
    api = account_data.get("api")
    if not isinstance(api, dict):
        api = {}
        account_data["api"] = api
    api["base_url"] = base_url


def _persist_cached_session_identity(*, account_data: dict[str, Any], session: Any) -> None:
    username = _usable_username(getattr(session, "login_username", ""))
    if not username:
        return
    auth = account_data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        account_data["auth"] = auth
    if not _usable_username(auth.get("username")):
        auth["username"] = username


def _persist_account_config(
    *,
    force: bool,
    config: Config,
    session: Any,
    prompted_credentials: tuple[str, str, str] | None,
    non_interactive: bool,
    verbose: bool,
) -> None:
    path = Config.writable_config_path()
    if path is None:
        raise click.ClickException("No active account configured. Run `inspire account add` first.")
    if path.exists() and not force:
        if non_interactive:
            raise ValueError(
                "Account configuration already exists; rerun non-interactive init with --force."
            )
        click.echo(click.style("Account configuration already exists.", fg="yellow"))
        if not click.confirm(
            "Refresh and remove obsolete derived fields? (will rewrite file)",
            default=True,
        ):
            return

    raw_data = Config._load_toml(path) if path.exists() else {}
    account_data = _sanitize_account_config(raw_data)
    _persist_api_base_url(account_data=account_data, config=config, session=session)
    _persist_prompted_credentials(
        account_data=account_data,
        prompted_credentials=prompted_credentials,
    )
    if not prompted_credentials:
        _persist_cached_session_identity(account_data=account_data, session=session)

    _progress(verbose, "Writing account configuration...")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml_dumps(account_data), encoding="utf-8")
    if prompted_credentials:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    _ensure_ssh_key(non_interactive=non_interactive)


def _init_discover_mode(
    force: bool,
    *,
    cli_username: str | None = None,
    cli_base_url: str | None = None,
    non_interactive: bool = False,
    verbose: bool = False,
) -> None:
    """Validate the live session and normalize the active account file."""
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session import DEFAULT_WORKSPACE_ID
    from inspire.platform.web.session.browser_client import _close_browser_client

    config, _ = Config.from_files_and_env(require_credentials=False)
    session, prompted_credentials, account_key = _resolve_discover_runtime(
        config=config,
        web_session_module=web_session_module,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
        non_interactive=non_interactive,
        verbose=verbose,
    )
    _progress(verbose, f"Account: {account_key}")
    try:
        _persist_account_config(
            force=force,
            config=config,
            session=session,
            prompted_credentials=prompted_credentials,
            non_interactive=non_interactive,
            verbose=verbose,
        )
    finally:
        _close_browser_client()


__all__ = ["_init_discover_mode", "_sanitize_account_config"]
