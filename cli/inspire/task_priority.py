"""Task-priority policy shared by platform reads and workload writes."""

from __future__ import annotations

from typing import Any

FAIR_PRIORITY_LOW = 1
FAIR_PRIORITY_HIGH = 4
FAIR_PRIORITIES = (FAIR_PRIORITY_LOW, FAIR_PRIORITY_HIGH)
STANDARD_PRIORITY_MIN = 1
STANDARD_PRIORITY_MAX = 10


class TaskPriorityError(ValueError):
    """Raised when a requested priority violates the workspace policy."""


def default_task_priority(*, fair_scheduling: bool) -> int:
    """Return the platform UI default for the selected workspace policy."""
    return FAIR_PRIORITY_HIGH if fair_scheduling else STANDARD_PRIORITY_MAX


def _project_priority_limit(value: Any, *, fair_scheduling: bool) -> int | None:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None

    if fair_scheduling:
        return FAIR_PRIORITY_LOW if limit < FAIR_PRIORITY_HIGH else FAIR_PRIORITY_HIGH
    if STANDARD_PRIORITY_MIN <= limit <= STANDARD_PRIORITY_MAX:
        return limit
    return None


def resolve_task_priority(
    requested: int | None,
    *,
    fair_scheduling: bool,
    project_limit: Any = None,
) -> int:
    """Validate, default, and optionally cap a task priority for one workspace."""
    priority = default_task_priority(fair_scheduling=fair_scheduling)
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise TaskPriorityError("Task priority must be an integer.")
        priority = requested

    if fair_scheduling:
        if priority not in FAIR_PRIORITIES:
            raise TaskPriorityError(
                "Fair-scheduling workspaces accept only priority 1=LOW or 4=HIGH."
            )
    elif not STANDARD_PRIORITY_MIN <= priority <= STANDARD_PRIORITY_MAX:
        raise TaskPriorityError("Task priority must be between 1 and 10.")

    limit = _project_priority_limit(project_limit, fair_scheduling=fair_scheduling)
    return min(priority, limit) if limit is not None else priority


def is_low_task_priority(value: Any) -> bool:
    """Classify task values while retaining support for historical 2/3 rows."""
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return False
    return FAIR_PRIORITY_LOW <= priority < FAIR_PRIORITY_HIGH


__all__ = [
    "FAIR_PRIORITIES",
    "STANDARD_PRIORITY_MAX",
    "STANDARD_PRIORITY_MIN",
    "TaskPriorityError",
    "default_task_priority",
    "is_low_task_priority",
    "resolve_task_priority",
]
