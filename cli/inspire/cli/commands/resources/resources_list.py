"""Resources list command (availability)."""

from __future__ import annotations

import re
from typing import Optional

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
from inspire.cli.formatters.table import render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import (
    AuthenticationError,
    SessionExpiredError,
    get_web_session,
)

_REDACTED_ID_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)"
)


def _resolve_workspace_scope(
    *,
    session,
    workspace: Optional[str],
) -> tuple[str, dict[str, str]]:
    workspace_names = dict(session.all_workspace_names or {})
    workspace_id = resolve_workspace_operation_scope(
        workspace=workspace,
        session=session,
    )
    return workspace_id, workspace_names


def _ordered_availability(availability: list) -> list:  # noqa: ANN401
    """Return the same decision order for Human and JSON output.

    GPU and CPU rows render as separate table sections.  Sorting only inside
    the Human formatter made JSON use platform enumeration order and, more
    importantly, applied the default output limit before ranking capacity.
    """
    gpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "gpu"]
    cpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "cpu"]
    gpu_rows.sort(
        # Workload defaults are high priority, so rank by the capacity they can
        # actually obtain after preemption; use the guarantee balance only as
        # the tiebreaker.
        key=lambda item: (item.high_priority_available_gpus, item.available_gpus),
        reverse=True,
    )
    cpu_rows.sort(key=lambda item: item.cpu_available, reverse=True)
    return [*gpu_rows, *cpu_rows]


def _format_metric(value: float | int) -> str:
    numeric = float(value)
    if abs(numeric - round(numeric)) < 1e-6:
        return str(int(round(numeric)))
    return f"{numeric:.1f}"


def _public_metric(value: float | int) -> float:
    """Remove binary floating-point noise without hiding useful precision."""
    return round(float(value), 4)


def _display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def _public_availability_row(availability) -> dict[str, object]:  # noqa: ANN001
    row: dict[str, object] = {
        "workspace": _display_name(
            getattr(availability, "workspace_name", ""),
            fallback="",
        ),
        "compute_group": _display_name(getattr(availability, "group_name", "")),
        "kind": getattr(availability, "resource_kind", "gpu") or "gpu",
    }
    if row["kind"] == "cpu":
        row.update(
            {
                "cpu_total": _public_metric(availability.cpu_total),
                "cpu_used": _public_metric(availability.cpu_used),
                "cpu_available": _public_metric(availability.cpu_available),
                "memory_total_gib": _public_metric(availability.memory_total_gib),
                "memory_used_gib": _public_metric(availability.memory_used_gib),
                "memory_available_gib": _public_metric(
                    availability.memory_available_gib
                ),
            }
        )
        return row

    row.update(
        {
            "gpu_type": _display_name(getattr(availability, "gpu_type", "")),
            "total_gpus": availability.total_gpus,
            "used_gpus": availability.used_gpus,
            "available_gpus": availability.available_gpus,
            "high_priority_available_gpus": availability.high_priority_available_gpus,
            "low_priority_gpus": availability.low_priority_gpus,
            "total_nodes": availability.total_nodes,
            "ready_nodes": availability.ready_nodes,
            "free_nodes": availability.free_nodes,
            "gpus_per_node": availability.gpu_per_node,
        }
    )
    return row


def _format_accurate_availability_table(availability, *, include_cpu: bool) -> None:
    gpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "gpu"]
    cpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "cpu"]

    sections: list[str] = []

    if gpu_rows:
        gpu_table_rows = [
            (
                _display_name(row.group_name),
                _display_name(row.gpu_type),
                row.available_gpus,
                row.high_priority_available_gpus,
                row.used_gpus,
                row.total_gpus,
                row.free_nodes,
            )
            for row in gpu_rows
        ]
        sections.append(
            "\n".join(
                render_table(
                    (
                        "Compute Group",
                        "GPU",
                        "Available",
                        "High Pri",
                        "Used",
                        "Total",
                        "Free Nodes",
                    ),
                    gpu_table_rows,
                    [25, 18, 10, 12, 8, 8, 10],
                    aligns=["left", "left", "right", "right", "right", "right", "right"],
                    line_char="─",
                )
            )
        )

    if include_cpu and cpu_rows:
        cpu_table_rows = [
            (
                _display_name(row.group_name),
                _format_metric(row.cpu_available),
                _format_metric(row.cpu_used),
                _format_metric(row.cpu_total),
                f"{_format_metric(row.memory_available_gib)} GiB",
                f"{_format_metric(row.memory_used_gib)} GiB",
                f"{_format_metric(row.memory_total_gib)} GiB",
            )
            for row in cpu_rows
        ]
        sections.append(
            "\n".join(
                render_table(
                    (
                        "Compute Group",
                        "CPU Available",
                        "CPU Used",
                        "CPU Total",
                        "Memory Available",
                        "Memory Used",
                        "Memory Total",
                    ),
                    cpu_table_rows,
                    [25, 12, 10, 10, 14, 12, 12],
                    aligns=["left", "right", "right", "right", "right", "right", "right"],
                    line_char="─",
                )
            )
        )

    click.echo("\n\n".join(sections))


