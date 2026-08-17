"""Small, command-local projections for notebook CLI output.

The web API returns a large object graph containing platform handles, request
payloads, and implementation metadata.  Those values are useful to the
internal API calls, but are not part of the notebook CLI contract.  Keep the
projection here so command code never has to hand an API response directly to
the output formatter.
"""

from __future__ import annotations

import re
from typing import Any

from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.platform.web.browser_api.datasets import mounted_dataset_views

_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_REDACTION_MARKER_RE = re.compile(
    r"<redacted>|<(?:raw|[a-z][a-z-]*)-id>",
    re.IGNORECASE,
)

_DROP_KEYS = {
    "debug",
    "from",
    "handle",
    "handles",
    "log",
    "logs",
    "metadata",
    "payload",
    "raw",
    "request",
    "request_id",
    "response",
    "result",
    "scanned",
    "source",
    "suffix",
    "token",
    "trace",
}


def _is_internal_key(key: object, *, omit_urls: bool) -> bool:
    normalized = str(key or "").replace("-", "_").lower()
    if normalized in _DROP_KEYS:
        return True
    if normalized in {
        "id",
        "ids",
        "hostname",
        "identity_file",
        "notebook_url",
        "proxy_url",
        "runtime",
        "url",
    }:
        return True
    if normalized.endswith("_id") or normalized.endswith("_ids"):
        return True
    return omit_urls and normalized in {"address", "uri", "web_url"}


def sanitize_public_text(value: object, *, omit_urls: bool = False) -> str:
    """Remove platform handles and internal URLs without leaving placeholders."""
    text = scrub_raw_ids(value)
    if omit_urls:
        text = _URL_RE.sub("", text)
    text = _REDACTION_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([,;:.])", r"\1", text)
    return text.strip()


def sanitize_public_data(value: Any, *, omit_urls: bool = False) -> Any:
    """Return a compact, handle-free value suitable for CLI JSON output."""
    if isinstance(value, dict):
        return {
            key: sanitize_public_data(child, omit_urls=omit_urls)
            for key, child in value.items()
            if not _is_internal_key(key, omit_urls=omit_urls)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_public_data(item, omit_urls=omit_urls) for item in value]
    if isinstance(value, str):
        return sanitize_public_text(value, omit_urls=omit_urls)
    return value


def public_operation(name: str, status: str, **fields: Any) -> dict[str, Any]:
    """Build the stable result shape used by mutating notebook commands."""
    payload: dict[str, Any] = {"name": name, "status": status}
    payload.update(fields)
    return sanitize_public_data(payload)


def _nested_name(item: dict[str, Any], key: str, *fallbacks: str) -> str:
    value = item.get(key)
    if isinstance(value, dict):
        for candidate in ("name", f"{key}_name", "display_name"):
            text = str(value.get(candidate) or "").strip()
            if text:
                return sanitize_public_text(text, omit_urls=True)
    elif value not in (None, ""):
        return sanitize_public_text(value, omit_urls=True)
    for fallback in fallbacks:
        text = str(item.get(fallback) or "").strip()
        if text:
            return sanitize_public_text(text, omit_urls=True)
    return ""


def _compact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", [], {})
    }


def _first_public_text(*values: object) -> str:
    for value in values:
        text = sanitize_public_text(value or "", omit_urls=True)
        if text:
            return text
    return ""


def _image_label(item: dict[str, Any]) -> str:
    image = item.get("image")
    if isinstance(image, dict):
        name = _first_public_text(
            image.get("name"),
            image.get("image_name"),
            image.get("display_name"),
        )
        version = _first_public_text(image.get("version"), image.get("tag"))
        if name and version:
            return f"{name}:{version}"
        if name:
            return name
    return _first_public_text(
        item.get("image_name"),
        item.get("mirror_name"),
        image,
    )


def _gpu_type(item: dict[str, Any], quota: dict[str, Any]) -> str:
    resource_spec = item.get("resource_spec")
    resource_spec_price = item.get("resource_spec_price")
    node = item.get("node")
    logic_compute_group = item.get("logic_compute_group")
    compute_group = item.get("compute_group")

    resource_spec = resource_spec if isinstance(resource_spec, dict) else {}
    resource_spec_price = (
        resource_spec_price if isinstance(resource_spec_price, dict) else {}
    )
    node = node if isinstance(node, dict) else {}
    logic_compute_group = (
        logic_compute_group if isinstance(logic_compute_group, dict) else {}
    )
    compute_group = compute_group if isinstance(compute_group, dict) else {}

    gpu_info = resource_spec_price.get("gpu_info")
    gpu_info = gpu_info if isinstance(gpu_info, dict) else {}
    node_gpu_info = node.get("gpu_info")
    node_gpu_info = node_gpu_info if isinstance(node_gpu_info, dict) else {}

    return _first_public_text(
        gpu_info.get("gpu_product_simple"),
        gpu_info.get("gpu_type_display"),
        gpu_info.get("brand_name"),
        gpu_info.get("gpu_type"),
        quota.get("gpu_type_display"),
        quota.get("gpu_type"),
        resource_spec.get("gpu_type_display"),
        resource_spec.get("gpu_type"),
        node_gpu_info.get("gpu_type_display"),
        node_gpu_info.get("brand_name"),
        node_gpu_info.get("gpu_type"),
        item.get("gpu_type_display"),
        item.get("gpu_type"),
        logic_compute_group.get("gpu_type_display"),
        logic_compute_group.get("gpu_type"),
        compute_group.get("gpu_type_display"),
        compute_group.get("gpu_type"),
    )


