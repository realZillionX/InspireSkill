"""Config options for GitHub Actions."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_int

GITHUB_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_GITHUB_SERVER",
        toml_key="github.server",
        field_name="github_server",
        description="GitHub server URL",
        default="https://github.com",
        category="GitHub",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_REPO",
        toml_key="github.repo",
        field_name="github_repo",
        description="GitHub repository (owner/repo format)",
        default=None,
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_TOKEN",
        toml_key="github.token",
        field_name="github_token",
        description="GitHub personal access token (falls back to GITHUB_TOKEN)",
        default=None,
        category="GitHub",
        secret=True,
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_LOG_WORKFLOW",
        toml_key="github.log_workflow",
        field_name="github_log_workflow",
        description="Workflow filename for retrieving logs",
        default="retrieve_job_log.yml",
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_SYNC_WORKFLOW",
        toml_key="github.sync_workflow",
        field_name="github_sync_workflow",
        description="Workflow filename for code sync",
        default="sync_code.yml",
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_GITHUB_BRIDGE_WORKFLOW",
        toml_key="github.bridge_workflow",
        field_name="github_bridge_workflow",
        description="Workflow filename for bridge execution",
        default="run_bridge_action.yml",
        category="GitHub",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_REMOTE_TIMEOUT",
        toml_key="github.remote_timeout",
        field_name="remote_timeout",
        description="Max time to wait for remote artifact (seconds)",
        default=90,
        category="GitHub",
        parser=_parse_int,
        scope="project",
    ),
]
