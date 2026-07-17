"""Shared helpers for submitting GPU jobs through the platform client."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import ProjectInfo
from inspire.config import Config, ConfigError, build_env_exports, default_remote_cwd
from inspire.cli.utils.quota_resolver import ResolvedQuota, build_resource_spec_price


@dataclass(frozen=True)
class JobSubmission:
    job_id: Optional[str]
    data: dict
    result: Any
    log_path: Optional[str]
    wrapped_command: str
    max_time_ms: Optional[str]


@dataclass(frozen=True)
class JobSubmissionPlan:
    """Fully resolved local submission plan, before the create API call."""

    create_kwargs: dict[str, Any]
    log_path: Optional[str]
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


_NAME_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_job_name_for_filename(name: str) -> str:
    """Project a job name onto a filesystem-safe filename fragment.

    Job names are tame in practice (alnum + ``-`` / ``_``), but a stray
    slash or shell metacharacter would break the `command > path` redirect
    or the corresponding `inspire job logs` lookup. Replace anything
    outside ``A-Za-z0-9._-`` with ``_``.
    """
    return _NAME_FILENAME_RE.sub("_", (name or "").strip()) or "job"


def _now_log_timestamp() -> str:
    """ISO-ish timestamp suffix used in deterministic log filenames.

    UTC + ``%Y%m%dT%H%M%SZ`` so the suffix is filesystem-safe and sortable
    by ``ls -1t`` (which sorts on mtime, but the lexicographic order of
    these timestamps matches mtime ordering too — useful for tools that
    fall back to lexicographic sorting).
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def derive_remote_log_glob(config: Config, *, name: str) -> str | None:
    """Glob pattern matching every log file written by jobs with this NAME.

    ``inspire job logs <name>`` resolves it via SSH (`ls -1t <pattern> |
    head -1`) to find the most recent run. Returns ``None`` when no default
    path alias is configured (no shared-FS log redirect).

    Naming convention: ``<remote_cwd>/.inspire/training_master_<safe>_*.log``
    where ``<safe>`` is the sanitized job name and ``*`` is a UTC timestamp
    that ``submit_training_job`` writes per submission. Re-submitting the
    same NAME produces a new log file rather than clobbering the previous
    run's output.
    """
    remote_cwd = default_remote_cwd(config.path_aliases)
    if not remote_cwd:
        return None
    safe = sanitize_job_name_for_filename(name)
    return os.path.join(remote_cwd, ".inspire", f"training_master_{safe}_*.log")


def build_remote_logged_command(
    config: Config, *, command: str, name: str
) -> tuple[str, str | None]:
    """Build the remote command (with optional logging) and return (final_command, log_path).

    The concrete log path uses a per-submission UTC timestamp so two jobs
    with the same name (e.g. delete-and-recreate iteration) write to
    distinct files. ``derive_remote_log_glob`` recovers the matching
    pattern at lookup time.
    """
    env_exports = build_env_exports(config.remote_env)
    final_command = f"{env_exports}{command}" if env_exports else command

    log_path: str | None = None
    remote_cwd = default_remote_cwd(config.path_aliases)
    if remote_cwd:
        remote_env = dict(config.remote_env)
        remote_env.setdefault("PYTHONUNBUFFERED", "1")
        env_exports = build_env_exports(remote_env)
        safe = sanitize_job_name_for_filename(name)
        log_dir = os.path.join(remote_cwd, ".inspire")
        log_path = os.path.join(log_dir, f"training_master_{safe}_{_now_log_timestamp()}.log")
        quoted_log_path = shlex.quote(log_path)
        stdout_tee = f"tee -a {quoted_log_path}"
        stderr_tee = f"tee -a {quoted_log_path} >&2"
        script = (
            f"{env_exports}"
            f"mkdir -p {shlex.quote(log_dir)} && "
            f": > {quoted_log_path} && "
            f"cd {shlex.quote(remote_cwd)} && "
            f"{{ {command} 2> >({stderr_tee}); }} | {stdout_tee}"
        )
        final_command = f"bash -o pipefail -c {shlex.quote(script)}"

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
    if not requested_value:
        raise ConfigError("--project is required.")
    if requested_value and not requested_value.startswith("project-"):
        alias_map = config.projects or {}
        for alias, project_id in alias_map.items():
            if alias.lower() == requested_value.lower():
                requested_value = project_id
                break

    return browser_api_module.select_project(
        projects,
        requested_value,
        project_order=config.project_order or None,
        congested_projects=congested or None,
    )


