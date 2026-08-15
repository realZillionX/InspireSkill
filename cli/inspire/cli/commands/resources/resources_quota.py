"""`inspire resources quota` — the workspace's own ceiling, not the cluster's.

`resources availability` answers "are there free nodes"; this answers "am I
allowed to take them". A submit lands in QUOTA_PENDING when the workspace has
already drawn its quota even though nodes sit idle, and that failure is
invisible in every other read the CLI has.

The ceiling is per priority band: high-priority (guaranteed) and low-priority
(reclaimable) work draw against separate allowances, so `--priority low` is a
different question from the default, not a filter on the same one.

Note this is the *workspace* ceiling shared by every member. The per-user and
per-task ceilings (`GetUserTaskQuota`, `GetWorkspaceTaskQuota`,
`GetDefaultUserTaskQuota`, `ListUserQuotas`) are workspace-admin only and
answer `AccessForbidden` to ordinary members.
"""

from __future__ import annotations

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
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_operation_scope,
    workspace_name_map,
)
from inspire.platform.web.browser_api.workspaces import (
    WorkspaceQuotaUsage,
    get_workspace_quota_usage,
)
from inspire.platform.web.session import SessionExpiredError, get_web_session

_RESOURCE_LABELS = {
    "gpu": "GPU",
    "cpu": "CPU",
    "memory_gib": "Memory (GiB)",
}


def _amount(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _public_row(
    usage: WorkspaceQuotaUsage,
    *,
    workspace: str,
    priority: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workspace": workspace,
        "priority": priority,
        "resource": usage.resource,
        "used": usage.used,
        "unlimited": usage.unlimited,
    }
    if not usage.unlimited:
        row["limit"] = usage.limit
        row["available"] = usage.available
    if usage.capacity is not None:
        row["capacity"] = usage.capacity
    if usage.capacity_used is not None:
        row["capacity_used"] = usage.capacity_used
    return row


def _format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No workspace quota reported."

    headers = ("Resource", "Quota Used", "Quota Limit", "Quota Free", "Cluster Used/Total")

    table_rows: list[tuple[str, ...]] = []
    for row in rows:
        capacity = (
            f"{_amount(row.get('capacity_used'))}/{_amount(row.get('capacity'))}"
            if row.get("capacity") is not None
            else "-"
        )
        table_rows.append(
            (
                _RESOURCE_LABELS.get(str(row["resource"]), str(row["resource"])),
                _amount(row.get("used")),
                "unlimited" if row.get("unlimited") else _amount(row.get("limit")),
                "-" if row.get("unlimited") else _amount(row.get("available")),
                capacity,
            )
        )

    widths = [
        column_width(header, [row[index] for row in table_rows], max_width=32)
        for index, header in enumerate(headers)
    ]
    rendered = render_table(
        headers,
        table_rows,
        widths,
        aligns=["left", "right", "right", "right", "right"],
        line_char="─",
    )
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


@click.command("quota")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--priority",
    type=click.Choice(["high", "low"], case_sensitive=False),
    default="high",
    show_default=True,
    help=(
        "Which allowance to report. High priority is the guaranteed band; low "
        "priority is reclaimable capacity that the scheduler can take back."
    ),
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
def quota_resources(
    ctx: Context,
    workspace: str,
    priority: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """Show a workspace's GPU / CPU / memory quota ceiling and current draw.

    \b
    Read this before submitting a large workload: `Quota Free` is how much the
    workspace may still take, while `Cluster Used/Total` is what the hardware
    physically has. A job can be refused by either one, and the two fail
    differently — a spent quota leaves the task in QUOTA_PENDING, while a busy
    cluster leaves it PENDING with a FailedScheduling event.

    \b
    Use `inspire resources availability` for free capacity per compute group,
    and `inspire <workload> quota` for the valid `gpu,cpu,mem` triples.

    \b
    Examples:
        inspire resources quota --workspace 分布式训练空间
        inspire --json resources quota --workspace 分布式训练空间 --priority low
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    priority_value = priority.lower()
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
        label = scrub_raw_ids(workspace_name_map(session).get(workspace_id) or workspace)

        rows = [
            _public_row(usage, workspace=label, priority=priority_value)
            for usage in get_workspace_quota_usage(
                workspace_id,
                session=session,
                priority=priority_value,
            )
        ]

        page = bound_collection(rows, limit=effective_limit)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "priority": priority_value,
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return

        click.echo(_format_rows(page.items))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["quota_resources"]
