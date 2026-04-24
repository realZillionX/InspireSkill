"""Config options: SSH, Tunnel, Bridge, and Paths."""

from __future__ import annotations

from inspire.config.schema_models import (
    ConfigOption,
    _parse_float,
    _parse_int,
    _parse_list,
)

# SSH bootstrap has no user-configurable knobs: the container-side SSH stack
# (rtunnel + sshd) is installed exclusively from the global_public offline kit
# at /inspire/hdd/global_public/inspire-skill-bootstrap/v1/ (see
# inspire.platform.web.browser_api.rtunnel.INSPIRE_BOOTSTRAP_ROOT).
SSH_OPTIONS: list[ConfigOption] = []

TUNNEL_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRIES",
        toml_key="tunnel.retries",
        field_name="tunnel_retries",
        description="SSH tunnel connection retries",
        default=3,
        category="Tunnel",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRY_PAUSE",
        toml_key="tunnel.retry_pause",
        field_name="tunnel_retry_pause",
        description="Seconds to wait between SSH tunnel retries",
        default=2.0,
        category="Tunnel",
        parser=_parse_float,
        scope="global",
    ),
]

BRIDGE_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_BRIDGE_ACTION_TIMEOUT",
        toml_key="bridge.action_timeout",
        field_name="bridge_action_timeout",
        description="Bridge action timeout in seconds",
        default=600,
        category="Bridge",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_BRIDGE_DENYLIST",
        toml_key="bridge.denylist",
        field_name="bridge_action_denylist",
        description="Glob patterns to block from sync (comma/newline separated)",
        default=[],
        category="Bridge",
        parser=_parse_list,
        scope="project",
    ),
]

PATHS_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TARGET_DIR",
        toml_key="paths.target_dir",
        field_name="target_dir",
        description="Target directory on Bridge shared filesystem",
        default=None,
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_LOG_PATTERN",
        toml_key="paths.log_pattern",
        field_name="log_pattern",
        description="Log file glob pattern",
        default="training_master_*.log",
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_CACHE",
        toml_key="paths.job_cache",
        field_name="job_cache_path",
        description="Local job cache file path",
        default="~/.inspire/jobs.json",
        category="Paths",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_LOG_CACHE_DIR",
        toml_key="paths.log_cache_dir",
        field_name="log_cache_dir",
        description="Cache directory for remote logs",
        default="~/.inspire/logs",
        category="Paths",
        scope="global",
    ),
]
