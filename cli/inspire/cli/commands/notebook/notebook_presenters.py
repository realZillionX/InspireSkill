"""Presentation helpers for notebook CLI output."""

from __future__ import annotations

import click

from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.raw_ids import scrub_raw_ids
from .notebook_lookup import _format_notebook_cpu, _format_notebook_gpu, _notebook_gpu_type, _positive_int
from .public_output import public_notebook


def _nested_name(item: dict, key: str, *fallback_keys: str) -> str:
    value = item.get(key)
    if isinstance(value, dict):
        for nested_key in ("name", *fallback_keys):
            nested_value = value.get(nested_key)
            if nested_value:
                return scrub_raw_ids(str(nested_value))
    for fallback_key in fallback_keys:
        fallback_value = item.get(fallback_key)
        if fallback_value:
            return scrub_raw_ids(str(fallback_value))
    return "-"


def _format_notebook_project(item: dict) -> str:
    return _nested_name(item, "project", "project_name", "projectName")


def _format_notebook_workspace(item: dict) -> str:
    return _nested_name(item, "workspace", "workspace_name", "workspaceName")


def _print_notebook_detail(notebook: dict) -> None:
    """Print detailed notebook information."""
    project = notebook.get("project") or {}
    quota = notebook.get("quota") or {}
    compute_group = notebook.get("logic_compute_group") or {}
    image = notebook.get("image") or {}
    start_cfg = notebook.get("start_config") or {}
    workspace = notebook.get("workspace") or {}

    gpu_type = _notebook_gpu_type(notebook)
    gpu_count = _positive_int(quota.get("gpu_count"))
    gpu_str = f"{gpu_count}x {gpu_type}" if gpu_type and gpu_count else str(gpu_count or "N/A")

    img_name = image.get("name", "")
    img_ver = image.get("version", "")
    img_str = f"{img_name}:{img_ver}" if img_name and img_ver else img_name or "N/A"

    live_seconds = int(notebook.get("live_time") or 0)
    uptime = ""
    if live_seconds > 0:
        hours, rem = divmod(live_seconds, 3600)
        minutes = rem // 60
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        uptime = " ".join(parts) or "< 1m"

    shm = start_cfg.get("shared_memory_size", 0) or 0

    # Raw notebook_id intentionally omitted — names are the CLI boundary.
    fields = [
        ("Status", notebook.get("status")),
        ("Project", project.get("name") or notebook.get("project_name")),
        ("Priority", project.get("priority_name")),
        ("Compute Group", compute_group.get("name")),
        ("Image", img_str),
        ("GPU", gpu_str),
        ("CPU", quota.get("cpu_count")),
        ("Memory", f"{quota['memory_size']} GiB" if quota.get("memory_size") else None),
        ("SHM", f"{shm} GiB" if shm else None),
        ("Uptime", uptime or None),
        ("Workspace", workspace.get("name")),
        ("Created", notebook.get("created_at")),
    ]

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {scrub_raw_ids(value)}")


def _print_notebook_list(items: list, json_output: bool) -> None:
    """Print notebook list in appropriate format.

    The CLI takes names only. JSON output follows the same boundary.
    """
    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": [public_notebook(item) for item in items],
                    "total": len(items),
                }
            )
        )
        return

    if not items:
        click.echo("No notebook instances found.")
        return

    name_strings = [scrub_raw_ids(item.get("name") or "N/A") for item in items]
    status_strings = [scrub_raw_ids(item.get("status") or "Unknown") for item in items]
    project_strings = [_format_notebook_project(item) for item in items]
    workspace_strings = [_format_notebook_workspace(item) for item in items]
    gpu_strings = [scrub_raw_ids(_format_notebook_gpu(item)) for item in items]
    cpu_strings = [scrub_raw_ids(_format_notebook_cpu(item)) for item in items]
    created_strings = [scrub_raw_ids(format_epoch(item.get("created_at"))) for item in items]

    table_rows = list(
        zip(
            name_strings,
            status_strings,
            project_strings,
            workspace_strings,
            gpu_strings,
            cpu_strings,
            created_strings,
        )
    )
    widths = [
        column_width("Name", name_strings, max_width=80),
        column_width("Status", status_strings, max_width=18),
        column_width("Project", project_strings, max_width=32),
        column_width("Workspace", workspace_strings, max_width=24),
        column_width("GPU", gpu_strings, max_width=24),
        column_width("CPU", cpu_strings, max_width=8),
        column_width("Created", created_strings, max_width=19),
    ]
    lines = render_table(
        ("Name", "Status", "Project", "Workspace", "GPU", "CPU", "Created"),
        table_rows,
        widths,
        line_char="─",
    )
    click.echo("\n".join(lines))


__all__ = ["_print_notebook_detail", "_print_notebook_list"]
