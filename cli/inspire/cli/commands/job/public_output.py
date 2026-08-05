"""Stable public projections for training-job output."""

from __future__ import annotations

import re
from typing import Any

from inspire.cli.utils.raw_ids import scrub_raw_ids

_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_REDACTION_RE = re.compile(r"<redacted>|<(?:raw|[a-z][a-z-]*)-id>", re.IGNORECASE)


def _value(item: object, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _text(value: object, *, omit_paths: bool = True) -> str:
    if value in (None, ""):
        return ""
    text = scrub_raw_ids(value).strip()
    if not text:
        return ""
    if omit_paths and (_URL_RE.search(text) or _PATH_RE.match(text)):
        return ""
    text = _URL_RE.sub("", text)
    text = _REDACTION_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _scalar(value: object) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text(value)
    return None


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", [], {})
    }


def _nested_name(item: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _value(item, key)
        if isinstance(value, dict):
            for name_key in ("name", "display_name", "label", f"{key}_name"):
                text = _text(value.get(name_key))
                if text:
                    return text
        else:
            text = _text(value)
            if text:
                return text
    return ""


def _identity_name(item: object) -> str:
    for key in ("created_by", "creator", "owner"):
        value = _value(item, key)
        if not isinstance(value, dict):
            continue
        for name_key in ("name", "display_name"):
            name = value.get(name_key)
            if isinstance(name, str):
                text = _text(name)
                if text:
                    return text
    for key in ("created_by_name", "creator_name", "owner_name"):
        name = _value(item, key)
        if isinstance(name, str):
            text = _text(name)
            if text:
                return text
    return ""


def _framework_spec(item: object) -> dict[str, Any]:
    raw = _value(item, "framework_config")
    if isinstance(raw, list):
        return next((entry for entry in raw if isinstance(entry, dict)), {})
    if isinstance(raw, dict):
        return raw
    return {}


def _resource_sources(item: object) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    framework = _framework_spec(item)
    if framework:
        sources.append(framework)
        price = framework.get("instance_spec_price_info")
        if isinstance(price, dict):
            sources.insert(0, price)
    for key in ("resource", "resource_spec_price", "quota"):
        value = _value(item, key)
        if isinstance(value, dict):
            sources.insert(0, value)
    if isinstance(item, dict):
        sources.append(item)
    return sources


def _first_scalar(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for source in sources:
        for key in keys:
            value = _scalar(source.get(key))
            if value is not None:
                return value
    return None


def _gpu_type(sources: list[dict[str, Any]]) -> str:
    for source in sources:
        gpu_info = source.get("gpu_info")
        if isinstance(gpu_info, dict):
            for key in ("gpu_type_display", "display_name", "name", "gpu_type"):
                text = _text(gpu_info.get(key))
                if text:
                    return text
        for key in ("gpu_type_display", "gpu_name", "gpu_type"):
            text = _text(source.get(key))
            if text:
                return text
    return ""


def _resource(item: object) -> Any:
    raw_resource = _value(item, "resource")
    if isinstance(raw_resource, str):
        text = _text(raw_resource)
        if text:
            return text

    sources = _resource_sources(item)
    block = _compact(
        {
            "cpu": _first_scalar(sources, ("cpu", "cpu_count", "cpus")),
            "memory_gib": _first_scalar(
                sources,
                ("memory_gib", "memory_size_gib", "memory_size", "mem_gi"),
            ),
            "gpu": _first_scalar(sources, ("gpu", "gpu_count", "gpus")),
            "nodes": _first_scalar(
                sources,
                ("nodes", "node_count", "instance_count"),
            ),
            "shared_memory_gib": _first_scalar(
                sources,
                ("shared_memory_gib", "shm_gib", "shm_gi"),
            ),
            "gpu_type": _gpu_type(sources),
        }
    )
    return block or None


def _timestamp(item: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(_value(item, key))
        if text:
            return text
    return ""


def public_job_status(item: object, *, fallback_name: str = "") -> dict[str, Any]:
    """Project one job detail payload onto stable, name-only fields."""
    name = _nested_name(item, ("name", "job_name")) or _text(fallback_name)
    sources = _resource_sources(item)
    compute_group = _nested_name(
        item,
        (
            "logic_compute_group",
            "compute_group",
            "compute_group_info",
            "logic_compute_group_name",
            "compute_group_name",
        ),
    )
    if not compute_group:
        for source in sources:
            compute_group = _nested_name(
                source,
                (
                    "logic_compute_group",
                    "compute_group",
                    "compute_group_info",
                    "logic_compute_group_name",
                    "compute_group_name",
                ),
            )
            if compute_group:
                break

    priority = _scalar(_value(item, "priority"))
    if priority is None:
        priority = _scalar(_value(item, "task_priority"))
    priority_level = _text(
        _value(item, "priority_level") or _value(item, "priority_name")
    )
    if priority is not None and priority_level == str(priority):
        priority_level = ""

    return _compact(
        {
            "name": name or "N/A",
            "status": _text(_value(item, "status")) or "N/A",
            "project": _nested_name(
                item,
                ("project", "project_info", "project_name"),
            ),
            "workspace": _nested_name(
                item,
                ("workspace", "workspace_info", "workspace_name"),
            ),
            "compute_group": compute_group,
            "resource": _resource(item),
            "priority": priority,
            "priority_level": priority_level,
            "sub_status": _text(_value(item, "sub_status")),
            "created_at": _timestamp(item, ("created_at", "create_time")),
            "updated_at": _timestamp(item, ("updated_at", "update_time")),
            "finished_at": _timestamp(
                item,
                ("finished_at", "finish_time", "end_time"),
            ),
        }
    )


def public_job_list_item(item: object) -> dict[str, str]:
    """Project one training-job list row onto the shared workload schema."""
    return {
        "name": _nested_name(item, ("name", "job_name")) or "N/A",
        "status": _text(_value(item, "status")) or "N/A",
        "project": _nested_name(item, ("project", "project_name")),
        "workspace": _nested_name(item, ("workspace", "workspace_name")),
        "compute_group": _nested_name(
            item,
            (
                "logic_compute_group",
                "compute_group",
                "logic_compute_group_name",
                "compute_group_name",
            ),
        ),
        "created_by": _identity_name(item),
    }


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
        ("priority", "Priority"),
        ("priority_level", "Priority Level"),
        ("sub_status", "Sub-status"),
        ("created_at", "Created"),
        ("updated_at", "Updated"),
        ("finished_at", "Finished"),
    ):
        value = view.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


__all__ = [
    "format_job_status",
    "public_job_list_item",
    "public_job_status",
]
