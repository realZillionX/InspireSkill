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
from inspire.config.workspaces import resolve_workspace_query_scope
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

_REDACTED_ID_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)"
)


def _resolve_workspace_scope(
    *,
    config: Optional[Config],
    session,
    workspace: Optional[str],
) -> tuple[list[str], dict[str, str], bool]:
    workspace_names = dict(session.all_workspace_names or {})
    if config is None:
        raise ConfigError("Workspace selection requires a loaded config.")
    workspace_ids, all_workspaces = resolve_workspace_query_scope(
        workspace=workspace,
        session=session,
    )
    return workspace_ids, workspace_names, all_workspaces


def _format_metric(value: float | int) -> str:
    numeric = float(value)
    if abs(numeric - round(numeric)) < 1e-6:
        return str(int(round(numeric)))
    return f"{numeric:.1f}"


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
                "cpu_total": availability.cpu_total,
                "cpu_used": availability.cpu_used,
                "cpu_available": availability.cpu_available,
                "memory_total_gib": availability.memory_total_gib,
                "memory_used_gib": availability.memory_used_gib,
                "memory_available_gib": availability.memory_available_gib,
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
    workspace_names = {
        _display_name(getattr(a, "workspace_name", ""), fallback="")
        for a in availability
    }
    show_workspace = len(workspace_names - {""}) > 1

    sections: list[str] = []

    if gpu_rows:
        gpu_widths = [16, 25, 18, 10, 12, 8, 8, 10] if show_workspace else [
            25,
            18,
            10,
            12,
            8,
            8,
            10,
        ]
        gpu_headers = (
            (
                "Workspace",
                "Compute Group",
                "GPU",
                "Available",
                "Reclaimable",
                "Used",
                "Total",
                "Free Nodes",
            )
            if show_workspace
            else (
                "Compute Group",
                "GPU",
                "Available",
                "Reclaimable",
                "Used",
                "Total",
                "Free Nodes",
            )
        )
        gpu_aligns = (
            ["left", "left", "left", "right", "right", "right", "right", "right"]
            if show_workspace
            else ["left", "left", "right", "right", "right", "right", "right"]
        )
        gpu_table_rows: list[tuple[object, ...]] = []

        sorted_gpu_rows = sorted(
            gpu_rows,
            key=lambda x: (x.available_gpus, x.high_priority_available_gpus),
            reverse=True,
        )
        for row in sorted_gpu_rows:
            if show_workspace:
                gpu_table_rows.append(
                    (
                        _display_name(row.workspace_name, fallback=""),
                        _display_name(row.group_name),
                        _display_name(row.gpu_type),
                        row.available_gpus,
                        row.high_priority_available_gpus,
                        row.used_gpus,
                        row.total_gpus,
                        row.free_nodes,
                    )
                )
            else:
                gpu_table_rows.append(
                    (
                        _display_name(row.group_name),
                        _display_name(row.gpu_type),
                        row.available_gpus,
                        row.high_priority_available_gpus,
                        row.used_gpus,
                        row.total_gpus,
                        row.free_nodes,
                    )
                )
        sections.append(
            "\n".join(
                render_table(
                    gpu_headers,
                    gpu_table_rows,
                    gpu_widths,
                    aligns=gpu_aligns,
                    line_char="─",
                )
            )
        )

    if include_cpu and cpu_rows:
        cpu_widths = (
            [16, 25, 12, 10, 10, 14, 12, 12]
            if show_workspace
            else [25, 12, 10, 10, 14, 12, 12]
        )
        cpu_headers = (
            (
                "Workspace",
                "Compute Group",
                "CPU Available",
                "CPU Used",
                "CPU Total",
                "Memory Available",
                "Memory Used",
                "Memory Total",
            )
            if show_workspace
            else (
                "Compute Group",
                "CPU Available",
                "CPU Used",
                "CPU Total",
                "Memory Available",
                "Memory Used",
                "Memory Total",
            )
        )
        cpu_aligns = (
            ["left", "left", "right", "right", "right", "right", "right", "right"]
            if show_workspace
            else ["left", "right", "right", "right", "right", "right", "right"]
        )
        cpu_table_rows: list[tuple[object, ...]] = []

        for row in sorted(cpu_rows, key=lambda item: item.cpu_available, reverse=True):
            values = (
                _display_name(row.group_name),
                _format_metric(row.cpu_available),
                _format_metric(row.cpu_used),
                _format_metric(row.cpu_total),
                f"{_format_metric(row.memory_available_gib)} GiB",
                f"{_format_metric(row.memory_used_gib)} GiB",
                f"{_format_metric(row.memory_total_gib)} GiB",
            )
            if show_workspace:
                cpu_table_rows.append(
                    (_display_name(row.workspace_name, fallback=""), *values)
                )
            else:
                cpu_table_rows.append(values)
        sections.append(
            "\n".join(
                render_table(
                    cpu_headers,
                    cpu_table_rows,
                    cpu_widths,
                    aligns=cpu_aligns,
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
        config = None
        try:
            config, _ = Config.from_files_and_env(
                require_credentials=False
            )
        except Exception:
            config = None

        session = get_web_session()
        workspace_ids, workspace_names, explicit_workspace_selected = _resolve_workspace_scope(
            config=config,
            session=session,
            workspace=workspace,
        )
        target_workspace_id = workspace_ids[0] if not explicit_workspace_selected else None

        availability = browser_api_module.get_accurate_resource_availability(
            workspace_id=target_workspace_id,
            session=session,
            include_cpu=include_cpu,
            all_workspaces=explicit_workspace_selected,
        )

        group_filter = (group or "").strip().lower()
        if group_filter:
            availability = [
                a for a in availability if group_filter in str(a.group_name or "").lower()
            ]
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
    except (SessionExpiredError, ValueError) as e:
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
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--group",
    default=None,
    metavar="NAME",
    help=(
        "Filter by compute group name keyword/substring; full name is not "
        "required. Use this to find the exact compute group name required by "
        "workload create/profile --group."
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

    Requires --workspace <name|all> and shows real-time GPU usage.
    Use --include-cpu to include CPU-only compute groups and CPU/memory totals.

    \b
    Examples:
        inspire resources availability --workspace 分布式训练空间
        inspire resources availability --workspace all --include-cpu
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
