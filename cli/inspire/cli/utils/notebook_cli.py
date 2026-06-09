"""Notebook command helpers.

These helpers centralize global CLI behaviors (JSON output, config/session loading)
so notebook subcommands can stay small and consistent.
"""

from __future__ import annotations

import os

from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.platform.web import session as web_session_module
from inspire.config import Config, ConfigError
from inspire.cli.utils.errors import exit_with_error


# Single source of truth for the "you need a logged-in account" hint.
# Surface in every command that touches a web session, so users get a
# consistent next-action regardless of where they hit the wall.
WEB_AUTH_HINT = (
    "Run `inspire account add <name>` first; that command captures the platform "
    "credentials, sets the active account, and provisions Playwright for SSO login."
)


def get_base_url(account: str | None = None) -> str:
    try:
        if account:
            config, _ = Config.from_files_and_env(require_credentials=False, account=account)
        else:
            config, _ = Config.from_files_and_env(require_credentials=False)
        return config.base_url
    except Exception:
        return os.environ.get("INSPIRE_BASE_URL", "https://api.example.com")


def resolve_json_output(ctx: Context, json_output: bool) -> bool:
    if json_output and not ctx.json_output:
        ctx.json_output = True
    return ctx.json_output


def require_web_session(
    ctx: Context,
    *,
    hint: str,
    account: str | None = None,
) -> web_session_module.WebSession:
    try:
        if account:
            return web_session_module.get_web_session(account=account)
        return web_session_module.get_web_session()
    except (ValueError, ConfigError) as e:
        exit_with_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR, hint=hint)
        raise  # pragma: no cover


def load_config(ctx: Context, account: str | None = None) -> Config:
    try:
        if account:
            config, _ = Config.from_files_and_env(require_credentials=False, account=account)
        else:
            config, _ = Config.from_files_and_env(require_credentials=False)
        return config
    except ConfigError as e:
        exit_with_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        raise  # pragma: no cover
