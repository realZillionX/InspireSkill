"""Human-readable output formatter for CLI commands.

Provides compact plain-text output for terminal and agent use.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    lines = ["Job Status"]

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

    # Determine dynamic column widths to avoid truncation while keeping the table aligned.
    name_strings = [scrub_raw_ids(job.get("name", "N/A")) for job in jobs]
    name_width = max(len("Name"), *(len(name) for name in name_strings))
    status_strings = [scrub_raw_ids(job.get("status", "UNKNOWN")) for job in jobs]
    status_width = (
        max(len("Status"), *(len(s) for s in status_strings)) if status_strings else len("Status")
    )
    created_strings = [scrub_raw_ids(job.get("created_at", "N/A")) for job in jobs]
    created_width = max(len("Created"), *(len(created) for created in created_strings))

    header_line = (
        f"{'Name':<{name_width}}  {'Status':<{status_width}}  {'Created':<{created_width}}"
    )
    separator = "-" * len(header_line)
    lines = ["Jobs", header_line, separator]

    for name, status_str, created in zip(name_strings, status_strings, created_strings):

        lines.append(
            f"{name:<{name_width}}  {status_str:<{status_width}}  {created:<{created_width}}"
        )

    lines.append(separator)
    lines.append(f"Total: {len(jobs)} job(s)")

    return "\n".join(lines)


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
    lines = ["Available resources", "GPU configurations:"]

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

    lines.extend(
        [
            "",
            "Usage:",
            "- --quota '1,20,200' -> 1 GPU + 20 CPU + 200 GiB",
            "- --quota '4,80,800' -> 4 GPUs + 80 CPU + 800 GiB",
            "- --quota '0,4,32'   -> CPU-only (4 CPU + 32 GiB)",
            "  See 'inspire resources specs' for valid triples; add --group to disambiguate.",
        ]
    )

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

    lines = [
        "Cluster nodes",
        f"{'Node':<40} {'Pool':<12} {'Status':<12} {'GPUs':<8}",
        "-" * 80,
    ]

    for node in nodes:
        node_label = scrub_raw_ids(
            node.get("name") or node.get("node_name") or node.get("node_id") or "N/A"
        )[:38]
        pool = scrub_raw_ids(node.get("resource_pool", "unknown"))
        status = scrub_raw_ids(node.get("status", "unknown"))
        gpus = str(node.get("gpu_count", "?"))

        lines.append(f"{node_label:<40} {pool:<12} {status:<12} {gpus:<8}")

    lines.append("-" * 80)
    if total:
        lines.append(f"Showing {len(nodes)} of {total} nodes")
    else:
        lines.append(f"Total: {len(nodes)} node(s)")

    return "\n".join(lines)


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

    name_w = max(len("Name"), *(len(r["name"]) for r in rendered))
    version_w = max(len("Version"), *(len(r["version"]) for r in rendered))
    source_w = max(len("Source"), *(len(r["source"]) for r in rendered))
    status_w = max(len("Status"), *(len(r["status"]) for r in rendered))
    framework_w = max(len("Framework"), *(len(r["framework"]) for r in rendered))

    header = (
        f"{'Name':<{name_w}}  {'Version':<{version_w}}  "
        f"{'Source':<{source_w}}  {'Status':<{status_w}}  {'Framework':<{framework_w}}"
    )
    sep = "-" * len(header)
    lines = [header, sep]

    for r in rendered:
        lines.append(
            f"{r['name']:<{name_w}}  {r['version']:<{version_w}}  "
            f"{r['source']:<{source_w}}  {r['status']:<{status_w}}  {r['framework']:<{framework_w}}"
        )

    lines.append(sep)
    lines.append(f"Total: {len(images)} image(s)")

    return "\n".join(lines)


def format_project_list(projects: List[Dict[str, Any]]) -> str:
    """Format project list as a table.

    Args:
        projects: List of project data dictionaries

    Returns:
        Formatted table string
    """
    if not projects:
        return "No projects found."

    lines = [
        f"{'Name':<24} {'Priority':<10} {'Budget remain':<16}",
        "-" * 52,
    ]

    for proj in projects:
        name = scrub_raw_ids(str(proj.get("name", "N/A")))[:24]
        priority = scrub_raw_ids(str(proj.get("priority_level", "")))[:10] or "-"
        budget = proj.get("member_remain_budget", 0.0)
        budget_str = f"{budget:,.0f}"

        lines.append(f"{name:<24} {priority:<10} {budget_str:<16}")

    lines.append("-" * 52)
    lines.append(f"Total: {len(projects)} project(s)")

    return "\n".join(lines)


def format_image_detail(image_data: Dict[str, Any]) -> str:
    """Format image detail as compact key-value lines.

    Args:
        image_data: Image data dictionary

    Returns:
        Formatted string with image details
    """
    lines = ["Image Detail"]

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
