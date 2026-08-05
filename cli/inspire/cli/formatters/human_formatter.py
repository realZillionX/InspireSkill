"""Compact human-readable output helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from inspire.cli.utils.raw_ids import scrub_raw_ids


def format_error(message: str, hint: Optional[str] = None) -> str:
    """Format an error and optional recovery hint."""
    lines = [f"Error: {scrub_raw_ids(message)}"]
    if hint:
        lines.append(f"Hint: {scrub_raw_ids(hint)}")
    return "\n".join(lines)


def format_success(message: str) -> str:
    """Format a success message."""
    return f"OK {scrub_raw_ids(message)}"


def format_mutation_success(resource: str, status: str, name: object) -> str:
    """Format a successful single-resource mutation."""
    return format_success(f"{resource} {status}: {name}")


def _format_duration(ms: str) -> str:
    """Format milliseconds as a compact duration."""
    try:
        milliseconds = int(ms)
        seconds = milliseconds // 1000
        minutes = seconds // 60
        hours = minutes // 60

        if hours > 0:
            return f"{hours}h {minutes % 60}m {seconds % 60}s"
        if minutes > 0:
            return f"{minutes}m {seconds % 60}s"
        return f"{seconds}s"
    except (ValueError, TypeError):
        return "Unknown"


def _format_timestamp(timestamp_ms: str) -> str:
    """Format an epoch-millisecond timestamp."""
    try:
        timestamp = int(timestamp_ms) / 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "Unknown"


def format_epoch(value: Any) -> str:
    """Format an epoch in seconds or milliseconds for display."""
    if value is None or value == "":
        return "-"
    try:
        epoch = int(str(value))
    except (ValueError, TypeError):
        return str(value)
    if epoch <= 0:
        return "-"
    if epoch >= 100_000_000_000:
        epoch //= 1000
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "-"


def format_job_status(job_data: Dict[str, Any]) -> str:
    """Format job status as compact key-value lines."""
    status = str(job_data.get("status", "UNKNOWN"))
    fields = [
        ("Name", job_data.get("name", "N/A")),
        ("Status", status),
        ("Running Time", _format_duration(job_data.get("running_time_ms", "0"))),
    ]

    if job_data.get("node_count"):
        fields.append(("Nodes", str(job_data["node_count"])))
    if job_data.get("priority"):
        fields.append(("Requested Priority", str(job_data["priority"])))
    if job_data.get("priority_name"):
        fields.append(("Priority Name", str(job_data["priority_name"])))
    if job_data.get("priority_level"):
        fields.append(("Priority Level", str(job_data["priority_level"])))
    if job_data.get("sub_msg"):
        fields.append(("Message", scrub_raw_ids(job_data["sub_msg"][:40])))
    if job_data.get("created_at"):
        fields.append(("Created", _format_timestamp(job_data["created_at"])))
    if job_data.get("finished_at"):
        fields.append(("Finished", _format_timestamp(job_data["finished_at"])))

    return "\n".join(
        f"{label}: {scrub_raw_ids(value)}" for label, value in fields
    )
