"""Stable public projection for Ray status output."""

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


def _resource_sources(item: object) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if not isinstance(item, dict):
        return sources
    for key in (
        "resource",
        "resource_spec_price",
        "quota",
        "spec",
    ):
        value = item.get(key)
        if isinstance(value, dict):
            sources.append(value)
            nested = value.get("resource")
            if isinstance(nested, dict):
                sources.insert(0, nested)
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


def _resource_block(item: object) -> dict[str, Any]:
    sources = _resource_sources(item)
    return _compact(
        {
            "cpu": _first_scalar(sources, ("cpu", "cpu_count", "cpus")),
            "memory_gib": _first_scalar(
                sources,
                ("memory_gib", "memory_size_gib", "memory_size", "mem_gi"),
            ),
            "gpu": _first_scalar(sources, ("gpu", "gpu_count", "gpus")),
            "nodes": _first_scalar(sources, ("nodes", "node_count", "instance_count")),
            "gpu_type": _gpu_type(sources),
            "compute_group": _nested_name(
                item,
                (
                    "logic_compute_group",
                    "compute_group",
                    "compute_group_info",
                    "logic_compute_group_name",
                    "compute_group_name",
                ),
            ),
        }
    )


def _worker_items(item: object) -> list[dict[str, Any]]:
    raw = _value(item, "worker_groups")
    if raw is None:
        raw = _value(item, "workers")
    if raw is None:
        raw = _value(item, "worker_group")
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("list") or raw.get("groups")
    if not isinstance(raw, list):
        return []
    return [group for group in raw if isinstance(group, dict)]


def _worker_resource(group: dict[str, Any]) -> dict[str, Any]:
    block = _resource_block(group)
    view = _compact(
        {
            "name": _nested_name(
                group,
                ("group_name", "worker_group_name", "name", "display_name"),
            ),
            "min": _first_scalar(
                [group],
                ("min_replicas", "min_instances", "min"),
            ),
            "max": _first_scalar(
                [group],
                ("max_replicas", "max_instances", "max"),
            ),
        }
    )
    view.update(block)
    return _compact(view)


def _resource(item: object) -> Any:
    head = _value(item, "head_node")
    if head is None:
        head = _value(item, "head")
    workers = _worker_items(item)

    if head is not None or workers:
        grouped = _compact(
            {
                "head": _resource_block(head) if head is not None else None,
                "workers": [_worker_resource(group) for group in workers],
            }
        )
        return grouped or None

    raw_resource = _value(item, "resource")
    if isinstance(raw_resource, str):
        text = _text(raw_resource)
        return text or None
    block = _resource_block(item)
    return block or None


def _timestamp(item: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(_value(item, key))
        if text:
            return text
    return ""


def public_ray_status(item: object, *, fallback_name: str = "") -> dict[str, Any]:
    """Project one Ray detail payload onto stable, name-only fields."""
    name = _nested_name(item, ("name", "job_name")) or _text(fallback_name)
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
    priority_value = _value(item, "priority")
    if priority_value in (None, ""):
        priority_value = _value(item, "task_priority")
    return _compact(
        {
            "name": name or "N/A",
            "status": _text(_value(item, "status")) or "N/A",
            "project": _nested_name(
                item,
                ("project", "project_info", "project_name"),
            ),
            "compute_group": compute_group,
            "resource": _resource(item),
            "priority": _scalar(priority_value),
            "priority_level": _text(
                _value(item, "priority_level") or _value(item, "priority_name")
            ),
            "sub_status": _text(_value(item, "sub_status")),
            "created_at": _timestamp(item, ("created_at", "create_time")),
            "updated_at": _timestamp(item, ("updated_at", "update_time")),
            "finished_at": _timestamp(
                item,
                ("finished_at", "finish_time", "end_time"),
            ),
        }
    )


def _list_compute_group(item: object) -> str:
    names: list[str] = []

    def _remember(source: object) -> None:
        name = _nested_name(
            source,
            (
                "logic_compute_group",
                "compute_group",
                "compute_group_info",
                "logic_compute_group_name",
                "compute_group_name",
            ),
        )
        if name and name not in names:
            names.append(name)

    _remember(item)
    raw = _value(item, "raw")
    if isinstance(raw, dict):
        _remember(raw)
        for key in ("head_node", "head"):
            source = raw.get(key)
            if isinstance(source, dict):
                _remember(source)
        workers = raw.get("worker_groups") or raw.get("workers")
        if isinstance(workers, list):
            for worker in workers:
                if isinstance(worker, dict):
                    _remember(worker)
    return ", ".join(names)


def public_ray_list_item(
    item: object,
    *,
    workspace: str = "",
) -> dict[str, str]:
    """Project one Ray list row onto the shared workload schema."""
    return {
        "name": _nested_name(item, ("name", "job_name")) or "N/A",
        "status": _text(_value(item, "status")) or "N/A",
        "project": _nested_name(item, ("project", "project_name")),
        "workspace": (
            _nested_name(item, ("workspace", "workspace_name"))
            or _text(workspace)
        ),
        "compute_group": _list_compute_group(item),
        "created_by": _identity_name(item),
    }


def _format_resource(resource: Any) -> str:
    if isinstance(resource, str):
        return resource
    if not isinstance(resource, dict):
        return ""
    if "head" in resource or "workers" in resource:
        grouped_parts: list[str] = []
        head = resource.get("head")
        if isinstance(head, dict):
            head_text = _format_resource(head)
            if head_text:
                grouped_parts.append(f"head: {head_text}")
        workers = resource.get("workers")
        if isinstance(workers, list):
            worker_texts: list[str] = []
            for worker in workers:
                if not isinstance(worker, dict):
                    continue
                name = str(worker.get("name") or "worker")
                minimum = worker.get("min")
                maximum = worker.get("max")
                replicas = ""
                if minimum is not None or maximum is not None:
                    replicas = f" ({minimum or '-'}-{maximum or '-'} replicas)"
                body = _format_resource(
                    {
                        key: value
                        for key, value in worker.items()
                        if key not in {"name", "min", "max"}
                    }
                )
                worker_texts.append(f"{name}{replicas}: {body}" if body else f"{name}{replicas}")
            if worker_texts:
                grouped_parts.append("workers: " + "; ".join(worker_texts))
        return "; ".join(grouped_parts)

    parts: list[str] = []
    for key, label in (
        ("cpu", "CPU"),
        ("memory_gib", "GiB"),
        ("gpu", "GPU"),
        ("nodes", "nodes"),
    ):
        value = resource.get(key)
        if value not in (None, ""):
            parts.append(f"{value} {label}")
    gpu_type = resource.get("gpu_type")
    if gpu_type:
        parts.append(str(gpu_type))
    compute_group = resource.get("compute_group")
    if compute_group:
        parts.append(str(compute_group))
    return ", ".join(parts)


def format_ray_status(view: dict[str, Any]) -> str:
    """Render a projected Ray status without a command banner."""
    lines = [
        f"Name: {view.get('name') or 'N/A'}",
        f"Status: {view.get('status') or 'N/A'}",
    ]
    for key, label in (
        ("project", "Project"),
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
    "format_ray_status",
    "public_ray_list_item",
    "public_ray_status",
]
