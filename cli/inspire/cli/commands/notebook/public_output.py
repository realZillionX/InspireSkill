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

_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_ID_MARKER_RE = re.compile(r"<(?:raw|[a-z][a-z-]*)-id>", re.IGNORECASE)

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
    text = _ID_MARKER_RE.sub("", text)
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


def public_notebook(item: dict[str, Any]) -> dict[str, Any]:
    """Project a notebook list/detail object onto its user-facing fields."""
    quota = item.get("quota") if isinstance(item.get("quota"), dict) else {}
    resource = _compact_mapping(
        {
            "gpu_count": quota.get("gpu_count"),
            "cpu_count": quota.get("cpu_count"),
            "memory_gib": quota.get("memory_size") or quota.get("memory_size_gib"),
        }
    )
    return _compact_mapping(
        {
            "name": sanitize_public_text(item.get("name") or "", omit_urls=True),
            "status": sanitize_public_text(item.get("status") or "", omit_urls=True),
            "project": _nested_name(item, "project", "project_name"),
            "workspace": _nested_name(item, "workspace", "workspace_name"),
            "compute_group": _nested_name(
                item,
                "logic_compute_group",
                "compute_group_name",
                "logic_compute_group_name",
            ),
            "image": _nested_name(item, "image", "image_name", "mirror_name"),
            "resource": resource,
            "priority": item.get("task_priority"),
            "created_at": sanitize_public_text(item.get("created_at") or ""),
            "updated_at": sanitize_public_text(item.get("updated_at") or ""),
        }
    )


def public_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project notebook lifecycle events without object handles or source metadata."""
    return [
        _compact_mapping(
            {
                "time": event.get("last_timestamp") or event.get("first_timestamp"),
                "type": sanitize_public_text(event.get("type") or "", omit_urls=True),
                "reason": sanitize_public_text(event.get("reason") or "", omit_urls=True),
                "message": sanitize_public_text(
                    event.get("message") or event.get("content") or "",
                    omit_urls=True,
                ),
                "count": event.get("count"),
            }
        )
        for event in events
        if isinstance(event, dict)
    ]


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
    "public_events",
    "public_notebook",
    "public_operation",
    "public_runs",
    "sanitize_public_data",
    "sanitize_public_text",
]