def _list_accurate_resources(
    ctx: Context,
    *,
    workspace: Optional[str],
    group: Optional[str],
    limit: Optional[int],
    include_cpu: bool,
) -> None:
    """List accurate compute-group availability using browser API."""
    try:
        Config.from_files_and_env(require_credentials=False)

        session = get_web_session()
        workspace_id, workspace_names = _resolve_workspace_scope(
            session=session,
            workspace=workspace,
        )

        availability = browser_api_module.get_accurate_resource_availability(
            workspace_id=workspace_id,
            session=session,
            include_cpu=include_cpu,
        )

        group_filter = (group or "").strip().lower()
        if group_filter:
            availability = [
                a for a in availability if group_filter in str(a.group_name or "").lower()
            ]
        availability = _ordered_availability(availability)
        page = bound_collection(availability, limit=limit)
        availability = page.items
        for entry in availability:
            if not entry.workspace_name:
                entry.workspace_name = workspace_names.get(entry.workspace_id, entry.workspace_name)

        if not availability:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"items": []}))
            else:
                click.echo("No compute resources found.")
            return

        if ctx.json_output:
            output = [_public_availability_row(entry) for entry in availability]
            click.echo(
                json_formatter.format_json(
                    {
                        "items": output,
                        **page.metadata(),
                    }
                )
            )
        else:
            _format_accurate_availability_table(availability, include_cpu=include_cpu)
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (AuthenticationError, SessionExpiredError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def run_resources_list(
    ctx: Context,
    *,
    workspace: str,
    group: Optional[str],
    limit: Optional[int],
    include_cpu: bool,
) -> None:
    if group:
        group = reject_id_at_boundary(
            ctx,
            group,
            resource_type="compute group",
            list_command=f"inspire resources availability --workspace {workspace}",
        )
    _list_accurate_resources(
        ctx,
        workspace=workspace,
        group=group,
        limit=limit,
        include_cpu=include_cpu,
    )


@click.command("availability")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--group",
    default=None,
    metavar="NAME",
    help=(
        "Filter by compute group name keyword/substring; full name is not "
        "required. Use this to find the exact compute group name required by "
        "workload create --group."
    ),
)
@click.option(
    "--include-cpu",
    is_flag=True,
    help="Include CPU-only compute groups with CPU and memory totals",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum compute groups to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching compute group.")
@pass_context
def availability_resources(
    ctx: Context,
    workspace: str,
    group: Optional[str],
    include_cpu: bool,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List compute-group availability.

    Requires one --workspace <name> and shows real-time GPU usage. `Available`
    is the workspace's guarantee balance and can be negative when usage is
    over guarantee; `High Pri` adds GPUs held by preemptible tasks and can
    still be negative. `Free Nodes` is the physical idle-node signal.
    Availability is defined per workspace, so `all` is rejected here.
    Use --include-cpu to include CPU-only compute groups and CPU/memory totals.

    \b
    Examples:
        inspire resources availability --workspace 分布式训练空间
        inspire resources availability --workspace CPU资源空间 --include-cpu
        inspire resources availability --workspace 分布式训练空间 --group H200
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    run_resources_list(
        ctx,
        workspace=workspace,
        group=group,
        limit=effective_limit,
        include_cpu=include_cpu,
    )
