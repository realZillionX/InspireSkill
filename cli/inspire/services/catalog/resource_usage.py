"""Resource usage filtering and task, member and project aggregation."""

from __future__ import annotations

from typing import Any, Callable, Optional
import re

from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import MemberUsage, TaskUsage
from inspire.platform.web.browser_api.workspaces import is_fair_scheduling_workspace
from inspire.task_priority import is_preemptible_task_priority


_REDACTED_ID_RE = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)")


def display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def fair_scheduling_or_unknown(session: Any, workspace_id: str) -> Optional[bool]:
    """The workspace's priority contract, or ``None`` when it cannot be read.

    Which values count as preemptible depends on the contract, so an
    unresolved one has to stay unresolved: guessing would label a holder's
    cards takeable when they are not. It must not take the report down with
    it either — every other column here is still answerable.
    """
    try:
        return is_fair_scheduling_workspace(session, workspace_id)
    except Exception:
        return None


def project_user_rows(
    tasks: list[TaskUsage],
    *,
    workspace: str,
    fair_scheduling: Optional[bool],
) -> list[dict[str, Any]]:
    """Attribute every live allocation to one project and one user."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for task in tasks:
        key = (task.project_name or "(unknown)", task.user_name or "(unknown)")
        bucket = buckets.setdefault(
            key,
            {
                "project": key[0],
                "user": key[1],
                "gpus": 0,
                "low_priority_gpus": 0,
                "cpus": 0.0,
                "memory_gib": 0.0,
                "nodes": set(),
                "tasks": 0,
                "_gpu_busy": 0.0,
            },
        )
        bucket["gpus"] += task.gpus
        if fair_scheduling is not None and is_preemptible_task_priority(
            task.priority,
            fair_scheduling=fair_scheduling,
        ):
            bucket["low_priority_gpus"] += task.gpus
        bucket["cpus"] += task.cpus
        bucket["memory_gib"] += task.memory_gib
        bucket["nodes"].update(task.node_names)
        bucket["tasks"] += 1
        bucket["_gpu_busy"] += task.gpus * task.gpu_usage_rate

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        gpus = bucket["gpus"]
        rows.append(
            {
                "workspace": workspace,
                "project": bucket["project"],
                "user": bucket["user"],
                "gpus": gpus,
                "low_priority_gpus": (
                    bucket["low_priority_gpus"] if fair_scheduling is not None else None
                ),
                "cpus": round(bucket["cpus"], 1),
                "memory_gib": round(bucket["memory_gib"], 1),
                "nodes": len(bucket["nodes"]),
                "tasks": bucket["tasks"],
                "gpu_usage_rate": round(bucket["_gpu_busy"] / gpus, 4) if gpus else None,
            }
        )
    rows.sort(key=lambda row: (row["gpus"], row["nodes"], row["cpus"]), reverse=True)
    return rows


def filter_tasks(
    tasks: list[TaskUsage],
    *,
    project: Optional[str],
    user: Optional[str],
    task: Optional[str],
) -> list[TaskUsage]:
    """Apply visible-name filters before any aggregate is computed."""
    needles = {
        "project": (project or "").casefold(),
        "user": (user or "").casefold(),
        "task": (task or "").casefold(),
    }
    return [
        item
        for item in tasks
        if (not needles["project"] or needles["project"] in item.project_name.casefold())
        and (not needles["user"] or needles["user"] in item.user_name.casefold())
        and (not needles["task"] or needles["task"] in item.name.casefold())
    ]


def resolve_group_ids(
    *,
    session: Any,
    workspace_id: str,
    keyword: str,
    groups_loader: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[tuple[str, str]]:
    """Match a compute-group keyword the way the sibling commands do.

    Substring, not exact: `--group H200` is one question about a hardware
    generation, and answering it for only one of the three groups that carry
    that hardware would be a different, quieter answer.
    """
    groups = (
        groups_loader()
        if groups_loader
        else browser_api_module.list_compute_groups(workspace_id=workspace_id, session=session)
    )
    needle = keyword.casefold()
    matched: list[tuple[str, str]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("logic_compute_group_id") or item.get("id") or "").strip()
        name = str(item.get("name") or item.get("logic_compute_group_name") or "").strip()
        if group_id and name and needle in name.casefold():
            matched.append((group_id, name))
    return matched


def task_rows(tasks: list[TaskUsage], *, workspace: str) -> list[dict[str, Any]]:
    rows = [
        {
            "workspace": workspace,
            "task": display_name(task.name),
            "type": task.task_type,
            "status": task.status,
            "user": display_name(task.user_name),
            "project": display_name(task.project_name, fallback=""),
            "gpus": task.gpus,
            "priority": task.priority or None,
            "cpus": round(task.cpus, 1),
            "memory_gib": round(task.memory_gib, 1),
            "nodes": len(task.node_names),
            "gpu_usage_rate": round(task.gpu_usage_rate, 4) if task.gpus > 0 else None,
        }
        for task in tasks
    ]
    rows.sort(key=lambda row: (row["gpus"], row["nodes"], row["cpus"]), reverse=True)
    return rows


def member_rows(usages: list[MemberUsage], *, workspace: str) -> list[dict[str, Any]]:
    rows = [
        {
            "workspace": workspace,
            "project": display_name(usage.project_name),
            "gpus": usage.gpus,
            "cpus": round(usage.cpus, 1),
            "memory_gib": round(usage.memory_gib, 1),
            "gpu_nodes": usage.gpu_nodes,
            "cpu_nodes": usage.cpu_nodes,
            "hpc_nodes": usage.hpc_nodes,
        }
        for usage in usages
    ]
    rows.sort(key=lambda row: (row["gpus"], row["cpus"]), reverse=True)
    return rows
