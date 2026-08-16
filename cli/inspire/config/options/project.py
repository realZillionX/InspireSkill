"""Config options: Job and Notebook."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_bool, _parse_int

JOB_OPTIONS: list[ConfigOption] = [
    # Job project, image, and workspace defaults are intentionally unsupported.
    # Commands that need those values require explicit flags.
    ConfigOption(
        env_var="INSPIRE_SHM_SIZE",
        toml_key="job.shm_size",
        field_name="shm_size",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_AUTO_FAULT_TOLERANCE",
        toml_key="job.auto_fault_tolerance",
        field_name="job_auto_fault_tolerance",
        parser=_parse_bool,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY",
        toml_key="job.fault_tolerance_max_retry",
        field_name="job_fault_tolerance_max_retry",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_ENABLE_NOTIFICATION",
        toml_key="job.enable_notification",
        field_name="job_enable_notification",
        parser=_parse_bool,
        scope="project",
    ),
]

NOTEBOOK_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_POST_START",
        toml_key="notebook.post_start",
        field_name="notebook_post_start",
        scope="project",
    ),
]