def _quota_display(quota: ResolvedQuota) -> str:
    if quota.gpu_count > 0:
        return f"{quota.gpu_count}x{quota.gpu_type or 'GPU'}"
    return f"{quota.cpu_count}xCPU"


def normalize_exclude_nodes(exclude_nodes: Iterable[str] | None) -> list[str]:
    """Normalize the Web UI's exclude_nodes create option."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_node in exclude_nodes or []:
        node = str(raw_node).strip()
        if not node:
            raise ValueError("exclude_nodes entries must be non-empty node names.")
        if node not in seen:
            normalized.append(node)
            seen.add(node)
    return normalized


def training_plan_exclude_nodes(plan: JobSubmissionPlan) -> list[str]:
    """Return excluded node names from a training create plan."""
    nodes = plan.create_kwargs.get("exclude_nodes")
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
) -> JobSubmissionPlan:
    if not image:
        raise ValueError("--image is required.")
    if nodes is None:
        raise ValueError("--nodes is required.")
    if int(nodes) < 1:
        raise ValueError("--nodes must be >= 1.")

    wrapped_command = wrap_in_bash(command)
    final_command, log_path = build_remote_logged_command(
        config, command=wrapped_command, name=name
    )

    max_time_ms = str(int(max_time_hours * 3600 * 1000)) if max_time_hours is not None else None

    resource_spec_price = build_resource_spec_price(quota=quota)
    framework_config: dict[str, Any] = {
        "image_type": "SOURCE_PRIVATE",
        "image": image,
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
    if normalized_exclude_nodes:
        create_kwargs["exclude_nodes"] = normalized_exclude_nodes

    if auto_fault_tolerance is True:
        if fault_tolerance_max_retry is not None and fault_tolerance_max_retry < 1:
            raise ValueError(
                "fault_tolerance_max_retry must be >= 1 when auto_fault_tolerance is enabled"
            )
        create_kwargs["auto_fault_tolerance"] = True
        create_kwargs["fault_tolerance_max_retry"] = (
            fault_tolerance_max_retry if fault_tolerance_max_retry is not None else 10
        )

    return JobSubmissionPlan(
        create_kwargs=create_kwargs,
        log_path=log_path,
        wrapped_command=wrapped_command,
        max_time_ms=max_time_ms,
        project_name=project_name,
        workspace_id=workspace_id,
        quota=quota,
        shm_size_gib=resolved_shm_size,
    )


def training_plan_payload(plan: JobSubmissionPlan) -> dict[str, Any]:
    """Return a JSON-friendly dry-run payload for scripts."""
    return {
        "dry_run": True,
        "kind": "training",
        "create_kwargs": dict(plan.create_kwargs),
        "project_name": plan.project_name,
        "workspace_id": plan.workspace_id,
        "quota": {
            "quota_id": plan.quota.quota_id,
            "logic_compute_group_id": plan.quota.logic_compute_group_id,
            "compute_group_name": plan.quota.compute_group_name,
            "gpu_count": plan.quota.gpu_count,
            "gpu_type": plan.quota.gpu_type,
            "cpu_count": plan.quota.cpu_count,
            "memory_gib": plan.quota.memory_gib,
        },
        "shm_size_gib": plan.shm_size_gib,
        "wrapped_command": plan.wrapped_command,
        "log_path": plan.log_path,
        "max_time_ms": plan.max_time_ms,
    }


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
        shm_size=shm_size,
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
        log_path=plan.log_path,
        wrapped_command=plan.wrapped_command,
        max_time_ms=plan.max_time_ms,
    )


__all__ = [
    "JobSubmission",
    "JobSubmissionPlan",
    "build_training_job_plan",
    "build_remote_logged_command",
    "derive_remote_log_glob",
    "normalize_exclude_nodes",
    "sanitize_job_name_for_filename",
    "select_project_for_workspace",
    "submit_training_job",
    "training_plan_exclude_nodes",
    "training_plan_payload",
    "wrap_in_bash",
]
