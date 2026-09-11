"""Shared helpers for submitting GPU jobs through the platform client."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inspire.services.job import job_submission as shared_submission
from inspire.services.job.job_submission import (
    JobSubmission,
    JobSubmissionPlan,
    wrap_in_bash,
    build_remote_command,
    parse_env_assignments,
    hours_to_ms_string,
    normalize_exclude_nodes,
    normalize_specified_nodes,
    training_plan_exclude_nodes,
    training_plan_specified_nodes,
)

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import ProjectInfo
from inspire.cli.utils.id_resolver import _looks_like_platform_id
from inspire.services.catalog.image_resolution import (
    ImageCatalogCache,
    resolve_image_url,
)
from inspire.config import (
    Config,
    ConfigError,
)
from inspire.cli.utils.quota_resolver import ResolvedQuota


ProjectSelectionCache = dict[str, tuple[list[ProjectInfo], set[str]]]


def select_project_for_workspace(
    config: Config,
    *,
    workspace_id: str,
    requested: str | None,
    session: Any = None,
    selection_cache: ProjectSelectionCache | None = None,
) -> tuple[ProjectInfo, str | None]:
    """Select a project for the given workspace, with quota-aware fallback."""
    requested_name = (requested or "").strip()
    if not requested_name:
        raise ConfigError("--project is required.")
    if _looks_like_platform_id(requested_name):
        raise ConfigError("--project only accepts a project name.")

    if session is None:
        try:
            session = web_session_module.get_web_session()
        except ValueError as e:
            raise ConfigError(str(e)) from e

    snapshot = selection_cache.get(workspace_id) if selection_cache is not None else None
    if snapshot is None:
        projects = browser_api_module.list_projects(
            workspace_id=workspace_id,
            session=session,
        )
        if not projects:
            raise ConfigError("No projects available")
        congested = browser_api_module.check_scheduling_health(
            workspace_id=workspace_id,
            project_ids={p.project_id for p in projects},
            session=session,
        )
        if selection_cache is not None:
            selection_cache[workspace_id] = (projects, congested)
    else:
        projects, congested = snapshot

    name_matches = [
        project for project in projects if project.name.casefold() == requested_name.casefold()
    ]
    if not name_matches:
        raise ValueError(f"Project name '{requested_name}' not found")
    if len(name_matches) > 1:
        raise ValueError(f"Project name '{requested_name}' is ambiguous")

    return browser_api_module.select_project(
        projects,
        name_matches[0].name,
        project_order=config.project_order or None,
        congested_projects=congested or None,
    )


def submit_training_job(
    *,
    session: Any,
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
    max_time_hours: Optional[float],
    project_name: Optional[str] = None,
    auto_fault_tolerance: Optional[bool] = None,
    fault_tolerance_max_retry: Optional[int] = None,
    enable_notification: bool = False,
    exclude_nodes: Iterable[str] | None = None,
    shm_size: Optional[int] = None,
    dataset_info: Optional[list[dict[str, str]]] = None,
    envs: Optional[list[dict[str, str]]] = None,
    description: Optional[str] = None,
    keep_after_success_hours: Optional[float] = None,
    keep_after_failure_hours: Optional[float] = None,
    public_path_readonly: Optional[bool] = None,
    fault_tolerance_retry_interval_sec: Optional[int] = None,
    specified_nodes: Iterable[str] | None = None,
) -> JobSubmission:
    plan = build_training_job_plan(
        config=config,
        name=name,
        command=command,
        quota=quota,
        framework=framework,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        priority=priority,
        nodes=nodes,
        max_time_hours=max_time_hours,
        project_name=project_name,
        auto_fault_tolerance=auto_fault_tolerance,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        enable_notification=enable_notification,
        exclude_nodes=exclude_nodes,
        specified_nodes=specified_nodes,
        shm_size=shm_size,
        dataset_info=dataset_info,
        envs=envs,
        description=description,
        keep_after_success_hours=keep_after_success_hours,
        keep_after_failure_hours=keep_after_failure_hours,
        public_path_readonly=public_path_readonly,
        fault_tolerance_retry_interval_sec=fault_tolerance_retry_interval_sec,
        session=session,
    )

    data = browser_api_module.create_training_job(
        payload=plan.create_kwargs,
        session=session,
    )
    result = {"code": 0, "data": data}
    job_id = data.get("job_id") or data.get("id")

    return JobSubmission(
        job_id=job_id,
        data=data,
        result=result,
        wrapped_command=plan.wrapped_command,
        max_time_ms=plan.max_time_ms,
    )


__all__ = [
    "JobSubmission",
    "JobSubmissionPlan",
    "build_training_job_plan",
    "build_remote_command",
    "hours_to_ms_string",
    "normalize_exclude_nodes",
    "normalize_specified_nodes",
    "parse_env_assignments",
    "select_project_for_workspace",
    "submit_training_job",
    "training_plan_exclude_nodes",
    "training_plan_specified_nodes",
    "wrap_in_bash",
]


def build_training_job_plan(
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
    max_time_hours: Optional[float],
    project_name: Optional[str] = None,
    auto_fault_tolerance: Optional[bool] = None,
    fault_tolerance_max_retry: Optional[int] = None,
    enable_notification: bool = False,
    exclude_nodes: Iterable[str] | None = None,
    shm_size: Optional[int] = None,
    dataset_info: Optional[list[dict[str, str]]] = None,
    envs: Optional[list[dict[str, str]]] = None,
    description: Optional[str] = None,
    keep_after_success_hours: Optional[float] = None,
    keep_after_failure_hours: Optional[float] = None,
    public_path_readonly: Optional[bool] = None,
    fault_tolerance_retry_interval_sec: Optional[int] = None,
    specified_nodes: Iterable[str] | None = None,
    session: Any = None,
    image_catalog_cache: ImageCatalogCache | None = None,
) -> JobSubmissionPlan:
    if image:
        image = resolve_image_url(image, session=session, workspace_id=workspace_id,
                                  catalog_cache=image_catalog_cache)
    return shared_submission.build_training_job_plan(
        config=config,
        name=name,
        command=command,
        quota=quota,
        framework=framework,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        priority=priority,
        nodes=nodes,
        max_time_hours=max_time_hours,
        project_name=project_name,
        auto_fault_tolerance=auto_fault_tolerance,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        enable_notification=enable_notification,
        exclude_nodes=exclude_nodes,
        shm_size=shm_size,
        dataset_info=dataset_info,
        envs=envs,
        description=description,
        keep_after_success_hours=keep_after_success_hours,
        keep_after_failure_hours=keep_after_failure_hours,
        public_path_readonly=public_path_readonly,
        fault_tolerance_retry_interval_sec=fault_tolerance_retry_interval_sec,
        specified_nodes=specified_nodes,
        session=session,
        image_catalog_cache=image_catalog_cache,
    )
