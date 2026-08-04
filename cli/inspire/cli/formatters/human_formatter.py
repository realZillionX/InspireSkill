"""Human-readable output formatter for CLI commands.

Provides compact plain-text output for terminal and agent use.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.raw_ids import scrub_raw_ids

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def format_error(message: str, hint: Optional[str] = None) -> str:
    """Format an error message.

    Args:
        message: Error message
        hint: Optional hint for fixing

    Returns:
        Formatted error string
    """
    lines = [f"Error: {scrub_raw_ids(message)}"]
    if hint:
        lines.append(f"Hint: {scrub_raw_ids(hint)}")
    return "\n".join(lines)


def format_success(message: str) -> str:
    """Format a success message.

    Args:
        message: Success message

    Returns:
        Formatted success string
    """
    return f"OK {scrub_raw_ids(message)}"


def format_warning(message: str) -> str:
    """Format a warning message.

    Args:
        message: Warning message

    Returns:
        Formatted warning string
    """
    return f"Warning: {scrub_raw_ids(message)}"


def print_error(message: str, hint: Optional[str] = None) -> None:
    """Print an error message to stderr."""
    print(format_error(message, hint), file=sys.stderr)


def _column_width(header: str, values: list[str], *, max_width: int | None = None) -> int:
    return column_width(header, values, max_width=max_width)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _format_duration(ms: str) -> str:
    """Format milliseconds as human-readable duration."""
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
    """Format millisecond timestamp as human-readable datetime."""
    try:
        timestamp = int(timestamp_ms) / 1000
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "Unknown"


def format_epoch(value: Any) -> str:
    """Format a platform-side epoch timestamp as ``YYYY-MM-DD HH:MM:SS``.

    The Inspire platform returns epochs in **two different units**: the
    model registry / events stream returns epoch-milliseconds (13 digits),
    while ``/project/{id}`` returns epoch-seconds (10 digits). This helper
    auto-detects the unit by magnitude (>=1e11 ⇒ ms, else s) and returns
    ``"-"`` for empty / unparseable inputs so it can be used directly in
    output templates.
    """
    if value is None or value == "":
        return "-"
    try:
        n = int(str(value))
    except (ValueError, TypeError):
        # Already formatted as a date-string? Pass through.
        return str(value)
    if n <= 0:
        return "-"
    if n >= 100_000_000_000:  # 1e11 ≈ year 5138 in seconds, clearly ms
        n = n // 1000
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "-"


def format_job_status(job_data: Dict[str, Any]) -> str:
    """Format job status as compact key-value lines.

    Args:
        job_data: Job data from API response

    Returns:
        Formatted string with job status
    """
    status = str(job_data.get("status", "UNKNOWN"))
    lines: list[str] = []

    # Core fields. Raw job_id intentionally omitted; names are the CLI boundary.
    fields = [
        ("Name", job_data.get("name", "N/A")),
        ("Status", status),
        ("Running Time", _format_duration(job_data.get("running_time_ms", "0"))),
    ]

    # Optional fields
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

    # Timeline
    if job_data.get("created_at"):
        fields.append(("Created", _format_timestamp(job_data["created_at"])))
    if job_data.get("finished_at"):
        fields.append(("Finished", _format_timestamp(job_data["finished_at"])))

    for label, value in fields:
        lines.append(f"{label}: {scrub_raw_ids(value)}")

    return "\n".join(lines)


def format_job_list(jobs: List[Dict[str, Any]]) -> str:
    """Format job list as a name-first table.

    Args:
        jobs: List of job data dictionaries

    Returns:
        Formatted table string
    """
    if not jobs:
        return "No jobs found."

    name_strings = [scrub_raw_ids(job.get("name", "N/A")) for job in jobs]
    status_strings = [scrub_raw_ids(job.get("status", "UNKNOWN")) for job in jobs]
    created_strings = [scrub_raw_ids(format_epoch(job.get("created_at"))) for job in jobs]

    widths = [
        _column_width("Name", name_strings, max_width=120),
        _column_width("Status", status_strings, max_width=16),
        _column_width("Created", created_strings, max_width=19),
    ]
    table_rows = list(zip(name_strings, status_strings, created_strings))
    return "\n".join(
        render_table(
            ("Name", "Status", "Created"),
            table_rows,
            widths,
            line_char="─",
        )
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def format_resources(specs: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> str:
    """Format available resources as a table.

    Args:
        specs: List of resource specifications
        groups: List of compute groups

    Returns:
        Formatted string with resources
    """
    lines = ["GPU configurations:"]

    for spec in specs:
        desc = spec.get("description", f"{spec.get('gpu_count', '?')}x GPU")
        lines.append(f"- {desc}")

    lines.extend(
        [
            "",
            "Compute groups:",
        ]
    )

    for group in groups:
        name = scrub_raw_ids(group.get("name", "Unknown"))
        location = scrub_raw_ids(group.get("location", ""))
        lines.append(f"- {name}" + (f" ({location})" if location else ""))

    return "\n".join(lines)


def format_nodes(nodes: List[Dict[str, Any]], total: int = 0) -> str:
    """Format cluster nodes as a table.

    Args:
        nodes: List of node data
        total: Total number of nodes (for pagination)

    Returns:
        Formatted table string
    """
    if not nodes:
        return "No nodes found."

    rows: list[tuple[str, str, str, str]] = []

    for node in nodes:
        node_label = scrub_raw_ids(
            node.get("name") or node.get("node_name") or "-"
        )[:38]
        pool = scrub_raw_ids(node.get("resource_pool", "unknown"))
        status = scrub_raw_ids(node.get("status", "unknown"))
        gpus = str(node.get("gpu_count", "?"))
        rows.append((node_label, pool, status, gpus))

    del total
    widths = [
        _column_width("Node", [row[0] for row in rows], max_width=40),
        _column_width("Pool", [row[1] for row in rows], max_width=20),
        _column_width("Status", [row[2] for row in rows], max_width=16),
        _column_width("GPUs", [row[3] for row in rows], max_width=8),
    ]
    return "\n".join(
        render_table(
            ("Node", "Pool", "Status", "GPUs"),
            rows,
            widths,
            aligns=["left", "left", "left", "right"],
            line_char="─",
        )
    )


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def format_image_list(images: List[Dict[str, Any]]) -> str:
    """Format image list as a table.

    Args:
        images: List of image data dictionaries

    Returns:
        Formatted table string
    """
    if not images:
        return "No images found."

    # Human-readable source labels
    source_labels = {
        "SOURCE_OFFICIAL": "official",
        "SOURCE_PUBLIC": "public",
        "SOURCE_PRIVATE": "private",
    }

    rendered = []
    for img in images:
        raw_source = str(img.get("source", ""))
        rendered.append(
            {
                "name": scrub_raw_ids(img.get("name", "N/A")),
                "version": scrub_raw_ids(img.get("version", "")),
                "source": scrub_raw_ids(source_labels.get(raw_source, raw_source)),
                "status": scrub_raw_ids(img.get("status", "")),
                "framework": scrub_raw_ids(img.get("framework", "")),
            }
        )

    table_rows = [
        (r["name"], r["version"], r["source"], r["status"], r["framework"])
        for r in rendered
    ]
    widths = [
        _column_width("Name", [row[0] for row in table_rows], max_width=64),
        _column_width("Version", [row[1] for row in table_rows], max_width=24),
        _column_width("Source", [row[2] for row in table_rows], max_width=12),
        _column_width("Status", [row[3] for row in table_rows], max_width=18),
        _column_width("Framework", [row[4] for row in table_rows], max_width=24),
    ]
    return "\n".join(
        render_table(
            ("Name", "Version", "Source", "Status", "Framework"),
            table_rows,
            widths,
            line_char="─",
        )
    )


def format_project_list(projects: List[Dict[str, Any]]) -> str:
    """Format project list as a table.

    Args:
        projects: List of project data dictionaries

    Returns:
        Formatted table string
    """
    if not projects:
        return "No projects found."

    rendered: list[dict[str, str]] = []

    for proj in projects:
        name = scrub_raw_ids(str(proj.get("name", "N/A")))
        workspace_names = proj.get("workspace_names") or []
        workspace = ", ".join(scrub_raw_ids(str(name)) for name in workspace_names) or "-"
        priority = scrub_raw_ids(str(proj.get("priority_level") or proj.get("priority_name") or ""))
        priority = priority or "-"
        budget = proj.get("member_remain_budget", 0.0)
        try:
            budget_str = f"{float(budget):,.0f}"
        except (TypeError, ValueError):
            budget_str = str(budget or "0")
        rendered.append(
            {
                "name": name,
                "workspace": workspace,
                "priority": priority,
                "budget": budget_str,
            }
        )

    widths = [
        _column_width("Name", [r["name"] for r in rendered], max_width=48),
        _column_width("Workspace", [r["workspace"] for r in rendered], max_width=32),
        _column_width("Priority", [r["priority"] for r in rendered], max_width=12),
        _column_width("Budget remain", [r["budget"] for r in rendered], max_width=16),
    ]
    rows = [(r["name"], r["workspace"], r["priority"], r["budget"]) for r in rendered]
    return "\n".join(
        render_table(
            ("Name", "Workspace", "Priority", "Budget remain"),
            rows,
            widths,
            aligns=["left", "left", "left", "right"],
            line_char="─",
        )
    )


def format_image_detail(image_data: Dict[str, Any]) -> str:
    """Format image detail as compact key-value lines.

    Args:
        image_data: Image data dictionary

    Returns:
        Formatted string with image details
    """
    lines: list[str] = []

    # Human-readable source labels
    source_labels = {
        "SOURCE_OFFICIAL": "official",
        "SOURCE_PUBLIC": "public",
        "SOURCE_PRIVATE": "private",
    }

    raw_source = str(image_data.get("source", ""))
    source = source_labels.get(raw_source, raw_source)

    fields = [
        ("Name", image_data.get("name", "N/A")),
        ("Version", image_data.get("version", "")),
        ("Framework", image_data.get("framework", "")),
        ("Source", source),
        ("Status", image_data.get("status", "")),
        ("URL", image_data.get("url", "")),
        ("Description", image_data.get("description", "")),
        ("Created", image_data.get("created_at", "")),
    ]

    for label, value in fields:
        if value:
            lines.append(f"{label}: {scrub_raw_ids(value)}")

    return "\n".join(lines)
