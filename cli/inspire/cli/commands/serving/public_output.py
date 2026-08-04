"""Command-local projections for inference-serving output."""

from __future__ import annotations

from typing import Any

from inspire.cli.commands.notebook.public_output import (
    sanitize_public_data,
    sanitize_public_text,
)


def _value(item: object, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _nested_name(item: object, key: str, *fallbacks: str) -> str:
    value = _value(item, key)
    if isinstance(value, dict):
        for candidate in ("name", f"{key}_name", "display_name"):
            text = str(value.get(candidate) or "").strip()
            if text:
                return sanitize_public_text(text, omit_urls=True)
    elif value not in (None, ""):
        return sanitize_public_text(value, omit_urls=True)
    for fallback in fallbacks:
        text = str(_value(item, fallback) or "").strip()
        if text:
            return sanitize_public_text(text, omit_urls=True)
    return ""


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", [], {})
    }


def _model_label(item: object) -> str:
    model = _value(item, "model")
    name = (
        _value(item, "model_name")
        or _value(item, "model_display_name")
        or (model.get("name") if isinstance(model, dict) else model)
    )
    version = (
        _value(item, "model_version")
        or (model.get("version") if isinstance(model, dict) else None)
    )
    if not name:
        return ""
    label = str(name)
    if version not in (None, ""):
        label += f" v{version}"
    return sanitize_public_text(label, omit_urls=True)


def _image_label(item: object) -> str:
    mirror = _value(item, "mirror")
    name = (
        _value(item, "image_name")
        or _value(item, "image")
        or (mirror.get("name") if isinstance(mirror, dict) else None)
    )
    version = mirror.get("version") if isinstance(mirror, dict) else None
    if name and version:
        return sanitize_public_text(f"{name}:{version}", omit_urls=True)
    if name:
        text = sanitize_public_text(name, omit_urls=True)
        if not text or str(name).strip().lower().startswith(
            ("http://", "https://", "docker://")
        ):
            return ""
        return text
    # Image URLs and registry addresses are implementation details.  Do not
    # fall back to them when the API did not provide a display name.
    return ""


def public_serving(item: object, *, fallback_name: str = "") -> dict[str, Any]:
    """Project a serving list/detail object onto stable user-facing fields."""
    name = str(_value(item, "name") or fallback_name or "").strip()
    resource = _value(item, "resource")
    if isinstance(resource, dict):
        resource = None
    if not resource:
        resource = _value(item, "quota")
        if isinstance(resource, dict):
            resource = None
    if not resource:
        spec = _value(item, "resource_spec_price")
        if isinstance(spec, dict):
            bits: list[str] = []
            if spec.get("cpu_count") not in (None, ""):
                bits.append(f"{spec['cpu_count']} CPU")
            if spec.get("memory_size_gib") not in (None, ""):
                bits.append(f"{spec['memory_size_gib']} GiB")
            if spec.get("gpu_count") not in (None, ""):
                bits.append(f"{spec['gpu_count']} GPU")
            resource = ", ".join(bits)
    return sanitize_public_data(
        _compact(
            {
                "name": sanitize_public_text(name, omit_urls=True),
                "status": sanitize_public_text(
                    _value(item, "status") or "",
                    omit_urls=True,
                ),
                "type": sanitize_public_text(
                    _value(
                        item,
                        "inference_serving_type",
                        _value(item, "type"),
                    )
                    or "",
                    omit_urls=True,
                ),
                "model": _model_label(item),
                "image": _image_label(item),
                "project": _nested_name(item, "project", "project_name"),
                "workspace": _nested_name(item, "workspace", "workspace_name"),
                "resource": (
                    sanitize_public_text(resource, omit_urls=True)
                    if resource not in (None, "")
                    else None
                ),
                "replicas": _value(item, "replicas"),
                "nodes_per_replica": _value(
                    item,
                    "node_num_per_replica",
                    _value(item, "nodes_per_replica"),
                ),
                "priority": _value(item, "task_priority", _value(item, "priority")),
                "port": _value(item, "port"),
                "command": sanitize_public_text(
                    _value(item, "command") or "",
                    omit_urls=True,
                ),
                "created_at": sanitize_public_text(_value(item, "created_at") or ""),
                "updated_at": sanitize_public_text(_value(item, "updated_at") or ""),
            }
        )
    )


def _config_item(item: object, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": f"config {index}"}
    return sanitize_public_data(
        _compact(
            {
                "name": sanitize_public_text(
                    item.get("name")
                    or item.get("config_name")
                    or item.get("image_name")
                    or item.get("model_name")
                    or f"config {index}",
                    omit_urls=True,
                ),
                "gpu_count_min": item.get("gpu_count_min"),
                "gpu_count_max": item.get("gpu_count_max"),
                "cpu_count_min": item.get("cpu_count_min"),
                "cpu_count_max": item.get("cpu_count_max"),
                "memory_gib_min": item.get("memory_size_min")
                or item.get("memory_size_gib_min"),
                "memory_gib_max": item.get("memory_size_max")
                or item.get("memory_size_gib_max"),
                "replicas": item.get("replicas"),
                "auto_stop_rules": item.get("auto_stop_ruleset"),
            }
        )
    )


def public_configs(data: object) -> dict[str, Any]:
    """Project serving config discovery data without raw API sections."""
    configs = data.get("configs") if isinstance(data, dict) else None
    if isinstance(configs, list):
        items = configs
        enabled = None
    elif isinstance(configs, dict):
        raw_items = configs.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        enabled = configs.get("enable_auto_stop")
    else:
        items = []
        enabled = None
    payload: dict[str, Any] = {
        "items": [_config_item(item, index) for index, item in enumerate(items, 1)],
    }
    if enabled is not None:
        payload["auto_stop"] = bool(enabled)
    return sanitize_public_data(payload)


def public_operation(name: str, status: str, **fields: Any) -> dict[str, Any]:
    """Build a stable, name-oriented result without platform metadata."""
    payload: dict[str, Any] = {"name": name, "status": status}
    payload.update(fields)
    return sanitize_public_data(payload)


__all__ = [
    "public_configs",
    "public_operation",
    "public_serving",
    "sanitize_public_data",
]
