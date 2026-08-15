"""`inspire resources usage` — who is holding the capacity right now.

`resources availability` answers "how much is left" and `resources nodes`
answers "how many whole nodes are idle". Neither answers "who took the rest",
which on a shared cluster is the question that decides whether to wait, ask, or
submit somewhere else. This reads the live per-workload dimension and rolls it
up by user, by project, or leaves it per task.

It takes one workspace and refuses `all`. Quota and scheduling are per
workspace, so every decision this feeds is too; and because the rollups below
bucket per workspace, a fanout would emit one row per workspace-and-user pair
while the shared Workspace column and truncation notice made it read as a
platform-wide ranking. "Who holds the most across the cluster" is not a
question this data can answer, so the flag that implies it is gone.

The rollups are computed here rather than requested from the platform on
purpose. `workspace.ListProjectDimension` answers an empty list to ordinary
members in every reachable workspace, and `workspace.ListUserDimension` returns
only the caller's own rows — asking for another member's id answers empty
rather than denying — so the task dimension is the only workspace-wide source.
`--mine` still reads the user dimension, because the caller's own footprint is
one pre-aggregated request there instead of a full sweep of the workspace.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import (
    resolve_workspace_operation_scope,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import MemberUsage, TaskUsage
from inspire.platform.web.session import SessionExpiredError, get_web_session

_REDACTED_ID_RE = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)")

_BY_CHOICES = ("user", "project", "task")


def _display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def _amount(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _percent(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


def _rollup(
    tasks: list[TaskUsage],
    *,
    by: str,
    workspace: str,
) -> list[dict[str, Any]]:
    """Fold live workloads into one row per user or per project."""
    buckets: dict[str, dict[str, Any]] = {}
    for task in tasks:
        key = task.user_name if by == "user" else task.project_name
        key = key or "(unknown)"
        bucket = buckets.setdefault(
            key,
            {
                "workspace": workspace,
                by: key,
                "gpus": 0,
                "cpus": 0.0,
                "memory_gib": 0.0,
                "tasks": 0,
                "_nodes": set(),
                "_peers": set(),
                "_gpu_busy": 0.0,
            },
        )
        bucket["gpus"] += task.gpus
        bucket["cpus"] += task.cpus
        bucket["memory_gib"] += task.memory_gib
        bucket["tasks"] += 1
        # A node shared by several of a user's tasks must be counted once, or
        # "nodes held" silently becomes "tasks running".
        bucket["_nodes"].update(task.node_names)
        peer = task.project_name if by == "user" else task.user_name
        if peer:
            bucket["_peers"].add(peer)
        bucket["_gpu_busy"] += task.gpus * task.gpu_usage_rate

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        gpus = bucket["gpus"]
        row = {
            "workspace": bucket["workspace"],
            by: bucket[by],
            "gpus": gpus,
            "cpus": round(bucket["cpus"], 1),
            "memory_gib": round(bucket["memory_gib"], 1),
            "nodes": len(bucket["_nodes"]),
            "tasks": bucket["tasks"],
            "projects" if by == "user" else "users": len(bucket["_peers"]),
        }
        if gpus > 0:
            row["gpu_usage_rate"] = round(bucket["_gpu_busy"] / gpus, 4)
        rows.append(row)

    rows.sort(
        key=lambda row: (row["gpus"], row["nodes"], row["cpus"]),
        reverse=True,
    )
    return rows


def _task_rows(tasks: list[TaskUsage], *, workspace: str) -> list[dict[str, Any]]:
    rows = [
        {
            "workspace": workspace,
            "task": _display_name(task.name),
            "type": task.task_type,
            "status": task.status,
            "user": _display_name(task.user_name),
            "project": _display_name(task.project_name, fallback=""),
            "gpus": task.gpus,
            "cpus": round(task.cpus, 1),
            "memory_gib": round(task.memory_gib, 1),
            "nodes": len(task.node_names),
            "gpu_usage_rate": round(task.gpu_usage_rate, 4) if task.gpus > 0 else None,
        }
        for task in tasks
    ]
    rows.sort(key=lambda row: (row["gpus"], row["nodes"], row["cpus"]), reverse=True)
    return rows


def _member_rows(usages: list[MemberUsage], *, workspace: str) -> list[dict[str, Any]]:
    rows = [
        {
            "workspace": workspace,
            "project": _display_name(usage.project_name),
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


def _render(
    rows: list[dict[str, Any]],
    *,
    columns: list[tuple[str, str, str]],
) -> str:
    """Render the projected rows using an explicit column allowlist."""
    table_rows: list[tuple[str, ...]] = []
    for row in rows:
        cells: list[str] = []
        for key, _header, _align in columns:
            value = row.get(key)
            if key.endswith("usage_rate"):
                cells.append(_percent(value))
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(_amount(float(value)))
            else:
                cells.append(_display_name(value, fallback="-"))
        table_rows.append(tuple(cells))

    headers = [header for _key, header, _align in columns]
    widths = [
        column_width(header, [row[index] for row in table_rows], max_width=28)
        for index, header in enumerate(headers)
    ]
    aligns = [align for _key, _header, align in columns]
    return "\n".join(
        render_table(tuple(headers), table_rows, widths, aligns=aligns, line_char="─")
    )


_COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "user": [
        ("user", "User", "left"),
        ("gpus", "GPUs", "right"),
        ("cpus", "CPUs", "right"),
        ("memory_gib", "Mem GiB", "right"),
        ("nodes", "Nodes", "right"),
        ("tasks", "Tasks", "right"),
        ("gpu_usage_rate", "GPU Busy", "right"),
    ],
    "project": [
        ("project", "Project", "left"),
        ("gpus", "GPUs", "right"),
        ("cpus", "CPUs", "right"),
        ("memory_gib", "Mem GiB", "right"),
        ("nodes", "Nodes", "right"),
        ("tasks", "Tasks", "right"),
        ("users", "Users", "right"),
    ],
    "task": [
        ("task", "Task", "left"),
        ("type", "Type", "left"),
        ("user", "User", "left"),
        ("gpus", "GPUs", "right"),
        ("nodes", "Nodes", "right"),
        ("gpu_usage_rate", "GPU Busy", "right"),
    ],
    "mine": [
        ("project", "Project", "left"),
        ("gpus", "GPUs", "right"),
        ("cpus", "CPUs", "right"),
        ("memory_gib", "Mem GiB", "right"),
        ("gpu_nodes", "GPU Nodes", "right"),
        ("cpu_nodes", "CPU Nodes", "right"),
        ("hpc_nodes", "HPC Nodes", "right"),
    ],
}


@click.command("usage")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--by",
    "by",
    type=click.Choice(_BY_CHOICES, case_sensitive=False),
    default=None,
    help=(
        "Roll live usage up by user or project, or list it per task "
        "[default: user]."
    ),
)
@click.option(
    "--mine",
    is_flag=True,
    help="Show only your own footprint, split by project (single request).",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum rows to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every row.")
@pass_context
def usage_resources(
    ctx: Context,
    workspace: str,
    by: Optional[str],
    mine: bool,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """Show who is currently holding a workspace's compute.

    \b
    Every row is a live allocation, so this is the counterpart to
    `resources availability`: that command reports what is left, this one
    reports where the rest went. `GPU Busy` is how much of the held GPU
    allocation is actually working — a large `GPUs` next to a low `GPU Busy`
    is capacity parked rather than used, which is what makes a request for it
    worth raising.

    \b
    `--mine` reads your own footprint directly and costs one request; the
    workspace-wide views sweep every running workload in the workspace.

    \b
    `--workspace` takes one name — `all` is refused. Quota, scheduling and the
    decision this command feeds (wait, ask, or submit somewhere else) are all
    per workspace, and the rollups are built per workspace too, so a fanout
    would rank workspace-and-user pairs while reading as a platform ranking.

    \b
    Examples:
        inspire resources usage --workspace 分布式训练空间
        inspire resources usage --workspace 分布式训练空间 --by project
        inspire resources usage --workspace 分布式训练空间 --by task -n 5
        inspire resources usage --workspace CPU资源空间 --mine
    """
    if mine and by:
        _handle_error(
            ctx,
            "ValidationError",
            "Use either --by or --mine, not both.",
            EXIT_VALIDATION_ERROR,
        )
        return

    mode = "mine" if mine else (by or "user").lower()

    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        session = get_web_session()
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
        label = _display_name(
            workspace_name_map(session).get(workspace_id) or workspace,
            fallback="(workspace name unavailable)",
        )

        rows: list[dict[str, Any]]
        if mode == "mine":
            rows = _member_rows(
                browser_api_module.list_member_usage(workspace_id, session=session),
                workspace=label,
            )
        else:
            tasks = browser_api_module.list_task_usage(workspace_id, session=session)
            rows = (
                _task_rows(tasks, workspace=label)
                if mode == "task"
                else _rollup(tasks, by=mode, workspace=label)
            )

        page = bound_collection(rows, limit=effective_limit)
        public_rows = page.items

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "by": mode,
                        "items": public_rows,
                        **page.metadata(),
                    }
                )
            )
            return

        if not rows:
            click.echo(
                "You are not holding any compute in this workspace."
                if mode == "mine"
                else "No live workloads in this workspace."
            )
            return

        click.echo(_render(public_rows, columns=_COLUMNS[mode]))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["usage_resources"]
