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


def get_base_url() -> str:
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        return config.base_url
    except Exception:
        return os.environ.get("INSPIRE_BASE_URL", "https://api.example.com")


def resolve_json_output(ctx: Context, json_output: bool) -> bool:
    if json_output and not ctx.json_output:
        ctx.json_output = True
    return ctx.json_output


def require_web_session(ctx: Context, *, hint: str) -> web_session_module.WebSession:
    try:
        return web_session_module.get_web_session()
    except (ValueError, ConfigError) as e:
        exit_with_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR, hint=hint)
        raise  # pragma: no cover


def load_config(ctx: Context) -> Config:
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        return config
    except ConfigError as e:
        exit_with_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        raise  # pragma: no cover
