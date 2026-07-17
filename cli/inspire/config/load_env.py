"""Environment-based config loading for Inspire CLI."""

from __future__ import annotations

import os

from inspire.config.env import _parse_denylist, _parse_remote_timeout
from inspire.config.models import Config, ConfigError
from inspire.config.schema_models import _parse_bool


def config_from_env() -> Config:
    """Create configuration from environment variables."""
    username = os.getenv("INSPIRE_USERNAME")
    password = os.getenv("INSPIRE_PASSWORD")

    if not username or not password:
        raise ConfigError(
            "Missing platform credentials. Run `inspire account add <name>` "
            "to configure them; the active account is the supported source."
        )

    timeout = 30
    max_retries = 3
    retry_delay = 1.0

    timeout_env = os.getenv("INSPIRE_TIMEOUT")
    if timeout_env:
        try:
            timeout = int(timeout_env)
        except ValueError as e:
            raise ConfigError(
                "Invalid INSPIRE_TIMEOUT value. It must be an integer number of seconds."
            ) from e

    max_retries_env = os.getenv("INSPIRE_MAX_RETRIES")
    if max_retries_env:
        try:
            max_retries = int(max_retries_env)
        except ValueError as e:
            raise ConfigError("Invalid INSPIRE_MAX_RETRIES value. It must be an integer.") from e

    retry_delay_env = os.getenv("INSPIRE_RETRY_DELAY")
    if retry_delay_env:
        try:
            retry_delay = float(retry_delay_env)
        except ValueError as e:
            raise ConfigError(
                "Invalid INSPIRE_RETRY_DELAY value. It must be a number of seconds."
            ) from e

    bridge_action_timeout = 600
    bat_env = os.getenv("INSPIRE_BRIDGE_ACTION_TIMEOUT")
    if bat_env:
        try:
            bridge_action_timeout = int(bat_env)
        except ValueError as e:
            raise ConfigError(
                "Invalid INSPIRE_BRIDGE_ACTION_TIMEOUT value. It must be an integer number of seconds."
            ) from e

    log_cache_dir = os.getenv("INSP_LOG_CACHE_DIR") or os.getenv(
        "INSPIRE_LOG_CACHE_DIR", "~/.inspire/logs"
    ) or "~/.inspire/logs"

    return Config(
        username=username,
        password=password,
        base_url=os.getenv("INSPIRE_BASE_URL", "https://api.example.com"),
        log_pattern=os.getenv("INSPIRE_LOG_PATTERN", "training_master_*.log"),
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        github_repo=os.getenv("INSP_GITHUB_REPO"),
        github_token=os.getenv("INSP_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN"),
        github_server=os.getenv("INSP_GITHUB_SERVER", "https://github.com"),
        github_sync_workflow=os.getenv("INSP_GITHUB_SYNC_WORKFLOW", "sync_code.yml"),
        github_bridge_workflow=os.getenv("INSP_GITHUB_BRIDGE_WORKFLOW", "run_bridge_action.yml"),
        log_cache_dir=log_cache_dir,
        remote_timeout=_parse_remote_timeout(os.getenv("INSP_REMOTE_TIMEOUT", "90")),
        default_remote=os.getenv("INSPIRE_DEFAULT_REMOTE", "origin"),
        bridge_action_timeout=bridge_action_timeout,
        bridge_action_denylist=_parse_denylist(os.getenv("INSPIRE_BRIDGE_DENYLIST")),
        requests_http_proxy=os.getenv("INSPIRE_REQUESTS_HTTP_PROXY"),
        requests_https_proxy=os.getenv("INSPIRE_REQUESTS_HTTPS_PROXY"),
        playwright_proxy=os.getenv("INSPIRE_PLAYWRIGHT_PROXY"),
        rtunnel_proxy=os.getenv("INSPIRE_RTUNNEL_PROXY"),
        job_enable_notification=_parse_bool(
            os.getenv("INSPIRE_JOB_ENABLE_NOTIFICATION", "false")
        ),
    )
