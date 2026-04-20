"""Output formatters for CLI commands."""

from inspire.cli.formatters.json_formatter import format_json, format_json_error
from inspire.cli.formatters.human_formatter import (
    format_job_status,
    format_job_list,
    format_resources,
    format_nodes,
    format_error,
    format_success,
    format_project_list,
)

__all__ = [
    "format_json",
    "format_json_error",
    "format_job_status",
    "format_job_list",
    "format_resources",
    "format_nodes",
    "format_error",
    "format_success",
    "format_project_list",
]
