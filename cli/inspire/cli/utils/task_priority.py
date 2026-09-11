"""CLI adapters for workspace-aware task priority."""

from __future__ import annotations

from typing import Callable

import click

from inspire.services.catalog.task_priority import (
    resolve_workspace_task_priority as resolve_workspace_task_priority,
)

from inspire.task_priority import (
    STANDARD_PRIORITY_MAX,
    STANDARD_PRIORITY_MIN,
    TaskPriorityError,
    resolve_task_priority,
)

TASK_PRIORITY_HELP = (
    "Task priority. Fair-scheduling workspaces accept 1=LOW (preemptible) or "
    "4=HIGH (default: 4); other workspaces accept 1-10 (default: 10). "
    "The selected project's platform policy may cap the requested value, and "
    "individual quota rows may accept LOW only -- see the Priority column of "
    "this workload's `quota` command."
)


def task_priority_option() -> Callable:
    """Return the shared Click option used by workload create commands."""
    return click.option(
        "--priority",
        type=click.IntRange(STANDARD_PRIORITY_MIN, STANDARD_PRIORITY_MAX),
        default=None,
        help=TASK_PRIORITY_HELP,
    )


__all__ = [
    "TASK_PRIORITY_HELP",
    "TaskPriorityError",
    "resolve_task_priority",
    "resolve_workspace_task_priority",
    "task_priority_option",
]
