"""Shared helpers for submitting GPU jobs through the platform client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import ProjectInfo
from inspire.cli.utils.id_resolver import _looks_like_platform_id
from inspire.cli.utils.image_resolver import (
    IMAGE_TYPE,
    ImageCatalogCache,
    resolve_image_url,
)
from inspire.config import (
    Config,
    ConfigError,
    build_env_exports,
)
from inspire.cli.utils.quota_resolver import ResolvedQuota, build_resource_spec_price


ProjectSelectionCache = dict[str, tuple[list[ProjectInfo], set[str]]]


@dataclass(frozen=True)
class JobSubmission:
    job_id: Optional[str]
    data: dict
    result: Any
    wrapped_command: str
    max_time_ms: Optional[str]


@dataclass(frozen=True)
class JobSubmissionPlan:
    """Fully resolved local submission plan, before the create API call."""

    create_kwargs: dict[str, Any]
    wrapped_command: str
    max_time_ms: Optional[str]
    project_name: Optional[str]
    workspace_id: str
    quota: ResolvedQuota
    shm_size_gib: Optional[int] = None


def wrap_in_bash(command: str) -> str:
    """Wrap a command in bash -c unless already wrapped."""
    stripped = command.strip()

    if stripped.startswith(("bash -c ", "sh -c ", "/bin/bash -c ", "/bin/sh -c ")):
        return command

    escaped = command.replace("'", "'\\''")
    return f"bash -c '{escaped}'"


def build_remote_command(config: Config, *, command: str) -> str:
    """Prefix the explicit job command with configured account environment exports."""
    env_exports = build_env_exports(config.remote_env)
    return f"{env_exports}{command}" if env_exports else command


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


def parse_env_assignments(values: Iterable[str] | None) -> list[dict[str, str]]:
    """Parse repeated ``KEY=VALUE`` pairs into the platform's `envs` entries.

    The wire shape is ``{"name": ..., "value": ...}``; ``{"key": ...}`` is
    rejected by the create Action. An empty value is allowed (``KEY=``), a
    missing ``=`` is not, and a repeated key is a mistake worth reporting
    rather than silently resolving.
    """
    envs: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values or ():
        text = str(raw)
        name, separator, value = text.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"--env expects 'KEY=VALUE'; got {text!r}")
        if name in seen:
            raise ValueError(f"--env {name} was given more than once")
        seen.add(name)
        envs.append({"name": name, "value": value})
    return envs


def hours_to_ms_string(hours: Optional[float]) -> Optional[str]:
    """Convert an hours option to the string-typed millisecond field.

    ``max_running_time_ms`` and both ``reserve_on_*_ms`` fields are declared as
    strings; sending a number is rejected outright.
    """
    if hours is None:
        return None
    return str(int(hours * 3600 * 1000))


def _normalize_node_names(
    values: Iterable[str] | None,
    *,
    field: str,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_node in values or []:
        node = str(raw_node).strip()
        if not node:
            raise ValueError(f"{field} entries must be non-empty node names.")
        if node not in seen:
            normalized.append(node)
            seen.add(node)
    return normalized


def normalize_exclude_nodes(exclude_nodes: Iterable[str] | None) -> list[str]:
    """Normalize the Web UI's ``exclude_nodes`` create option."""
    return _normalize_node_names(exclude_nodes, field="exclude_nodes")


def normalize_specified_nodes(specified_nodes: Iterable[str] | None) -> list[str]:
    """Normalize the Web UI's ``specified_nodes`` create option."""
    return _normalize_node_names(specified_nodes, field="specified_nodes")


def training_plan_exclude_nodes(plan: JobSubmissionPlan) -> list[str]:
    """Return excluded node names from a training create plan."""
    nodes = plan.create_kwargs.get("exclude_nodes")
    if isinstance(nodes, list):
        return [str(node) for node in nodes]
    return []


def training_plan_specified_nodes(plan: JobSubmissionPlan) -> list[str]:
    """Return pinned node names from a training create plan."""
    nodes = plan.create_kwargs.get("specified_nodes")
    if isinstance(nodes, list):
        return [str(node) for node in nodes]
    return []


def _resolve_shm_size(config: Config, shm_size: Optional[int]) -> int | None:
    resolved = shm_size if shm_size is not None else config.shm_size
    if resolved is None:
        return None
    resolved_int = int(resolved)
    if resolved_int < 1:
        raise ValueError(
            "Shared memory size must be >= 1 "
            "(set --shm-size, INSPIRE_SHM_SIZE, or job.shm_size)."
        )
    return resolved_int


