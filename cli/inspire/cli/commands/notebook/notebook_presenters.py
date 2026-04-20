"""Presentation helpers for notebook CLI output."""

from __future__ import annotations

import click

from inspire.cli.formatters import json_formatter
from .notebook_lookup import _format_notebook_resource


def _print_notebook_detail(notebook: dict) -> None:
    """Print detailed notebook information."""
    click.echo(f"\n{'='*60}")
    click.echo(f"Notebook: {notebook.get('name', 'N/A')}")
    click.echo(f"{'='*60}")

    project = notebook.get("project") or {}
    quota = notebook.get("quota") or {}
    compute_group = notebook.get("logic_compute_group") or {}
    extra = notebook.get("extra_info") or {}
    image = notebook.get("image") or {}
    start_cfg = notebook.get("start_config") or {}
    workspace = notebook.get("workspace") or {}
    node = notebook.get("node") or {}

    gpu_type = ""
    node_gpu_info = node.get("gpu_info")
    if isinstance(node_gpu_info, dict):
        gpu_type = node_gpu_info.get("gpu_product_simple", "")
    if not gpu_type:
        spec = notebook.get("resource_spec") or {}
        gpu_type = spec.get("gpu_type", "")

    gpu_count = quota.get("gpu_count", 0)
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

    fields = [
        ("ID", notebook.get("notebook_id") or notebook.get("id")),
        ("Status", notebook.get("status")),
        ("Project", project.get("name") or notebook.get("project_name")),
        ("Priority", project.get("priority_name")),
        ("Compute Group", compute_group.get("name")),
        ("Image", img_str),
        ("GPU", gpu_str),
        ("CPU", quota.get("cpu_count")),
        ("Memory", f"{quota['memory_size']} GiB" if quota.get("memory_size") else None),
        ("SHM", f"{shm} GiB" if shm else None),
        ("Node", extra.get("NodeName") or None),
        ("Host IP", extra.get("HostIP") or None),
        ("Uptime", uptime or None),
        ("Workspace", workspace.get("name")),
        ("Created", notebook.get("created_at")),
    ]

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {value}")

    click.echo(f"{'='*60}\n")


def _print_notebook_list(items: list, json_output: bool) -> None:
    """Print notebook list in appropriate format."""
    if json_output:
        click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
        return

    if not items:
        click.echo("No notebook instances found.")
        return

    lines = [
        f"{'Name':<25} {'Status':<12} {'Resource':<12} {'ID':<38}",
        "-" * 90,
    ]

    for item in items:
        name = item.get("name", "N/A")[:25]
        status = item.get("status", "Unknown")[:12]
        notebook_id = item.get("notebook_id", item.get("id", "N/A"))
        resource_info = _format_notebook_resource(item)
        lines.append(f"{name:<25} {status:<12} {resource_info:<12} {notebook_id:<38}")

    lines.append(f"\nShowing {len(items)} notebook(s)")
    click.echo("\n".join(lines))


__all__ = ["_print_notebook_detail", "_print_notebook_list"]
