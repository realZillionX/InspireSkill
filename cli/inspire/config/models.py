"""Configuration models and shared constants for Inspire CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Config file paths
CONFIG_FILENAME = "config.toml"
PROJECT_CONFIG_DIR = ".inspire"  # ./.inspire/accounts/<account>/config.toml
PROJECT_ACCOUNT_CONFIG_DIR = "accounts"


class ConfigError(Exception):
    """Configuration error - missing or invalid settings."""


# Source tracking for config values
SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_PROJECT = "project"
SOURCE_ENV = "env"
SOURCE_ENV_FILE = "env-file"


@dataclass
class Config:
    """Inspire CLI configuration."""

    # Required (for platform API)
    username: str
    password: str

    # Optional with defaults
    base_url: str = "https://api.example.com"
    log_pattern: str = "training_master_*.log"

    # API settings
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # GitHub settings
    github_repo: Optional[str] = None
    github_token: Optional[str] = None
    github_server: str = "https://github.com"
    github_sync_workflow: str = "sync_code.yml"
    github_bridge_workflow: str = "run_bridge_action.yml"

    log_cache_dir: str = "~/.inspire/logs"
    remote_timeout: int = 90

    # Sync settings
    default_remote: str = "origin"

    # Bridge action settings
    bridge_action_timeout: int = 600
    bridge_action_denylist: list[str] = field(default_factory=list)

    # API settings (additional)
    skip_ssl_verify: bool = False
    force_proxy: bool = False

    # API path prefixes (None = use code defaults)
    browser_api_prefix: Optional[str] = None
    docker_registry: Optional[str] = None

    # Proxy settings ([proxy] in TOML)
    requests_http_proxy: Optional[str] = None
    requests_https_proxy: Optional[str] = None
    playwright_proxy: Optional[str] = None
    rtunnel_proxy: Optional[str] = None

    # Job settings
    job_auto_fault_tolerance: bool = False
    job_fault_tolerance_max_retry: int = 10
    job_enable_notification: bool = False

    # Project alias map for project_id resolution (alias -> project-...)
    projects: dict[str, str] = field(default_factory=dict)

    # Discovered per-account project metadata (loaded from the account layer
    # [project_catalog] table when present; kept for display helpers like
    # `inspire config context`).
    # project_id -> metadata dict (best-effort, schema may evolve)
    project_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Legacy project_id -> discovered workdir field; new path aliases use project_catalog.
    project_workdirs: dict[str, str] = field(default_factory=dict)
    # Legacy account-level train job workdir field.
    account_train_job_workdir: Optional[str] = None

    # Notebook settings
    notebook_post_start: Optional[str] = None

    # Tunnel retry settings
    tunnel_retries: int = 3
    tunnel_retry_pause: float = 2.0

    # Other
    shm_size: Optional[int] = None

    # User-defined project selection order (list of project names or IDs)
    project_order: list[str] = field(default_factory=list)

    # Compute groups (loaded from config.toml [[compute_groups]] sections)
    compute_groups: list[dict] = field(default_factory=list)

    # Remote environment variables (injected into bridge exec, jobs, run commands)
    remote_env: dict[str, str] = field(default_factory=dict)

    # Project-scoped remote path aliases used by notebook exec/shell/scp.
    path_aliases: dict[str, str] = field(default_factory=dict)

    # Project-scoped workload condition profiles. These are command aliases
    # for workspace/project/group/image/quota, not defaults.
    profiles: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    # Display-only project context from project config. These names are shown
    # by `inspire config context`; create commands still require explicit
    # arguments or workload profiles.
    context_project: Optional[str] = None
    context_workspace: Optional[str] = None

    # Source precedence: "env" (default) = env vars win, "toml" = project TOML wins
    prefer_source: str = "env"

    @classmethod
    def writable_config_path(cls) -> Optional[Path]:
        """Return the active account's ``config.toml`` path, or ``None``.

        ``None`` signals "no global-scope write target" — the caller
        (typically ``inspire init --global`` or discover writes) should
        error out at that point with a clear "run 'inspire account add'
        first" message. Project-scope writes don't consult this and are
        unaffected.
        """
        from inspire.accounts import account_config_path, current_account

        name = current_account()
        if not name:
            return None
        return account_config_path(name)

    @classmethod
    def _find_project_config(cls) -> Path | None:
        from inspire.config.toml import _find_project_config

        return _find_project_config()

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
    def from_env(cls) -> "Config":
        from inspire.config.load_env import config_from_env

        return config_from_env()

    @classmethod
    def from_files_and_env(
        cls, require_credentials: bool = True, account: str | None = None
    ) -> tuple["Config", dict[str, str]]:
        from inspire.config.load import config_from_files_and_env

        return config_from_files_and_env(require_credentials=require_credentials, account=account)

    @classmethod
    def get_config_paths(cls, account: str | None = None) -> tuple[Path | None, Path | None]:
        from inspire.config.load import get_config_paths

        return get_config_paths(account=account)
