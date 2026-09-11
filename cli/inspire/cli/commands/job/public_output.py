"""Terminal rendering for training-job output."""

from __future__ import annotations

from typing import Any

from inspire.cli.formatters.human_formatter import format_epoch


def _format_resource(resource: Any) -> str:
    if isinstance(resource, str):
        return resource
    if not isinstance(resource, dict):
        return ""
    parts: list[str] = []
    for key, label in (
        ("cpu", "CPU"),
        ("memory_gib", "GiB"),
        ("gpu", "GPU"),
        ("nodes", "nodes"),
        ("shared_memory_gib", "GiB shared memory"),
    ):
        value = resource.get(key)
        if value not in (None, ""):
            parts.append(f"{value} {label}")
    gpu_type = resource.get("gpu_type")
    if gpu_type:
        parts.append(str(gpu_type))
    return ", ".join(parts)


def format_job_status(view: dict[str, Any]) -> str:
    """Render a projected job status without an implementation banner."""
    lines = [
        f"Name: {view.get('name') or 'N/A'}",
        f"Status: {view.get('status') or 'N/A'}",
    ]
    for key, label in (
        ("project", "Project"),
        ("workspace", "Workspace"),
        ("compute_group", "Compute Group"),
    ):
        value = view.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    resource = _format_resource(view.get("resource"))
    if resource:
        lines.append(f"Resource: {resource}")
    for key, label in (
        ("nodes", "Nodes"),
        ("pinned_nodes", "Pinned Nodes"),
        ("excluded_nodes", "Excluded Nodes"),
    ):
        names = view.get(key)
        if names:
            lines.append(f"{label}: {', '.join(names)}")
    for mount in view.get("datasets") or []:
        lines.append(f"Dataset: {mount['name']}:{mount['version']} -> {mount['path']}")
    for key, label in (
        ("priority", "Priority"),
        ("priority_level", "Priority Level"),
        ("sub_status", "Sub-status"),
    ):
        value = view.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    # The view keeps epoch millis for machine consumers; a human reading
    # `job status` should see the same wall-clock format `job list` prints.
    for key, label in (
        ("created_at", "Created"),
        ("updated_at", "Updated"),
        ("finished_at", "Finished"),
    ):
        value = view.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {format_epoch(value)}")
    return "\n".join(lines)


__all__ = [
    "format_job_status",
]
