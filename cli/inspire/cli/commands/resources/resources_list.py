"""Resources list command (availability)."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.table import render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_query_scope
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.resources import (
    KNOWN_COMPUTE_GROUPS,
    clear_availability_cache,
    fetch_resource_availability,
)
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _known_compute_groups_from_config(*, show_all: bool) -> dict[str, str]:
    known_groups = KNOWN_COMPUTE_GROUPS
    if show_all:
        return known_groups

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.compute_groups:
            groups_tuple = load_compute_groups_from_config(config.compute_groups)
            return compute_group_name_map(groups_tuple)
    except Exception:
        return known_groups
    return known_groups


def _workspace_name_map(
    *,
    config: Optional[Config],
    session,
) -> dict[str, str]:
    del config
    return dict(session.all_workspace_names or {})


def _resolve_workspace_scope(
    *,
    config: Optional[Config],
    session,
    workspace: Optional[str],
) -> tuple[list[str], dict[str, str], bool]:
    workspace_names = _workspace_name_map(config=config, session=session)
    if config is None:
        raise ConfigError("Workspace selection requires a loaded config.")
    workspace_ids, all_workspaces = resolve_workspace_query_scope(
        config,
        workspace=workspace,
        session=session,
    )
    return workspace_ids, workspace_names, all_workspaces


def _format_metric(value: float | int) -> str:
    numeric = float(value)
    if abs(numeric - round(numeric)) < 1e-6:
        return str(int(round(numeric)))
    return f"{numeric:.1f}"


def _format_availability_table(availability, workspace_mode: bool = False) -> None:
    title = "📊 GPU Availability (Workspace)" if workspace_mode else "📊 GPU Availability (Live)"
    scope_note = "Shows availability in your workspace only" if workspace_mode else ""

    lines = [
        "",
        title,
        "─" * 80,
    ]

    if scope_note:
        lines.append(f"{scope_note}")
        lines.append("─" * 80)

    rows = []
    for a in availability:
        free_gpus = a.free_gpus
        if free_gpus >= 8:
            status = ""
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        rows.append(
            (
                a.gpu_type,
                a.group_name,
                a.ready_nodes,
                a.free_nodes,
                free_gpus,
                status,
            )
        )

    lines.extend(
        render_table(
            ("GPU Type", "Location", "Ready", "Free", "Free GPUs", ""),
            rows,
            [12, 25, 8, 8, 12, 2],
            aligns=["left", "left", "right", "right", "right", "left"],
            line_char="─",
        )
    )
    lines.append("")
    lines.append("💡 Usage:")
    lines.append(
        "  inspire job quota --workspace <workspace> --group H100   # Filter to find full group names"
    )
    lines.append(
        '  inspire job create -n train --workspace <workspace> --project <project> --group H100-2号机房 -q 4,80,800 -c "python train.py"'
    )
    lines.append("  # create/profile --group requires the full compute group name")
    lines.append("")

    click.echo("\n".join(lines))


def _format_accurate_availability_table(availability, *, include_cpu: bool) -> None:
    gpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "gpu"]
    cpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "cpu"]
    workspace_names = {
        str(getattr(a, "workspace_name", "") or getattr(a, "workspace_id", ""))
        for a in availability
    }
    show_workspace = len(workspace_names - {""}) > 1

    lines = ["", "📊 Compute Group Availability (Accurate Real-Time)"]

    if gpu_rows:
        widths = [16, 22, 25, 10, 8, 8, 8, 2] if show_workspace else [22, 25, 10, 8, 8, 8, 2]
        headers = (
            ("Workspace", "GPU Type", "Compute Group", "Available", "Used", "Low Pri", "Total", "")
            if show_workspace
            else ("GPU Type", "Compute Group", "Available", "Used", "Low Pri", "Total", "")
        )
        aligns = (
            ["left", "left", "left", "right", "right", "right", "right", "left"]
            if show_workspace
            else ["left", "left", "right", "right", "right", "right", "left"]
        )
        gpu_table_rows: list[tuple[object, ...]] = []
        if show_workspace:
            total_row: tuple[object, ...] = ("TOTAL", "", "", 0, 0, 0, 0, "")
        else:
            total_row = ("TOTAL", "", 0, 0, 0, 0, "")

        sorted_gpu_rows = sorted(gpu_rows, key=lambda x: x.available_gpus, reverse=True)
        total_available = 0
        total_used = 0
        total_low_pri = 0
        total_gpus = 0

        for row in sorted_gpu_rows:
            available = row.available_gpus
            if available >= 100:
                status = "✓"
            elif available >= 32:
                status = "○"
            elif available >= 8:
                status = "◐"
            elif available > 0:
                status = "⚠"
            else:
                status = "✗"

            if show_workspace:
                gpu_table_rows.append(
                    (
                        row.workspace_name,
                        row.gpu_type,
                        row.group_name,
                        row.available_gpus,
                        row.used_gpus,
                        row.low_priority_gpus,
                        row.total_gpus,
                        status,
                    )
                )
            else:
                gpu_table_rows.append(
                    (
                        row.gpu_type,
                        row.group_name,
                        row.available_gpus,
                        row.used_gpus,
                        row.low_priority_gpus,
                        row.total_gpus,
                        status,
                    )
                )

            total_available += row.available_gpus
            total_used += row.used_gpus
            total_low_pri += row.low_priority_gpus
            total_gpus += row.total_gpus

        if show_workspace:
            total_row = (
                "TOTAL",
                "",
                "",
                total_available,
                total_used,
                total_low_pri,
                total_gpus,
                "",
            )
        else:
            total_row = (
                "TOTAL",
                "",
                total_available,
                total_used,
                total_low_pri,
                total_gpus,
                "",
            )
        gpu_table_rows.append(total_row)
        lines.extend(render_table(headers, gpu_table_rows, widths, aligns=aligns, line_char="─"))

    if include_cpu and cpu_rows:
        widths = (
            [16, 25, 10, 10, 10, 12, 12, 12] if show_workspace else [25, 10, 10, 10, 12, 12, 12]
        )
        headers = (
            (
                "Workspace",
                "Compute Group",
                "Avail CPU",
                "Used CPU",
                "Total CPU",
                "Avail GiB",
                "Used GiB",
                "Total GiB",
            )
            if show_workspace
            else (
                "Compute Group",
                "Avail CPU",
                "Used CPU",
                "Total CPU",
                "Avail GiB",
                "Used GiB",
                "Total GiB",
            )
        )
        aligns = (
            ["left", "left", "right", "right", "right", "right", "right", "right"]
            if show_workspace
            else ["left", "right", "right", "right", "right", "right", "right"]
        )
        cpu_table_rows: list[tuple[object, ...]] = []
        lines.append("")
        lines.append("CPU-Only Compute Groups")

        sorted_cpu_rows = sorted(cpu_rows, key=lambda x: x.cpu_available, reverse=True)
        total_cpu_available = 0.0
        total_cpu_used = 0.0
        total_cpu = 0.0
        total_mem_available = 0.0
        total_mem_used = 0.0
        total_mem = 0.0

        for row in sorted_cpu_rows:
            if show_workspace:
                cpu_table_rows.append(
                    (
                        row.workspace_name,
                        row.group_name,
                        _format_metric(row.cpu_available),
                        _format_metric(row.cpu_used),
                        _format_metric(row.cpu_total),
                        _format_metric(row.memory_available_gib),
                        _format_metric(row.memory_used_gib),
                        _format_metric(row.memory_total_gib),
                    )
                )
            else:
                cpu_table_rows.append(
                    (
                        row.group_name,
                        _format_metric(row.cpu_available),
                        _format_metric(row.cpu_used),
                        _format_metric(row.cpu_total),
                        _format_metric(row.memory_available_gib),
                        _format_metric(row.memory_used_gib),
                        _format_metric(row.memory_total_gib),
                    )
                )

            total_cpu_available += row.cpu_available
            total_cpu_used += row.cpu_used
            total_cpu += row.cpu_total
            total_mem_available += row.memory_available_gib
            total_mem_used += row.memory_used_gib
            total_mem += row.memory_total_gib

        if show_workspace:
            cpu_table_rows.append(
                (
                    "TOTAL",
                    "",
                    _format_metric(total_cpu_available),
                    _format_metric(total_cpu_used),
                    _format_metric(total_cpu),
                    _format_metric(total_mem_available),
                    _format_metric(total_mem_used),
                    _format_metric(total_mem),
                )
            )
        else:
            cpu_table_rows.append(
                (
                    "TOTAL",
                    _format_metric(total_cpu_available),
                    _format_metric(total_cpu_used),
                    _format_metric(total_cpu),
                    _format_metric(total_mem_available),
                    _format_metric(total_mem_used),
                    _format_metric(total_mem),
                )
            )
        lines.extend(render_table(headers, cpu_table_rows, widths, aligns=aligns, line_char="─"))

    lines.append("")
    lines.append("💡 Legend:")
    lines.append(
        "  Available = platform-reported total minus used; negative values come from the platform API"
    )
    lines.append("  Low Pri   = low-priority GPU usage that can be preempted by high-priority jobs")
    if include_cpu:
        lines.append("  CPU rows   = CPU-only compute groups with CPU and memory totals")
    lines.append("")
    lines.append("💡 Usage:")
    lines.append(
        "  inspire notebook quota --workspace <workspace> --group H100   # Filter to find full group names"
    )
    lines.append(
        '  inspire job create -n train --workspace <workspace> --project <project> --group H100-2号机房 -q 4,80,800 -c "python train.py"'
    )
    lines.append("  # create/profile --group requires the full compute group name")
    lines.append("")

    click.echo("\n".join(lines))


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
        if limit is not None:
            availability = availability[:limit]
        for entry in availability:
            if not entry.workspace_name:
                entry.workspace_name = workspace_names.get(entry.workspace_id, entry.workspace_name)

        if not availability:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"availability": []}))
            else:
                click.echo(human_formatter.format_error("No compute resources found"))
            return

        if ctx.json_output:
            output = [
                {
                    "workspace_id": a.workspace_id,
                    "workspace_name": a.workspace_name,
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "resource_kind": a.resource_kind,
                    "gpu_type": a.gpu_type,
                    "total_gpus": a.total_gpus,
                    "used_gpus": a.used_gpus,
                    "available_gpus": a.available_gpus,
                    "low_priority_gpus": a.low_priority_gpus,
                    "total_nodes": a.total_nodes,
                    "ready_nodes": a.ready_nodes,
                    "free_nodes": a.free_nodes,
                    "gpu_per_node": a.gpu_per_node,
                    "cpu_total": a.cpu_total,
                    "cpu_used": a.cpu_used,
                    "cpu_available": a.cpu_available,
                    "memory_total_gib": a.memory_total_gib,
                    "memory_used_gib": a.memory_used_gib,
                    "memory_available_gib": a.memory_available_gib,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
        else:
            _format_accurate_availability_table(availability, include_cpu=include_cpu)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_workspace_resources(ctx: Context, show_all: bool, no_cache: bool) -> None:
    """List workspace-specific GPU availability using browser API."""
    try:
        if no_cache:
            clear_availability_cache()

        config = None
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
        except Exception:
            pass

        availability = fetch_resource_availability(
            config=config,
            known_only=not show_all,
        )

        if not availability:
            click.echo(human_formatter.format_error("No GPU resources found in your workspace"))
            return

        if ctx.json_output:
            output = [
                {
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "gpu_type": a.gpu_type,
                    "gpus_per_node": a.gpu_per_node,
                    "total_nodes": a.total_nodes,
                    "ready_nodes": a.ready_nodes,
                    "free_nodes": a.free_nodes,
                    "free_gpus": a.free_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
            return

        _format_availability_table(availability, workspace_mode=True)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def run_resources_list(
    ctx: Context,
    *,
    no_cache: bool,
    workspace: str,
    group: Optional[str],
    limit: Optional[int],
    include_cpu: bool,
) -> None:
    _list_accurate_resources(
        ctx,
        workspace=workspace,
        group=group,
        limit=limit,
        include_cpu=include_cpu,
    )


@click.command("availability")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Clear optional workspace availability metadata cache before loading",
)
@click.option(
    "--workspace",
    required=True,
    help="Workspace name or 'all'.",
)
@click.option(
    "--group",
    default=None,
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
@click.option("--limit", "-n", type=click.IntRange(min=1), default=None, help="Maximum rows to show.")
@pass_context
def availability_resources(
    ctx: Context,
    no_cache: bool,
    workspace: str,
    group: Optional[str],
    include_cpu: bool,
    limit: Optional[int],
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
    run_resources_list(
        ctx,
        no_cache=no_cache,
        workspace=workspace,
        group=group,
        limit=limit,
        include_cpu=include_cpu,
    )
