from __future__ import annotations

from typing import Any
from inspire.services.utils.text import format_epoch
from inspire.services.utils.identifiers import looks_like_platform_id
from inspire.services.utils.raw_ids import scrub_raw_ids


def _public_serving_instance_text(
    item: dict[str, Any],
    *keys: str,
) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def _serving_instance_rank(item: dict[str, Any], position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


def _serving_instance_resource(item: dict[str, Any]) -> str:
    direct = _public_serving_instance_text(item, "resource")
    if direct:
        return direct

    spec = item
    for key in ("resource_spec", "resource_spec_price", "quota"):
        candidate = item.get(key)
        if isinstance(candidate, dict):
            spec = candidate
            break

    values = (
        ("CPU", _public_serving_instance_text(spec, "cpu_count", "cpu")),
        (
            "GiB",
            _public_serving_instance_text(
                spec,
                "memory_size_gib",
                "memory_gib",
                "memory_size",
                "memory",
            ),
        ),
        ("GPU", _public_serving_instance_text(spec, "gpu_count", "gpu")),
    )
    return ", ".join(f"{value} {unit}" for unit, value in values if value)


def public_serving_instances(
    instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for position, raw in enumerate(instances):
        item: dict[str, Any] = {}
        name = _public_serving_instance_text(
            raw,
            "name",
            "instance_name",
            "display_name",
        )
        if name and not looks_like_platform_id(name):
            item["name"] = name

        for key, candidates in (
            ("status", ("status", "instance_status", "phase", "state")),
            ("role", ("role", "instance_type", "component")),
            ("type", ("type",)),
            ("node", ("node", "node_name", "host_name")),
        ):
            value = _public_serving_instance_text(raw, *candidates)
            if value:
                item[key] = value

        resource = _serving_instance_resource(raw)
        if resource:
            item["resource"] = resource
        item["rank"] = _serving_instance_rank(raw, position)
        projected.append(item)
    return projected


def public_serving_version(item: dict[str, Any]) -> dict[str, Any]:
    """Project one `ListServingVersions` row onto rollback-relevant fields."""
    view: dict[str, Any] = {}
    raw_version = item.get("version")
    if raw_version not in (None, ""):
        try:
            view["version"] = int(str(raw_version))
        except (TypeError, ValueError):
            view["version"] = scrub_raw_ids(raw_version)
    for key, candidates in (
        ("status", ("status", "phase")),
        ("model", ("model_name", "model_display_name")),
        ("command", ("command",)),
        ("created_at", ("created_at", "updated_at")),
    ):
        value = _public_serving_instance_text(item, *candidates)
        if value:
            view[key] = value
    for key, candidates in (
        ("replicas", ("replicas", "replica_count")),
        ("port", ("port",)),
    ):
        for candidate in candidates:
            raw = item.get(candidate)
            if raw not in (None, ""):
                try:
                    view[key] = int(str(raw))
                except (TypeError, ValueError):
                    pass
                break
    resource = serving_resource_label(item)
    if resource:
        view["resource"] = resource
    return view


def _scale_replica_count(item: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = item.get(key)
        if raw in (None, "") or isinstance(raw, bool):
            continue
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def public_scale_history_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Project one `ListServingScaleHistory` row onto the replica delta.

    The row's `id` is an internal counter with nothing to look up, so it is
    dropped; what answers "why did latency move" is when the replica count
    changed and what it changed from and to.
    """
    view: dict[str, Any] = {}
    before = _scale_replica_count(
        item, "replicas_before_scale", "replicas_before", "before_replicas"
    )
    after = _scale_replica_count(item, "replicas_after_scale", "replicas_after", "after_replicas")
    if before is not None:
        view["replicas_from"] = before
    if after is not None:
        view["replicas_to"] = after
    status = _public_serving_instance_text(item, "status", "state", "phase")
    if status:
        view["status"] = status
    created_at = format_epoch(item.get("created_at") or item.get("updated_at") or "")
    if created_at not in ("", "-"):
        view["created_at"] = scrub_raw_ids(created_at)
    return view


def serving_resource_label(data: dict[str, Any]) -> str:
    spec = data.get("resource_spec_price")
    if not isinstance(spec, dict):
        return ""
    gpu_count = spec.get("gpu_count")
    cpu_count = spec.get("cpu_count")
    memory = spec.get("memory_size_gib")
    gpu_info_payload = spec.get("gpu_info")
    gpu_info: dict[str, Any] = gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    gpu_type = (
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or spec.get("gpu_type_display")
        or spec.get("gpu_type")
        or ""
    )
    bits = []
    if cpu_count not in (None, ""):
        bits.append(f"{cpu_count} CPU")
    if memory not in (None, ""):
        bits.append(f"{memory} GiB")
    if gpu_count not in (None, ""):
        gpu = f"{gpu_count} GPU"
        if gpu_type:
            gpu += f" ({gpu_type})"
        bits.append(gpu)
    return ", ".join(bits)
