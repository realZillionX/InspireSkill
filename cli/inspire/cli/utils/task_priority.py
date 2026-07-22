"""CLI adapters for workspace-aware task priority."""

from __future__ import annotations

from typing import Any, Callable

import click

from inspire.task_priority import (
    STANDARD_PRIORITY_MAX,
    STANDARD_PRIORITY_MIN,
    TaskPriorityError,
    resolve_task_priority,
)

TASK_PRIORITY_HELP = (
    "Task priority. Fair-scheduling workspaces accept 1=LOW (preemptible) or "
    "4=HIGH (default: 4); other workspaces accept 1-10 (default: 10). "
    "The selected project's platform policy may cap the requested value."
)


def task_priority_option() -> Callable:
    """Return the shared Click option used by workload create commands."""
    return click.option(
        "--priority",
        type=click.IntRange(STANDARD_PRIORITY_MIN, STANDARD_PRIORITY_MAX),
        default=None,
        help=TASK_PRIORITY_HELP,
    )


def resolve_workspace_task_priority(
    requested: int | None,
    *,
    session: Any,
    workspace_id: str,
    project_limit: Any = None,
    project_id: str | None = None,
) -> int:
    """Resolve priority from the selected workspace's live scheduling capability."""
    from inspire.platform.web.browser_api.workspaces import is_fair_scheduling_workspace

    fair_scheduling = is_fair_scheduling_workspace(session, workspace_id)
    if fair_scheduling and project_limit is None and project_id:
        from inspire.platform.web.browser_api.projects import list_projects

        project_limit = next(
            (
                project.priority_name
                for project in list_projects(workspace_id=workspace_id, session=session)
                if project.project_id == project_id
            ),
            None,
        )
        if project_limit is None:
            raise TaskPriorityError(
                "Could not resolve the selected project's fair-scheduling priority limit."
            )

    return resolve_task_priority(
        requested,
        fair_scheduling=fair_scheduling,
        project_limit=project_limit,
    )


__all__ = [
    "TASK_PRIORITY_HELP",
    "TaskPriorityError",
    "resolve_task_priority",
    "resolve_workspace_task_priority",
    "task_priority_option",
]
