"""Public Ray scaling history."""

from __future__ import annotations
from typing import Any, Optional
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.services.utils import text as human_formatter


def scaling_int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def event_time(item: dict[str, Any]) -> int:
    for key in ("event_time", "created_at", "timestamp_ms"):
        value = scaling_int_or_none(item.get(key))
        if value is not None:
            return value
    return 0


def scaling_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def public_ray_scaling_events(
    items: list[dict[str, Any]],
    *,
    group: str = "",
) -> list[dict[str, Any]]:
    """Project scaling rows onto a stable allowlist.

    The wire row is ``event_time`` / ``event_type`` / ``replicas_before`` /
    ``replicas_after``. ``event_type`` is one of ``initialized``, ``scale_up``
    and ``scale_down``; it is passed through rather than translated so the JSON
    stays greppable across locales.
    """
    projected: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "time": human_formatter.format_epoch(event_time(item)),
            "event": scaling_text(item, "event_type", "type") or "unknown",
        }
        group_name = scaling_text(item, "worker_group_name", "group_name") or scrub_raw_ids(group).strip()
        if group_name:
            row["group"] = group_name
        before = scaling_int_or_none(item.get("replicas_before"))
        after = scaling_int_or_none(item.get("replicas_after"))
        if before is not None:
            row["replicas_before"] = before
        if after is not None:
            row["replicas_after"] = after
        projected.append(row)
    return projected
