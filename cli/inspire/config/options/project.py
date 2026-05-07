"""Config options: Job, Notebook, Sync, and Workspaces."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_bool, _parse_int

JOB_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_PRIORITY",
        toml_key="job.priority",
        field_name="job_priority",
        description="Default job priority (1-10)",
        default=10,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_IMAGE",
        toml_key="job.image",
        field_name="job_image",
        description="Default Docker image for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_PROJECT_ID",
        toml_key="job.project_id",
        field_name="job_project_id",
        description="Default project ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    # NOTE: `[job].workspace_id` / `INSPIRE_WORKSPACE_ID` was removed in
    # v3.1.0. The "default workspace" concept is gone — workspace must be
    # passed explicitly via `--workspace <alias>` (resolved against
    # ``[workspaces]`` map) on every command that needs one. The loader
    # warns once if it still sees the legacy field; see
    # ``config/load_common.py::_warn_legacy_default_workspace``.
    ConfigOption(
        env_var="INSPIRE_SHM_SIZE",
        toml_key="job.shm_size",
        field_name="shm_size",
        description="Default shared memory size in GB (jobs + notebooks)",
        default=None,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_AUTO_FAULT_TOLERANCE",
        toml_key="job.auto_fault_tolerance",
        field_name="job_auto_fault_tolerance",
        description="Enable training fault tolerance by default",
        default=False,
        category="Job",
        parser=_parse_bool,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY",
        toml_key="job.fault_tolerance_max_retry",
        field_name="job_fault_tolerance_max_retry",
        description="Default max retry count when fault tolerance is enabled",
        default=10,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
]

NOTEBOOK_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_QUOTA",
        toml_key="notebook.quota",
        field_name="notebook_quota",
        description="Default quota for notebooks as 'gpu,cpu,mem' (mem in GiB)",
        default=None,
        category="Notebook",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_IMAGE",
        toml_key="notebook.image",
        field_name="notebook_image",
        description="Default Docker image for notebooks",
        default=None,
        category="Notebook",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_POST_START",
        toml_key="notebook.post_start",
        field_name="notebook_post_start",
        description="Post-start notebook action: none or a shell command",
        default=None,
        category="Notebook",
        scope="project",
    ),
]

SYNC_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_DEFAULT_REMOTE",
        toml_key="sync.default_remote",
        field_name="default_remote",
        description="Default git remote name",
        default="origin",
        category="Sync",
        scope="project",
    ),
]

WORKSPACES_OPTIONS: list[ConfigOption] = []