def _validate_shm_size_fits_memory(shm_size: int, memory_gib: int) -> None:
    memory_int = int(memory_gib)
    if shm_size > memory_int:
        raise ValueError(
            f"Shared memory size ({shm_size} GiB) must be <= quota memory "
            f"({memory_int} GiB). Lower --shm-size, INSPIRE_SHM_SIZE, or "
            "job.shm_size, or choose a quota with more memory."
        )


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
    if not image:
        raise ValueError("--image is required.")
    if nodes is None:
        raise ValueError("--nodes is required.")
    if int(nodes) < 1:
        raise ValueError("--nodes must be >= 1.")

    wrapped_command = wrap_in_bash(command)
    final_command = build_remote_command(config, command=wrapped_command)

    max_time_ms = hours_to_ms_string(max_time_hours)

    resource_spec_price = build_resource_spec_price(quota=quota)
    # The platform matches on the registry URL, not the visible name; sending
    # the name is rejected with 无法找到对应镜像.
    framework_config: dict[str, Any] = {
        "image_type": IMAGE_TYPE,
        "image": resolve_image_url(
            image,
            session=session,
            workspace_id=workspace_id,
            catalog_cache=image_catalog_cache,
        ),
        "instance_count": int(nodes),
        "resource_spec_price": resource_spec_price,
        "cpu": quota.cpu_count,
        "gpu_count": quota.gpu_count,
        "mem_gi": quota.memory_gib,
    }

    create_kwargs: dict[str, Any] = dict(
        name=name,
        command=final_command,
        framework=framework,
        project_id=project_id,
        workspace_id=workspace_id,
        logic_compute_group_id=quota.logic_compute_group_id,
        task_priority=priority,
        enable_notification=bool(enable_notification),
        framework_config=[framework_config],
    )

    if max_time_ms is not None:
        create_kwargs["max_running_time_ms"] = max_time_ms

    resolved_shm_size = _resolve_shm_size(config, shm_size)
    if resolved_shm_size is not None:
        _validate_shm_size_fits_memory(resolved_shm_size, quota.memory_gib)
        framework_config["shm_gi"] = resolved_shm_size

    normalized_exclude_nodes = normalize_exclude_nodes(exclude_nodes)
    normalized_specified_nodes = normalize_specified_nodes(specified_nodes)
    overlap = sorted(set(normalized_exclude_nodes) & set(normalized_specified_nodes))
    if overlap:
        raise ValueError(
            "The same node cannot be both specified and excluded: " + ", ".join(overlap)
        )
    if normalized_exclude_nodes:
        create_kwargs["exclude_nodes"] = normalized_exclude_nodes
    if normalized_specified_nodes:
        create_kwargs["specified_nodes"] = normalized_specified_nodes

    if auto_fault_tolerance is True:
        if fault_tolerance_max_retry is not None and fault_tolerance_max_retry < 1:
            raise ValueError(
                "fault_tolerance_max_retry must be >= 1 when auto_fault_tolerance is enabled"
            )
        create_kwargs["auto_fault_tolerance"] = True
        create_kwargs["fault_tolerance_max_retry"] = (
            fault_tolerance_max_retry if fault_tolerance_max_retry is not None else 10
        )
        if fault_tolerance_retry_interval_sec is not None:
            create_kwargs["fault_tolerance_retry_interval_sec"] = int(
                fault_tolerance_retry_interval_sec
            )
    elif fault_tolerance_retry_interval_sec is not None:
        raise ValueError(
            "--fault-tolerance-retry-interval only applies with --auto-fault-tolerance."
        )

    # Everything below stays out of the body unless it was asked for, so a
    # create built without these options is byte-for-byte the old request.
    if dataset_info:
        create_kwargs["dataset_info"] = [dict(entry) for entry in dataset_info]

    # `envs` entries are {name, value}; a `key` field is rejected by the proto.
    if envs:
        create_kwargs["envs"] = [dict(entry) for entry in envs]

    if description is not None:
        create_kwargs["description"] = description

    reserve_on_success_ms = hours_to_ms_string(keep_after_success_hours)
    if reserve_on_success_ms is not None:
        create_kwargs["reserve_on_success_ms"] = reserve_on_success_ms
    reserve_on_fail_ms = hours_to_ms_string(keep_after_failure_hours)
    if reserve_on_fail_ms is not None:
        create_kwargs["reserve_on_fail_ms"] = reserve_on_fail_ms

    if public_path_readonly is not None:
        create_kwargs["is_publicpath_readonly"] = bool(public_path_readonly)

    return JobSubmissionPlan(
        create_kwargs=create_kwargs,
        wrapped_command=wrapped_command,
        max_time_ms=max_time_ms,
        project_name=project_name,
        workspace_id=workspace_id,
        quota=quota,
        shm_size_gib=resolved_shm_size,
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