def _node(item: dict[str, Any]) -> dict[str, Any]:
    """Project where this notebook is actually running.

    ``GetNotebook`` carries the whole node object, but only while the instance
    holds one: a STOPPED notebook answers an empty ``name`` and the proto
    zero-value ``UNKNOWN_NODE_STATUS``, which must read as "not placed" rather
    than as a node in an unknown state. ``cordoned`` and ``maintenance`` are
    reported only when set, because a placed-but-draining node explains a
    notebook that is running now and will not be after the next restart.
    """
    node = item.get("node")
    node = node if isinstance(node, dict) else {}
    name = _first_public_text(node.get("name"))
    if not name:
        return {}

    status = _first_public_text(node.get("status"))
    if status.upper().startswith("UNKNOWN"):
        status = ""
    return _compact_mapping(
        {
            "name": name,
            "status": status,
            "cordoned": _first_public_text(node.get("cordon_type")),
            "maintenance": bool(node.get("is_maint")) or None,
        }
    )


def _created_by(item: dict[str, Any]) -> str:
    for key in ("created_by", "creator", "owner"):
        value = item.get(key)
        if not isinstance(value, dict):
            continue
        text = _first_public_text(
            value.get("name"),
            value.get("display_name"),
        )
        if text:
            return text
    return _first_public_text(
        item.get("created_by_name"),
        item.get("creator_name"),
        item.get("owner_name"),
    )


def public_notebook(
    item: dict[str, Any],
    *,
    fallback_name: str = "",
) -> dict[str, Any]:
    """Project a notebook list/detail object onto its user-facing fields."""
    quota_value = item.get("quota")
    quota: dict[str, Any] = quota_value if isinstance(quota_value, dict) else {}
    start_config_value = item.get("start_config")
    start_config = (
        start_config_value if isinstance(start_config_value, dict) else {}
    )
    priority = item.get("task_priority")
    if priority in (None, ""):
        priority = item.get("priority")
    # A notebook carries its priority under `project`, not at the top level
    # like the workload records do — the web 优先级 column reads it from there
    # ("高优任务-10" is priority_level + priority_name).
    project_value = item.get("project")
    project = project_value if isinstance(project_value, dict) else {}
    if priority in (None, ""):
        priority = project.get("priority_name")
    resource = _compact_mapping(
        {
            "gpu_count": quota.get("gpu_count"),
            "gpu_type": _gpu_type(item, quota),
            "cpu_count": quota.get("cpu_count"),
            "memory_gib": quota.get("memory_size") or quota.get("memory_size_gib"),
        }
    )
    return _compact_mapping(
        {
            "name": _first_public_text(item.get("name"), fallback_name),
            "status": sanitize_public_text(item.get("status") or "", omit_urls=True),
            "project": _nested_name(item, "project", "project_name"),
            "workspace": _nested_name(item, "workspace", "workspace_name"),
            "compute_group": _nested_name(
                item,
                "logic_compute_group",
                "compute_group_name",
                "logic_compute_group_name",
            ),
            "created_by": _created_by(item),
            "image": _image_label(item),
            "resource": resource,
            "node": _node(item),
            "priority": priority,
            "priority_level": _first_public_text(
                item.get("priority_level"),
                item.get("priority_name"),
                project.get("priority_level"),
            ),
            "shared_memory_gib": (
                start_config.get("shared_memory_size")
                or item.get("shared_memory_gib")
                or item.get("shm_gib")
            ),
            "uptime_seconds": item.get("live_time"),
            # What `--auto-stop-after` actually bought: the web shows this as
            # 剩余运行时长 and it is the only readback for that timer.
            "auto_stop_in_seconds": item.get("left_time"),
            "datasets": mounted_dataset_views(item.get("dataset_info")),
            "created_at": sanitize_public_text(item.get("created_at") or ""),
            "updated_at": sanitize_public_text(item.get("updated_at") or ""),
        }
    )


def public_notebook_list_item(item: dict[str, Any]) -> dict[str, Any]:
    """Project one notebook list row onto the shared workload schema."""
    view = public_notebook(item)
    return {
        key: view.get(key, "")
        for key in (
            "name",
            "status",
            "project",
            "workspace",
            "compute_group",
            "created_by",
        )
    }


def public_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project notebook run-cycle records without runtime handles."""
    return [
        _compact_mapping(
            {
                "index": run.get("index"),
                "start_time": sanitize_public_text(run.get("start_time") or ""),
                "end_time": sanitize_public_text(run.get("end_time") or ""),
                "status": sanitize_public_text(run.get("status") or "", omit_urls=True),
            }
        )
        for run in runs
        if isinstance(run, dict)
    ]


__all__ = [
    "public_notebook",
    "public_notebook_list_item",
    "public_operation",
    "public_runs",
    "sanitize_public_data",
    "sanitize_public_text",
]
