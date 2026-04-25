"""Shared helpers for submitting jobs via the Inspire OpenAPI client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import ProjectInfo
from inspire.config import Config, ConfigError, build_env_exports
from inspire.cli.utils.job_cache import JobCache
from inspire.cli.utils.quota_resolver import ResolvedQuota


@dataclass(frozen=True)
class JobSubmission:
    job_id: Optional[str]
    data: dict
    result: Any
    log_path: Optional[str]
    wrapped_command: str
    max_time_ms: str


def wrap_in_bash(command: str) -> str:
    """Wrap a command in bash -c unless already wrapped."""
    stripped = command.strip()

    if stripped.startswith(("bash -c ", "sh -c ", "/bin/bash -c ", "/bin/sh -c ")):
        return command

    escaped = command.replace("'", "'\\''")
    return f"bash -c '{escaped}'"


def build_remote_logged_command(config: Config, *, command: str) -> tuple[str, str | None]:
    """Build the remote command (with optional logging) and return (final_command, log_path)."""
    env_exports = build_env_exports(config.remote_env)
    final_command = f"{env_exports}{command}" if env_exports else command

    log_path = None
    if config.target_dir:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(config.target_dir, ".inspire")
        log_filename = f"training_master_{timestamp}.log"
        log_path = os.path.join(log_dir, log_filename)
        final_command = (
            f'{env_exports}mkdir -p "{log_dir}" && ( cd "{config.target_dir}" && {command} ) '
            f'> "{log_path}" 2>&1'
        )

    return final_command, log_path


def select_project_for_workspace(
    config: Config,
    *,
    workspace_id: str,
    requested: str | None,
) -> tuple[ProjectInfo, str | None]:
    """Select a project for the given workspace, with quota-aware fallback."""
    try:
        session = web_session_module.get_web_session()
    except ValueError as e:
        raise ConfigError(str(e)) from e

    projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    if not projects:
        raise ConfigError("No projects available")

    congested = browser_api_module.check_scheduling_health(
        workspace_id=workspace_id,
        project_ids={p.project_id for p in projects},
        session=session,
    )

    requested_value = requested
    if not requested_value and not config.project_order:
        requested_value = config.job_project_id
    if requested_value and not requested_value.startswith("project-"):
        alias_map = config.projects or {}
        for alias, project_id in alias_map.items():
            if alias.lower() == requested_value.lower():
                requested_value = project_id
                break

    shared_groups = getattr(config, "project_shared_path_groups", None)
    if not isinstance(shared_groups, dict) or not shared_groups:
        shared_groups = None

    return browser_api_module.select_project(
        projects,
        requested_value,
        shared_path_group_by_id=shared_groups,
        project_order=config.project_order or None,
        congested_projects=congested or None,
    )


def _quota_display(quota: ResolvedQuota) -> str:
    if quota.gpu_count > 0:
        return f"{quota.gpu_count}x{quota.gpu_type or 'GPU'}"
    return f"{quota.cpu_count}xCPU"


def cache_created_job(
    config: Config,
    *,
    job_id: str,
    name: str,
    resource: str,
    command: str,
    log_path: str | None,
    project: str | None = None,
) -> None:
    cache = JobCache(config.get_expanded_cache_path())
    cache.add_job(
        job_id=job_id,
        name=name,
        resource=resource,
        command=command,
        status="PENDING",
        log_path=log_path,
        project=project,
    )


def submit_training_job(
    api,  # noqa: ANN001
    *,
    config: Config,
    name: str,
    command: str,
    quota: ResolvedQuota,
    framework: str,
    project_id: str,
    workspace_id: str,
    image: Optional[str],
    priority: int,
    nodes: int,
    max_time_hours: float,
    project_name: Optional[str] = None,
) -> JobSubmission:
    wrapped_command = wrap_in_bash(command)
    final_command, log_path = build_remote_logged_command(config, command=wrapped_command)

    max_time_ms = str(int(max_time_hours * 3600 * 1000))

    create_kwargs: dict[str, Any] = dict(
        name=name,
        command=final_command,
        framework=framework,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        task_priority=priority,
        instance_count=nodes,
        max_running_time_ms=max_time_ms,
        spec_id_override=quota.quota_id,
        compute_group_id_override=quota.logic_compute_group_id,
    )

    if config.shm_size is not None:
        shm_size = int(config.shm_size)
        if shm_size < 1:
            raise ValueError(
                "Shared memory size must be >= 1 (set INSPIRE_SHM_SIZE or job.shm_size)."
            )
        create_kwargs["shm_gi"] = shm_size

    result = api.create_training_job_smart(**create_kwargs)
    data = result.get("data", {}) if isinstance(result, dict) else {}
    job_id = data.get("job_id")

    if job_id:
        cache_created_job(
            config,
            job_id=job_id,
            name=name,
            resource=_quota_display(quota),
            command=wrapped_command,
            log_path=log_path,
            project=project_name,
        )

    return JobSubmission(
        job_id=job_id,
        data=data,
        result=result,
        log_path=log_path,
        wrapped_command=wrapped_command,
        max_time_ms=max_time_ms,
    )


__all__ = [
    "JobSubmission",
    "build_remote_logged_command",
    "cache_created_job",
    "select_project_for_workspace",
    "submit_training_job",
    "wrap_in_bash",
]
