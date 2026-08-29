"""Configuration models and shared constants for Inspire CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Account config file name
CONFIG_FILENAME = "config.toml"

# The only Inspire deployment anyone points this CLI at. `base_url` stays
# configurable for staging hosts, but the default has to be a host that
# actually answers: a placeholder default made `inspire account check` report
# "placeholder host" to users whose only mistake was not running
# `inspire account add` yet.
DEFAULT_BASE_URL = "https://qz.sii.edu.cn"


class ConfigError(Exception):
    """Configuration error - missing or invalid settings."""


# Source tracking for config values
SOURCE_DEFAULT = "default"
SOURCE_ACCOUNT = "account"
SOURCE_ENV = "env"
SOURCE_ENV_FILE = "env-file"


@dataclass
class Config:
    """Inspire CLI configuration."""

    # Required (for platform API)
    username: str
    password: str

    # Optional with defaults
    base_url: str = DEFAULT_BASE_URL

    # Proxy settings ([proxy] in TOML)
    requests_http_proxy: Optional[str] = None
    requests_https_proxy: Optional[str] = None
    playwright_proxy: Optional[str] = None
    rtunnel_proxy: Optional[str] = None

    # Job settings
    job_auto_fault_tolerance: bool = False
    job_fault_tolerance_max_retry: int = 10
    job_enable_notification: bool = False

    # Notebook settings
    notebook_post_start: Optional[str] = None

    # Tunnel retry settings
    tunnel_retries: int = 3
    tunnel_retry_pause: float = 2.0

    # Other
    shm_size: Optional[int] = None

    # User-defined project selection order (list of project names or aliases)
    project_order: list[str] = field(default_factory=list)

    # Remote environment variables (injected into notebook commands and jobs)
    remote_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def writable_config_path(cls) -> Optional[Path]:
        """Return the active account's ``config.toml`` path, or ``None``.

        ``None`` signals that no active account exists. Callers should fail
        with a clear ``inspire account add`` / ``account use`` hint.
        """
        from inspire.accounts import account_config_path, current_account

        name = current_account()
        if not name:
            return None
        return account_config_path(name)

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        from inspire.config.toml import _load_toml

        return _load_toml(path)

    @staticmethod
    def _flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        from inspire.config.toml import _flatten_toml

        return _flatten_toml(data, prefix)

    @classmethod
    def _toml_key_to_field(cls, toml_key: str) -> str | None:
        from inspire.config.toml import _toml_key_to_field

        return _toml_key_to_field(toml_key)

    @classmethod
    def from_files_and_env(
        cls, require_credentials: bool = True, account: str | None = None
    ) -> tuple["Config", dict[str, str]]:
        from inspire.config.load import config_from_files_and_env

        return config_from_files_and_env(require_credentials=require_credentials, account=account)
