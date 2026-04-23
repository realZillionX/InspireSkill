"""Configuration models and shared constants for Inspire CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from inspire.config.rtunnel_defaults import default_rtunnel_download_url

# Config file paths
CONFIG_FILENAME = "config.toml"
PROJECT_CONFIG_DIR = ".inspire"  # ./.inspire/config.toml


class ConfigError(Exception):
    """Configuration error - missing or invalid settings."""


# Source tracking for config values
SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_PROJECT = "project"
SOURCE_ENV = "env"


@dataclass
class Config:
    """Inspire CLI configuration."""

    # Required (for platform API)
    username: str
    password: str

    # Optional with defaults
    base_url: str = "https://api.example.com"
    target_dir: Optional[str] = None  # INSPIRE_TARGET_DIR - unified for all Bridge operations
    log_pattern: str = "training_master_*.log"
    job_cache_path: str = "~/.inspire/jobs.json"

    # API settings
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # GitHub settings
    github_repo: Optional[str] = None
    github_token: Optional[str] = None
    github_server: str = "https://github.com"
    github_log_workflow: str = "retrieve_job_log.yml"
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
    openapi_prefix: Optional[str] = None
    browser_api_prefix: Optional[str] = None
    auth_endpoint: Optional[str] = None
    docker_registry: Optional[str] = None

    # Proxy settings ([proxy] in TOML)
    requests_http_proxy: Optional[str] = None
    requests_https_proxy: Optional[str] = None
    playwright_proxy: Optional[str] = None
    rtunnel_proxy: Optional[str] = None

    # Job settings
    job_priority: int = 10
    job_image: Optional[str] = None
    job_project_id: Optional[str] = None
    job_workspace_id: Optional[str] = None

    # Workspace routing (optional)
    workspace_cpu_id: Optional[str] = None
    workspace_gpu_id: Optional[str] = None
    workspace_internet_id: Optional[str] = None

    # Full workspace map loaded from TOML [workspaces]
    workspaces: dict[str, str] = field(default_factory=dict)

    # Project alias map for project_id resolution (alias -> project-...)
    projects: dict[str, str] = field(default_factory=dict)

    # Discovered per-account project metadata (loaded from layered [accounts."<user>"] catalog)
    # project_id -> metadata dict (best-effort, schema may evolve)
    project_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    # project_id -> shared-path grouping key (e.g. "/train/global_user/<user>")
    project_shared_path_groups: dict[str, str] = field(default_factory=dict)
    # project_id -> discovered workdir (best-effort; may come from API or probing)
    project_workdirs: dict[str, str] = field(default_factory=dict)
    # Account-level shared-path grouping key (if available)
    account_shared_path_group: Optional[str] = None
    # Account-level train job workdir (if available)
    account_train_job_workdir: Optional[str] = None

    # Project context account binding (from [context].account)
    context_account: Optional[str] = None

    # Notebook settings
    notebook_resource: str = "1xH200"
    notebook_image: Optional[str] = None
    notebook_post_start: Optional[str] = None

    # SSH settings
    sshd_deb_dir: Optional[str] = None
    dropbear_deb_dir: Optional[str] = None
    setup_script: Optional[str] = None
    rtunnel_download_url: str = field(default_factory=default_rtunnel_download_url)
    apt_mirror_url: Optional[str] = None

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

    # Global per-account secrets map, loaded from global config:
    # [accounts."<username>"].password
    accounts: dict[str, str] = field(default_factory=dict)

    # Source precedence: "env" (default) = env vars win, "toml" = project TOML wins
    prefer_source: str = "env"

    # Class-level config paths
    GLOBAL_CONFIG_PATH_ENV_VAR = "INSPIRE_GLOBAL_CONFIG_PATH"
    GLOBAL_CONFIG_PATH = Path.home() / ".config" / "inspire" / CONFIG_FILENAME

    @classmethod
    def resolve_global_config_path(cls) -> Path:
        """Return the effective global config path, honoring env override."""
        override = str(os.getenv(cls.GLOBAL_CONFIG_PATH_ENV_VAR) or "").strip()
        if override:
            return Path(override).expanduser()
        return cls.GLOBAL_CONFIG_PATH

    def get_expanded_cache_path(self) -> str:
        """Get the job cache path with ~ expanded."""
        return os.path.expanduser(self.job_cache_path)

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
    def from_env(cls, require_target_dir: bool = False) -> "Config":
        from inspire.config.load_env import config_from_env

        return config_from_env(require_target_dir=require_target_dir)

    @classmethod
    def from_env_for_sync(cls) -> "Config":
        from inspire.config.load_env import config_from_env_for_sync

        return config_from_env_for_sync()

    @classmethod
    def from_files_and_env(
        cls, require_target_dir: bool = False, require_credentials: bool = True
    ) -> tuple["Config", dict[str, str]]:
        from inspire.config.load import config_from_files_and_env

        return config_from_files_and_env(
            require_target_dir=require_target_dir, require_credentials=require_credentials
        )

    @classmethod
    def get_config_paths(cls) -> tuple[Path | None, Path | None]:
        from inspire.config.load import get_config_paths

        return get_config_paths()
