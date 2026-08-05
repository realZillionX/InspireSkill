"""Presentation helpers for notebook CLI output."""

from __future__ import annotations

import click

from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.raw_ids import scrub_raw_ids
from .notebook_lookup import _format_notebook_cpu, _format_notebook_gpu
from .public_output import public_notebook_list_item


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


def _format_public_resource(resource: object) -> str:
    if isinstance(resource, str):
        return resource
    if not isinstance(resource, dict):
        return ""
    parts: list[str] = []
    gpu_count = resource.get("gpu_count")
    if gpu_count not in (None, "", 0):
        gpu_type = str(resource.get("gpu_type") or "GPU")
        parts.append(f"{gpu_count}x {gpu_type}")
    cpu_count = resource.get("cpu_count")
    if cpu_count not in (None, ""):
        parts.append(f"{cpu_count} CPU")
    memory_gib = resource.get("memory_gib")
    if memory_gib not in (None, ""):
        parts.append(f"{memory_gib} GiB")
    return ", ".join(parts)


def _format_uptime(seconds: object) -> str:
    if not isinstance(seconds, (int, float, str)):
        return ""
    try:
        live_seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if live_seconds <= 0:
        return ""
    hours, rem = divmod(live_seconds, 3600)
    minutes = rem // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "< 1m"


def _print_notebook_detail(notebook: dict) -> None:
    """Print one already-projected notebook detail."""
    fields = [
        ("Name", notebook.get("name") or "N/A"),
        ("Status", notebook.get("status") or "N/A"),
        ("Project", notebook.get("project")),
        ("Workspace", notebook.get("workspace")),
        ("Compute Group", notebook.get("compute_group")),
        ("Created By", notebook.get("created_by")),
        ("Image", notebook.get("image")),
        ("Resource", _format_public_resource(notebook.get("resource"))),
        ("Priority", notebook.get("priority")),
        ("Priority Level", notebook.get("priority_level")),
        (
            "Shared Memory",
            (
                f"{notebook['shared_memory_gib']} GiB"
                if notebook.get("shared_memory_gib") not in (None, "")
                else None
            ),
        ),
        ("Uptime", _format_uptime(notebook.get("uptime_seconds"))),
        ("Created", notebook.get("created_at")),
        ("Updated", notebook.get("updated_at")),
    ]

    for label, value in fields:
        if value not in (None, ""):
            click.echo(f"{label}: {scrub_raw_ids(value)}")


def _print_notebook_list(
    items: list,
    json_output: bool,
    *,
    total: int | None = None,
    truncated: bool = False,
) -> None:
    """Print notebook list in appropriate format.

    The CLI takes names only. JSON output follows the same boundary.
    """
    if json_output:
        payload: dict[str, object] = {
            "items": [public_notebook_list_item(item) for item in items],
        }
        if truncated:
            payload.update(
                {
                    "shown": len(items),
                    "total": max(len(items), int(total or 0)),
                    "truncated": True,
                }
            )
        click.echo(json_formatter.format_json(payload))
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
    click.echo("\n".join([lines[1], lines[2], *lines[3:-1]]))


__all__ = ["_print_notebook_detail", "_print_notebook_list"]
