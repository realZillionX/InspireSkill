"""Config options: Job, Notebook, Sync, and Workspaces."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_int

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
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_ID",
        toml_key="job.workspace_id",
        field_name="job_workspace_id",
        description="Default workspace ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
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
]

NOTEBOOK_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_RESOURCE",
        toml_key="notebook.resource",
        field_name="notebook_resource",
        description="Default resource for notebooks",
        default="1xH200",
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
